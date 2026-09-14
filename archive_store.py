"""Transactional receipt outbox and immutable, content-addressed NAS archival."""
import hashlib
import html
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from readout_timing import elapsed_ms


TABLES = {'scans': 'id', 'bindings': 'id', 'scene_visits': 'id', 'jobs': 'id', 'automation_settings': 'camera'}


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
        self.status = {'status': 'disabled', 'last_error': None, 'last_archived_at': None}
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
                document = "json_set(NEW.document,'$.status',NEW.status)" if table == 'jobs' else 'NEW.document'
                changed = 'NEW.document != OLD.document' + (' OR NEW.status != OLD.status' if table == 'jobs' else '')
                for action, suffix in [('INSERT', ''), ('UPDATE', ' WHEN ' + changed)]:
                    conn.execute(f'''CREATE TRIGGER IF NOT EXISTS archive_{table}_{action.lower()}
                        AFTER {action} ON {table}{suffix} BEGIN
                        INSERT INTO archive_outbox(entity,entity_id,document,recorded_at)
                        VALUES('{table}',NEW.{key},{document},strftime('%Y-%m-%dT%H:%M:%fZ','now')); END''')
            if not conn.execute("SELECT 1 FROM archive_meta WHERE key='backfilled'").fetchone():
                for table, key in TABLES.items():
                    document = "json_set(document,'$.status',status)" if table == 'jobs' else 'document'
                    conn.execute(f'''INSERT INTO archive_outbox(entity,entity_id,document,recorded_at)
                        SELECT ?,{key},{document},? FROM {table}''', (table, core['now']()))
                conn.execute("INSERT INTO archive_meta VALUES('backfilled','1')")

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
        with self.lock:
            status = dict(self.status)
        return status | {'enabled': self.enabled(), 'pending_receipts': pending, 'archived_receipts': archived,
                         'root': os.environ.get('FIELD_ARCHIVE_ROOT'), 'source_instance': self.instance,
                         'ordinary_video_frames_saved': False}

    def _root(self):
        configured = os.environ.get('FIELD_ARCHIVE_ROOT')
        if not configured:
            raise ValueError('Archive root is not configured')
        root = Path(configured).resolve()
        # A pre-existing mount can be required so a missing NAS never fills local /mnt.
        required = os.environ.get('FIELD_ARCHIVE_MOUNT')
        if required and not os.path.ismount(required):
            raise OSError('Archive mount unavailable')
        if root.name != 'FieldRecognitionArchive' or 'VisionCortexExperimentArchive' in root.parts:
            raise ValueError('Archive must use its own FieldRecognitionArchive directory')
        root.mkdir(parents=True, exist_ok=True)
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
        root = self._root()
        with self.core['db']() as conn:
            rows = conn.execute('SELECT * FROM archive_outbox WHERE archived_at IS NOT NULL ORDER BY seq DESC LIMIT 200').fetchall()
            count = conn.execute('SELECT count(*) FROM archive_outbox WHERE archived_at IS NOT NULL').fetchone()[0]
        items, table = [], []
        labels = {'scans': '扫码 / 照片', 'bindings': '仪器绑定', 'scene_visits': '场景关系',
                  'jobs': '面板读数', 'automation_settings': '实验员登记 / 运行设置'}
        for row in rows:
            doc = json.loads(row['document'])
            target = (doc.get('instrument') or {}).get('name') or (doc.get('scene') or {}).get('name') or '、'.join(
                value['name'] for value in doc.get('matches', []) + doc.get('scene_matches', []))
            if row['entity'] == 'jobs' and not doc.get('binding_id'):
                target = '未绑定仪器 · 照片读数'
            state = doc.get('status') or ('已结束' if doc.get('ended_at') else '使用中' if row['entity'] in {'bindings','scene_visits'} else '已记录')
            readings = '、'.join(str(value['text']) for value in doc.get('lines', []) if value.get('text'))
            item = {'sequence': row['seq'], 'entity': row['entity'], 'entity_id': row['entity_id'],
                    'camera_id': doc.get('camera_id') or (row['entity_id'] if row['entity'] == 'automation_settings' else None),
                    'occurred_at': self.event_time(doc, row['recorded_at']), 'operator': doc.get('operator'),
                    'target': target, 'status': state, 'readings': readings, 'receipt': row['receipt_path'],
                    'archived_at': row['archived_at'],
                    'archive_queue_ms': elapsed_ms(row['recorded_at'], row['archived_at']),
                    'write_to_archive_ms': elapsed_ms((doc.get('timing') or {}).get('source_written_at'), row['archived_at'])}
            items.append(item)
            shown_time = datetime.fromisoformat(item['occurred_at']).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
            values = [shown_time, labels[row['entity']], item['camera_id'], item['operator'] or '当时未记录', target, state, readings]
            cells = ''.join('<td>' + html.escape(str(value or '—')) + '</td>' for value in values)
            table.append('<tr>' + cells + '<td><a href="' + html.escape(row['receipt_path'], quote=True) + '">完整回执</a></td></tr>')
        index = {'schema': 'field-recognition-index/1', 'timezone': 'Asia/Shanghai', 'source_instance': self.instance,
                 'updated_at': self.core['now'](), 'archived_receipts': count, 'listed_receipts': len(items),
                 'older_receipts': 'All immutable versions remain in Receipts/', 'items': items}
        overview = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>现场识别 · NAS 留存</title>
<style>body{font:14px/1.7 system-ui;margin:40px;color:#193b42;background:#f4f7f8}table{border-collapse:collapse;background:white;width:100%}td,th{padding:10px;text-align:left;border-bottom:1px solid #d7e2e4}small{color:#60777c}a{color:#236673}</style>
<h1>现场识别 · NAS 留存</h1><p>按相机与日期保留照片、绑定、场景与 OCR 回执。下表为最近 200 次记录变化，完整历史在 Receipts 目录，照片原件与面板图在 Objects 目录。</p>
<p><small>时间显示为 Asia/Shanghai。未记录的人员不补写；识别结果不代表已经确认物理操作。历史版本可能只保留规范化图片，完整回执会明确说明原始字节是否存在。</small></p>
<table><thead><tr><th>时间</th><th>类别</th><th>相机</th><th>实验员</th><th>仪器 / 场景</th><th>状态</th><th>读数</th><th>凭证</th></tr></thead><tbody>''' + ''.join(table) + '</tbody></table></html>'
        # The index is a replaceable view; receipt and object files are immutable.
        for name, raw in [('Index.json', canonical(index)), ('Readme.html', overview.encode())]:
            temporary = root / ('.' + name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                with temporary.open('xb') as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, root / name)
            finally:
                temporary.unlink(missing_ok=True)
        with self.core['db']() as conn:
            conn.execute('INSERT OR REPLACE INTO archive_meta VALUES(?,?)', ('index_count', str(count)))

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
                                 (type(exc).__name__, time.time() + 15, row['seq']))
                with self.lock:
                    self.status.update(status='retrying', last_error=type(exc).__name__)
                continue
            timestamp = self.core['now']()
            with self.core['db']() as conn:
                conn.execute('UPDATE archive_outbox SET archived_at=?,receipt_path=?,last_error=NULL WHERE seq=?',
                             (timestamp, relative, row['seq']))
            with self.lock:
                self.status.update(last_archived_at=timestamp)
        with self.core['db']() as conn:
            archived = conn.execute('SELECT count(*) FROM archive_outbox WHERE archived_at IS NOT NULL').fetchone()[0]
            indexed = conn.execute("SELECT value FROM archive_meta WHERE key='index_count'").fetchone()
        if not indexed or int(indexed[0]) != archived:
            self.write_index()
        with self.core['db']() as conn:
            failed = conn.execute('SELECT last_error FROM archive_outbox WHERE archived_at IS NULL AND last_error IS NOT NULL LIMIT 1').fetchone()
        with self.lock:
            self.status.update(status='retrying' if failed else 'ready', last_error=failed[0] if failed else None)

    def start(self):
        if self.enabled():
            self.worker = threading.Thread(target=self._run, name='receipt-archive', daemon=True)
            self.worker.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                self.step()
            except Exception as exc:
                with self.lock:
                    self.status.update(status='retrying', last_error=type(exc).__name__)
            self.stop.wait(3)

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=3)
