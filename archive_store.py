"""Transactional receipt outbox and immutable, content-addressed NAS archival."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from readout_timing import elapsed_ms


TABLES = {'scans': 'id', 'bindings': 'id', 'scene_visits': 'id', 'jobs': 'id', 'automation_settings': 'camera',
          'photo_measurements': 'id', 'experiment_records': 'id'}
TABLES['binding_handoffs'] = 'id'


def receipt_status(conn, entity, entity_id, source_written_at=None):
    row = conn.execute('SELECT * FROM archive_outbox WHERE entity=? AND entity_id=? ORDER BY seq DESC LIMIT 1',
                       (entity, entity_id)).fetchone()
    if not row:
        return {'status': 'not_queued'}
    return {'status': 'archived' if row['archived_at'] else 'pending', 'sequence': row['seq'],
            'queued_at': row['recorded_at'], 'archived_at': row['archived_at'], 'receipt_path': row['receipt_path'],
            'archive_queue_ms': elapsed_ms(row['recorded_at'], row['archived_at']),
            'write_to_archive_ms': elapsed_ms(source_written_at, row['archived_at'])}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def replace_view(path, data):
    """Atomically refresh a derived view; primary receipt/object files use immutable_write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as target:
            target.write(data); target.flush(); os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def immutable_write(path, data):
    """Publish a complete file; an existing different receipt is never replaced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('Archive content conflict')
        return
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        # There is one writer per database. An overlapping retry may only publish
        # the identical content-addressed file; never a different revision.
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError('Archive content conflict')
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ArchiveStore:
    def __init__(self, core):
        self.core = core
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.worker = None
        self.browse_digests = {}
        self.status = {'status': 'disabled', 'last_error': None, 'last_archived_at': None}
        base = Path(__file__).parent
        self.index_version = '2:' + hashlib.sha256(b''.join((base / p).read_bytes()
            for p in ('archive_catalog.py', 'archive_browse.py', 'archive_readable.py', 'archive_layout.py', 'static/archive.html', 'static/archive.js'))).hexdigest()[:16]
        with core['db']() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS archive_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS archive_outbox(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
                    document TEXT NOT NULL, recorded_at TEXT NOT NULL, archived_at TEXT, receipt_path TEXT,
                    retry_after REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
                CREATE INDEX IF NOT EXISTS archive_entity_version ON archive_outbox(entity,entity_id,seq);
            ''')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(archive_outbox)')}
            for name, declaration in [('retry_after', 'REAL NOT NULL DEFAULT 0'),
                                      ('attempts', 'INTEGER NOT NULL DEFAULT 0'), ('last_error', 'TEXT')]:
                if name not in columns:
                    conn.execute(f'ALTER TABLE archive_outbox ADD COLUMN {name} {declaration}')
            conn.execute('INSERT OR IGNORE INTO archive_meta VALUES(?,?)', ('source_instance', str(uuid.uuid4())))
            self.instance = conn.execute("SELECT value FROM archive_meta WHERE key='source_instance'").fetchone()[0]
            for table, key in TABLES.items():
                if not conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone():
                    continue
                document = "json_set(NEW.document,'$.status',NEW.status)" if table == 'jobs' else 'NEW.document'
                changed = 'NEW.document != OLD.document' + (' OR NEW.status != OLD.status' if table == 'jobs' else '')
                for action, suffix in [('INSERT', ''), ('UPDATE', ' WHEN ' + changed)]:
                    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS archive_{table}_{action.lower()}
                        AFTER {action} ON {table}{suffix} BEGIN
                        INSERT INTO archive_outbox(entity,entity_id,document,recorded_at)
                        VALUES('{table}',NEW.{key},{document},strftime('%Y-%m-%dT%H:%M:%fZ','now')); END''')
            if not conn.execute("SELECT 1 FROM archive_meta WHERE key='backfilled'").fetchone():
                for table, key in TABLES.items():
                    if not conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone():
                        continue
                    document = "json_set(document,'$.status',status)" if table == 'jobs' else 'document'
                    conn.execute(f'''INSERT INTO archive_outbox(entity,entity_id,document,recorded_at)
                        SELECT ?,{key},{document},? FROM {table}''', (table, core['now']()))
                conn.execute("INSERT INTO archive_meta VALUES('backfilled','1')")

        from archive_integrity import ArchiveIntegrity
        self.integrity = ArchiveIntegrity(self)

    def enabled(self):
        return os.environ.get('FIELD_ARCHIVE_ENABLED', '0').lower() in {'1', 'true', 'yes'}

    def job_receipt(self, document):
        with self.core['db']() as conn:
            return receipt_status(conn, 'jobs', document['job_id'],
                                  (document.get('timing') or {}).get('source_written_at'))

    def snapshot(self):
        with self.core['db']() as conn:
            pending, archived = conn.execute('SELECT count(*) FILTER (WHERE archived_at IS NULL), '
                                             'count(*) FILTER (WHERE archived_at IS NOT NULL) FROM archive_outbox').fetchone()
            indexed = dict(conn.execute("SELECT key,value FROM archive_meta WHERE key IN ('index_count','index_version')"))
        if os.environ.get('FIELD_SERVICE_ROLE') in {'api','capture','ocr'}:
            with self.core['db']() as conn:
                runtime=conn.execute("SELECT document,updated_at FROM runtime_status WHERE name='archive'").fetchone()
            with self.lock:
                if runtime and time.time()-runtime['updated_at']<30:
                    self.status.update(json.loads(runtime['document']))
                elif self.enabled():self.status.update(status='retrying',last_error='archive_worker_unavailable')
        with self.lock:
            status = dict(self.status)
        return status | {'enabled': self.enabled(), 'pending_receipts': pending, 'archived_receipts': archived,
                         'navigation_pending': self.enabled() and (indexed.get('index_count') != str(archived) or indexed.get('index_version') != self.index_version),
                         'root': os.environ.get('FIELD_ARCHIVE_ROOT'), 'source_instance': self.instance,
                         'ordinary_video_frames_saved': False, 'integrity': self.integrity.snapshot()}

    def _root(self, *, create=True):
        configured = os.environ.get('FIELD_ARCHIVE_ROOT')
        if not configured:
            raise ValueError('Archive root is not configured')
        root = Path(configured).resolve()
        # A pre-existing mount can be required so a missing NAS never fills local /mnt.
        required = os.environ.get('FIELD_ARCHIVE_MOUNT')
        if required:
            mount = Path(required).resolve()
            if not os.path.ismount(mount):
                raise OSError('Archive mount unavailable')
            root.relative_to(mount)
        if str(root).startswith('/mnt/') and not required:
            raise ValueError('NAS archive requires FIELD_ARCHIVE_MOUNT')
        if root.name != 'FieldRecognitionArchive' or 'VisionCortexExperimentArchive' in root.parts:
            raise ValueError('Archive must use its own FieldRecognitionArchive directory')
        if create:
            root.mkdir(parents=True, exist_ok=True)
        elif not root.is_dir():
            raise FileNotFoundError('Archive directory unavailable')
        return root

    def _artifact(self, root, relative, expected):
        if not re.fullmatch(r'[a-f0-9]{64}', expected or ''):
            raise ValueError('Missing artifact SHA-256')
        data_root = self.core['DATA'].resolve()
        source = (data_root / relative).resolve(strict=True)
        source.relative_to(data_root)
        # A decoded 16-megapixel RGB PNG can exceed the compressed upload limit.
        if not source.is_file() or source.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Invalid archive artifact')
        reserve = max(0, int(os.environ.get('FIELD_ARCHIVE_MIN_FREE_BYTES', '268435456')))
        if shutil.disk_usage(root).free < source.stat().st_size + reserve:
            raise OSError('Archive capacity below reserve; evidence remains queued locally')
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Artifact SHA-256 mismatch')
        destination = Path('Objects') / expected[:2] / (expected + source.suffix)
        immutable_write(root / destination, raw)
        return {'path': destination.as_posix(), 'sha256': expected, 'size_bytes': len(raw)}

    def _capture_artifacts(self, root, capture):
        ident = str(uuid.UUID(capture['capture_id']))
        result = {'image': self._artifact(root, Path('Images') / (ident + '.png'), capture['image_sha256'])}
        if capture.get('original_blob'):
            result['original'] = self._artifact(root, capture['original_blob'], capture['source_sha256'])
        else:
            result['original_status'] = 'not_retained_by_earlier_version'
        return result

    def publish(self, row):
        root = self._root()
        doc = json.loads(row['document'])
        artifacts = {}
        capture_id = doc.get('capture_id') if row['entity'] in {'scans', 'jobs'} else doc.get('scan_id')
        if capture_id:
            if row['entity'] == 'scans':
                capture = doc
            else:
                with self.core['db']() as conn:
                    capture_row = conn.execute('SELECT document FROM scans WHERE id=?', (capture_id,)).fetchone()
                if not capture_row:
                    raise ValueError('Source capture receipt missing')
                capture = json.loads(capture_row[0])
            artifacts = self._capture_artifacts(root, capture)
        if doc.get('crop_image_sha256'):
            ident = str(uuid.UUID(doc['job_id']))
            artifacts['panel'] = self._artifact(root, Path('Images') / (ident + '.png'), doc['crop_image_sha256'])
        for region in doc.get('panel_regions', []):
            if region.get('image_sha256'):
                ident = str(uuid.UUID(region['evidence_id']))
                artifacts['panel_' + ident] = self._artifact(root, Path('Images') / (ident + '.png'), region['image_sha256'])
        if row['entity'] in {'photo_measurements', 'experiment_records'}:
            for source in doc.get('sources', []):
                for kind, artifact in self._capture_artifacts(root, source).items():
                    artifacts[source['capture_id'] + '_' + kind] = artifact
                for region in source.get('panel_regions', []):
                    if region.get('image_sha256'):
                        ident = str(uuid.UUID(region['evidence_id']))
                        artifacts['panel_' + ident] = self._artifact(root, Path('Images') / (ident + '.png'), region['image_sha256'])
        receipt = {'schema': 'field-recognition-receipt/1', 'source_instance': self.instance,
                   'sequence': row['seq'], 'entity': row['entity'], 'entity_id': row['entity_id'],
                   'recorded_at': row['recorded_at'], 'document': doc, 'artifacts': artifacts,
                   'physical_action_confirmed': False}
        raw = canonical(receipt)
        digest = hashlib.sha256(raw).hexdigest()
        camera = doc.get('camera_id') or (row['entity_id'] if row['entity'] == 'automation_settings' else 'Unassigned')
        if not re.fullmatch('[A-Za-z0-9_-]{1,100}', camera):
            camera = 'Camera-' + hashlib.sha256(camera.encode()).hexdigest()[:16]
        timestamp = self.event_time(doc, row['recorded_at'])
        day = datetime.fromisoformat(timestamp).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        relative = Path('Receipts') / camera / day / self.instance / f'{row["seq"]:012d}-{digest}.json'
        immutable_write(root / relative, raw)
        return relative.as_posix()

    @staticmethod
    def event_time(doc, fallback):
        return next((doc.get(key) for key in ('finished_at', 'ended_at', 'started_at', 'submitted_at', 'received_at', 'registered_at')
                     if doc.get(key)), fallback)

    def write_index(self):
        from archive_catalog import build_index, render_index
        from archive_browse import build_views
        with self.lock:
            self.status.update(status='indexing')
        root = self._root()
        with self.core['db']() as conn:
            rows = conn.execute('SELECT * FROM archive_outbox WHERE archived_at IS NOT NULL ORDER BY seq DESC').fetchall()
        index = build_index(rows, self.instance, self.core['now'](), self.integrity.snapshot())
        replace_view(root / 'Index.json', canonical(index))
        replace_view(root / 'Audit.html', render_index(index))
        views = build_views(rows)
        for relative, content in views.items():
            digest = hashlib.sha256(content).hexdigest()
            if self.browse_digests.get(relative) != digest:
                replace_view(root / relative, content)
                self.browse_digests[relative] = digest
        # Only remove obsolete generated navigation files, never receipt/object data
        # or user files. A corrected target may move a readable record's folder.
        current = sorted(p for p in views if p.startswith(('Records/', 'Browse/')))
        with self.core['db']() as conn:
            previous = conn.execute("SELECT value FROM archive_meta WHERE key='readable_files'").fetchone()
        from archive_layout import retire_generated_views
        mappings=retire_generated_views(root,views,json.loads(previous[0]) if previous else [])
        with self.core['db']() as conn:
            old_links=conn.execute("SELECT value FROM archive_meta WHERE key='legacy_links'").fetchone()
        mappings=(json.loads(old_links[0]) if old_links else {}) | mappings
        if mappings:
            replace_view(root/'LegacyLinks.json',canonical(mappings))
            with self.core['db']() as conn:
                conn.execute('INSERT OR REPLACE INTO archive_meta VALUES(?,?)',('legacy_links',json.dumps(mappings)))
        with self.core['db']() as conn:
            conn.execute('INSERT OR REPLACE INTO archive_meta VALUES(?,?)', ('readable_files', json.dumps(current)))
            conn.execute('INSERT OR REPLACE INTO archive_meta VALUES(?,?)', ('index_count', str(len(rows))))
            conn.execute('INSERT OR REPLACE INTO archive_meta VALUES(?,?)', ('index_version', self.index_version))
            conn.execute('INSERT OR REPLACE INTO archive_meta VALUES(?,?)', ('index_integrity', index['integrity'].get('report_path') or ''))

    def step(self, batch_size=20):
        if not self.enabled():
            return
        with self.core['db']() as conn:
            rows = conn.execute('SELECT * FROM archive_outbox WHERE archived_at IS NULL AND retry_after<=? ORDER BY seq LIMIT ?',
                                (time.time(), batch_size)).fetchall()
        if not rows:
            self._root()  # Report a disconnected target even when the queue is empty.
        for row in rows:
            if self.stop.is_set():
                return
            try:
                relative = self.publish(row)
            except Exception as exc:
                with self.core['db']() as conn:
                    conn.execute('UPDATE archive_outbox SET attempts=attempts+1,last_error=?,retry_after=? WHERE seq=?',
                                 (type(exc).__name__, time.time() + min(900, 15 * 2 ** min(row['attempts'], 6)), row['seq']))
                with self.lock:
                    self.status.update(status='retrying', last_error=type(exc).__name__)
                continue
            timestamp = self.core['now']()
            with self.core['db']() as conn:
                conn.execute('UPDATE archive_outbox SET archived_at=?,receipt_path=?,last_error=NULL WHERE seq=?',
                             (timestamp, relative, row['seq']))
            with self.lock:
                self.status.update(last_archived_at=timestamp)
        self.integrity.publish_pending()
        with self.core['db']() as conn:
            archived = conn.execute('SELECT count(*) FROM archive_outbox WHERE archived_at IS NOT NULL').fetchone()[0]
            indexed = conn.execute("SELECT value FROM archive_meta WHERE key='index_count'").fetchone()
            version = conn.execute("SELECT value FROM archive_meta WHERE key='index_version'").fetchone()
            audit = conn.execute("SELECT value FROM archive_meta WHERE key='index_integrity'").fetchone()
        root = self._root()
        if (not indexed or int(indexed[0]) != archived or not version or version[0] != self.index_version
                or not audit or audit[0] != (self.integrity.snapshot()['report_path'] or '')
                or not (root / 'Readme.html').is_file() or not (root / 'Audit.html').is_file() or not (root / 'Index.json').is_file()
                or not (root / 'Browse/Readme.html').is_file() or not (root / '00_归档导航.html').is_file()):
            self.write_index()
        with self.core['db']() as conn:
            failed = conn.execute('SELECT last_error FROM archive_outbox WHERE archived_at IS NULL AND last_error IS NOT NULL LIMIT 1').fetchone()
        with self.lock:
            self.status.update(status='retrying' if failed else 'ready', last_error=failed[0] if failed else None)

    def start(self):
        if self.enabled():
            self.worker = threading.Thread(target=self._run, name='receipt-archive', daemon=True)
            self.worker.start()
            self.integrity.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                self.step()
            except Exception as exc:
                with self.lock:
                    self.status.update(status='retrying', last_error=type(exc).__name__)
            self.stop.wait(3)

    def close(self):
        self.integrity.close()
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=3)
