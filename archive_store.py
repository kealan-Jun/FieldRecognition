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
from archive_progress import advance


TABLES = {'scans': 'id', 'bindings': 'id', 'scene_visits': 'id', 'jobs': 'id', 'automation_settings': 'camera',
          'photo_measurements': 'id', 'experiment_records': 'id'}
TABLES['binding_handoffs'] = 'id'


def receipt_status(conn, entity, entity_id, source_written_at=None):
    row = conn.execute('SELECT * FROM archive_outbox WHERE entity=? AND entity_id=? ORDER BY seq DESC LIMIT 1',
                       (entity, entity_id)).fetchone()
    if not row:
        return {'status': 'not_queued'}
    keys = row.keys()
    published_at = row['published_at'] if 'published_at' in keys else None
    readable_at = row['readable_at'] if 'readable_at' in keys else None
    readability_basis = row['readability_basis'] if 'readability_basis' in keys else None
    archived_at = row['archived_at']
    # Keep the legacy archived_at contract while exposing the explicit
    # publication/readability milestones. Older rows intentionally stay null.
    queue_end = published_at or archived_at
    return {'status': 'archived' if archived_at else 'pending', 'sequence': row['seq'],
            'queued_at': row['recorded_at'], 'archived_at': archived_at,
            'published_at': published_at, 'readable_at': readable_at,
            'archive_published_at': published_at, 'archive_readable_at': readable_at,
            'readability_basis': readability_basis, 'receipt_path': row['receipt_path'],
            'archive_queue_ms': elapsed_ms(row['recorded_at'], queue_end),
            'write_to_archive_ms': elapsed_ms(source_written_at, queue_end),
            'archive_publication_ms': elapsed_ms(row['recorded_at'], published_at),
            'archive_readability_ms': elapsed_ms(published_at, readable_at)}


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
    advance()


def immutable_write(path, data):
    """Publish a complete file; an existing different receipt is never replaced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('Archive content conflict')
        advance()
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
    advance()


class ArchiveStore:
    def __init__(self, core):
        self.core = core
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.worker = None
        self.browse_digests = {}
        self.status = {'status': 'disabled', 'last_error': None, 'last_archived_at': None}
        base = Path(__file__).parent
        self.index_version = '3:' + hashlib.sha256(b''.join((base / p).read_bytes()
            for p in ('archive_catalog.py', 'archive_events.py', 'archive_paths.py', 'archive_migration.py',
                      'measurement_records.py', 'static/archive.html', 'static/archive.js'))).hexdigest()[:16]
        with core['db']() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS archive_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS archive_outbox(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
                    document TEXT NOT NULL, recorded_at TEXT NOT NULL, archived_at TEXT, receipt_path TEXT,
                    published_at TEXT, readable_at TEXT, readability_basis TEXT,
                    retry_after REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
                CREATE INDEX IF NOT EXISTS archive_entity_version ON archive_outbox(entity,entity_id,seq);
            ''')
            columns = {row[1] for row in conn.execute('PRAGMA table_info(archive_outbox)')}
            for name, declaration in [('retry_after', 'REAL NOT NULL DEFAULT 0'),
                                      ('attempts', 'INTEGER NOT NULL DEFAULT 0'), ('last_error', 'TEXT')]:
                if name not in columns:
                    conn.execute(f'ALTER TABLE archive_outbox ADD COLUMN {name} {declaration}')
            for name in ('published_at', 'readable_at', 'readability_basis'):
                if name not in columns:
                    conn.execute(f'ALTER TABLE archive_outbox ADD COLUMN {name} TEXT')
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
        from archive_paths import ArchivePaths
        return ArchivePaths(root).asset(destination.as_posix(), raw)

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
        relative = Path('.System/Receipts') / camera / day / self.instance / f'{row["seq"]:012d}-{digest}.json'
        immutable_write(root / relative, raw)
        return relative.as_posix()

    @staticmethod
    def event_time(doc, fallback):
        return next((doc.get(key) for key in ('finished_at', 'ended_at', 'started_at', 'submitted_at', 'received_at', 'registered_at')
                     if doc.get(key)), fallback)

    def write_index(self):
        from archive_migration import rebuild
        with self.lock:
            self.status.update(status='indexing')
        with self.core['db']() as conn:
            rows = conn.execute('SELECT * FROM archive_outbox WHERE archived_at IS NOT NULL ORDER BY seq DESC').fetchall()
        # The verifier cannot observe a file between path resolution and rename.
        if not self.integrity.lock.acquire(timeout=5):
            with self.lock:
                self.status.update(status='waiting_integrity')
            return None
        try:
            started = time.monotonic()
            result = rebuild(self, rows)
            with self.lock:
                self.status['last_view_rebuild'] = {'duration_ms': round((time.monotonic()-started)*1000, 2),
                    'clock_basis': 'process_monotonic', 'scope': 'whole_archive_derived_views', **result}
            return result
        finally:
            self.integrity.lock.release()

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
                publication_started = time.monotonic()
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
                conn.execute('UPDATE archive_outbox SET archived_at=?,published_at=?,receipt_path=?,last_error=NULL WHERE seq=?',
                             (timestamp, timestamp, relative, row['seq']))
            with self.lock:
                self.status.update(last_archived_at=timestamp)
                doc = json.loads(row['document'])
                self.status['last_receipt_publication'] = {'sequence': row['seq'],
                    'entity': row['entity'], 'entity_id': row['entity_id'], 'task_id': doc.get('job_id'),
                    'group_id': (doc.get('measurement_context') or {}).get('burst_id') or doc.get('measurement_id'),
                    'duration_ms': round((time.monotonic()-publication_started)*1000, 2),
                    'clock_basis': 'process_monotonic', 'scope': 'receipt_and_artifacts'}
        self.integrity.publish_pending()
        with self.core['db']() as conn:
            # Include publications whose view pass was interrupted in an earlier
            # step. Legacy receipts have no measured published_at and stay null.
            published_sequences = [r[0] for r in conn.execute(
                'SELECT seq FROM archive_outbox WHERE published_at IS NOT NULL AND readable_at IS NULL')]
            archived = conn.execute('SELECT count(*) FROM archive_outbox WHERE archived_at IS NOT NULL').fetchone()[0]
            indexed = conn.execute("SELECT value FROM archive_meta WHERE key='index_count'").fetchone()
            version = conn.execute("SELECT value FROM archive_meta WHERE key='index_version'").fetchone()
            audit = conn.execute("SELECT value FROM archive_meta WHERE key='index_integrity'").fetchone()
        root = self._root()
        index_ready = False
        if (published_sequences or not indexed or int(indexed[0]) != archived or not version or version[0] != self.index_version
                or not audit or audit[0] != (self.integrity.snapshot()['report_path'] or '')
                or not (root / 'Readme.html').is_file() or not (root / '.System/Audit.html').is_file() or not (root / '.System/Index.json').is_file()
                or not (root / '.System/MigrationMap.json').is_file()):
            index_ready = self.write_index() is not None
        if index_ready and published_sequences:
            readable_at = self.core['now']()
            placeholders = ','.join('?' for _ in published_sequences)
            with self.core['db']() as conn:
                conn.execute(f"""UPDATE archive_outbox SET readable_at=?,readability_basis=?
                                 WHERE seq IN ({placeholders}) AND published_at IS NOT NULL
                                   AND readable_at IS NULL""",
                             (readable_at, 'archive_writer_derived_views_published', *published_sequences))
                # Force a second derived-view pass so Evidence.json and the
                # index expose the readability confirmation just established.
                conn.execute("INSERT OR REPLACE INTO archive_meta VALUES('index_version','readability_metadata_pending')")
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
