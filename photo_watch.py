"""Read-only watch of the configured camera's photographs, with optional binding."""
import copy
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException

POLL_SECONDS = .5
STABLE_SECONDS = .5


class SavedPhotoWatcher:
    def __init__(self, core, *, clock=time.monotonic):
        self.core, self.clock = core, clock
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = None
        self.pending = {}
        self.binding_id = None
        self.state = {'status': 'disabled', 'binding_id': None, 'last_job_id': None}
        with core['db']() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS photo_watch_seen(binding_id TEXT, path TEXT, signature TEXT, '
                         'status TEXT, job_id TEXT, detail TEXT, observed_at TEXT, PRIMARY KEY(binding_id,path))')
            conn.execute('CREATE TABLE IF NOT EXISTS photo_watch_files(camera TEXT, path TEXT, signature TEXT, '
                         'status TEXT, job_id TEXT, detail TEXT, observed_at TEXT, PRIMARY KEY(camera,path))')
            conn.execute('CREATE TABLE IF NOT EXISTS photo_watch_windows(camera TEXT PRIMARY KEY, started_at TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS photo_ingest_queue(camera TEXT,path TEXT,signature TEXT,document TEXT NOT NULL,PRIMARY KEY(camera,path))')
            for row in conn.execute('SELECT path,signature,document FROM photo_ingest_queue WHERE camera=?',(os.environ.get('FIELD_CAMERA_ID'),)):
                item = json.loads(row['document'])
                self.pending[row['path']] = (row['signature'], self.clock(), item['first_observed_at'], item['is_backfill'], item.get('stable_at'))

    def enabled(self):
        return os.environ.get('FIELD_SAVED_PHOTO_WATCH_ENABLED', '0').lower() in {'1', 'true', 'yes'}

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
        with self.core['db']() as conn:
            queued = conn.execute('SELECT count(*) FROM photo_ingest_queue WHERE camera=?',(os.environ.get('FIELD_CAMERA_ID'),)).fetchone()[0]
        return result | {'enabled': self.enabled(), 'configured': bool(os.environ.get('FIELD_SAVED_PHOTO_ROOT')),
                         'persisted_pending_files': queued,
                         'poll_seconds': POLL_SECONDS, 'stable_seconds': STABLE_SECONDS, 'reads_existing_photos_only': True}

    def start(self):
        if not self.enabled():
            return
        self.worker = threading.Thread(target=self._run, name='voice-photo-watch', daemon=True)
        self.worker.start()

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=3)

    def update(self, **fields):
        with self.lock:
            self.state.update(fields)

    def _run(self):
        while not self.stop.is_set():
            try:
                self.step()
            except (OSError, ValueError, KeyError):
                self.update(status='storage_unavailable')
            except Exception:
                self.update(status='watch_error')
            self.stop.wait(POLL_SECONDS)

    def active_bindings(self):
        camera = self.core['receiver_camera']
        target = camera.target if camera else os.environ.get('FIELD_CAMERA_ID')
        with self.core['db']() as conn:
            rows = conn.execute('SELECT document FROM bindings WHERE camera=? AND ended IS NULL', (target,)).fetchall()
        bindings = []
        for row in rows:
            binding = json.loads(row['document'])
            if self.core['current_readout_binding'](binding):
                bindings.append(binding)
        return bindings

    def step(self):
        if not self.enabled() or self.stop.is_set():
            return
        bindings = self.active_bindings()
        binding = bindings[0] if len(bindings) == 1 else None
        self.binding_id = binding['binding_id'] if binding else None
        self.update(binding_id=self.binding_id, binding_ids=[b['binding_id'] for b in bindings])
        configured = os.environ.get('FIELD_SAVED_PHOTO_ROOT')
        if not configured:
            self.update(status='storage_unconfigured')
            return
        root = Path(configured)
        receiver = self.core['receiver_camera']
        camera = receiver.target if receiver else os.environ.get('FIELD_CAMERA_ID')
        if not camera or not re.fullmatch(r'[a-zA-Z0-9_-]+', camera):
            self.update(status='invalid_camera_directory')
            return
        # Never walk the NAS root or other cameras; only voice_photos/<configured camera>.
        camera_root = root / camera
        with self.core['db']() as conn:
            settings = conn.execute('SELECT document FROM automation_settings WHERE camera=?', (camera,)).fetchone()
            registered = json.loads(settings['document']).get('registered_at') if settings else None
            conn.execute('INSERT OR IGNORE INTO photo_watch_windows VALUES(?,?)', (camera, registered or self.core['now']()))
            window_start = conn.execute('SELECT started_at FROM photo_watch_windows WHERE camera=?', (camera,)).fetchone()[0]
        if not root.is_dir():
            self.update(status='storage_unavailable')
            return
        if not camera_root.is_dir():
            self.update(status='waiting_photo')
            return
        if camera_root.is_symlink():
            self.update(status='invalid_camera_directory')
            return
        zone = ZoneInfo(os.environ.get('FIELD_SAVED_PHOTO_TIMEZONE', 'Asia/Shanghai'))
        started = datetime.fromisoformat(window_start).astimezone(zone)
        today = datetime.now(timezone.utc).astimezone(zone).strftime('%Y-%m-%d')
        with self.core['db']() as conn:
            prefix = camera + '/'
            seen = {row['path']: row['signature'] for row in conn.execute(
                'SELECT path,signature FROM photo_watch_seen WHERE substr(path,1,?)=?', (len(prefix), prefix))}
            seen.update({row['path']: row['signature'] for row in conn.execute(
                'SELECT path,signature FROM photo_watch_files WHERE camera=?', (camera,))})
            queue_full = conn.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 4
        candidates = []
        for day in sorted(camera_root.iterdir()):
            if (not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day.name) or day.name < started.strftime('%Y-%m-%d')
                    or day.name > today or day.is_symlink() or not day.is_dir()):
                continue
            for moment in sorted(day.iterdir()):
                if not re.fullmatch(r'\d{2}-\d{2}-\d{2}', moment.name) or moment.is_symlink() or not moment.is_dir():
                    continue
                for photo in sorted(moment.iterdir()):
                    if photo.is_symlink() or photo.suffix.lower() not in {'.jpg', '.jpeg', '.png'} or not photo.is_file():
                        continue
                    match = re.fullmatch(r'(\d{8})_(\d{6})(?:_\d+)?\.(?:jpg|jpeg|png)', photo.name, re.I)
                    if not match:
                        continue
                    try:
                        captured = datetime.strptime(match[1] + match[2], '%Y%m%d%H%M%S').replace(tzinfo=zone)
                    except ValueError:
                        continue
                    if captured < started:
                        continue
                    info = photo.stat()
                    signature = f'{info.st_size}:{info.st_mtime_ns}'
                    relative = photo.relative_to(root).as_posix()
                    if seen.get(relative) == signature:
                        continue
                    candidates.append((relative, signature, info.st_mtime))
        waiting = {item[0] for item in candidates}
        self.pending = {path: value for path, value in self.pending.items() if path in waiting}
        with self.core['db']() as conn:
            processed = conn.execute("SELECT count(*) FROM photo_watch_files WHERE camera=? AND status='submitted'", (camera,)).fetchone()[0]
        self.update(status='watching', camera_id=camera, watch_started_at=window_start,
                    last_checked_at=self.core['now'](), pending_files=len(candidates), submitted_files=processed)
        for relative, signature, written in candidates:
            prior = self.pending.get(relative)
            if not prior or prior[0] != signature:
                self.pending[relative] = (signature, self.clock(), self.core['now'](),
                                          time.time() - written > 30, None)
                with self.core['db']() as conn:
                    stored = conn.execute('SELECT signature,document FROM photo_ingest_queue WHERE camera=? AND path=?', (camera, relative)).fetchone()
                if stored and stored['signature'] == signature:
                    item = json.loads(stored['document'])
                    self.pending[relative] = (signature, self.clock(), item['first_observed_at'], item['is_backfill'], None)
            prior = self.pending[relative]
            if self.clock() - prior[1] >= STABLE_SECONDS and prior[4] is None:
                self.pending[relative] = prior[:4] + (self.core['now'](),)
            prior = self.pending[relative]
            with self.core['db']() as conn:
                row = conn.execute('SELECT signature,document FROM photo_ingest_queue WHERE camera=? AND path=?', (camera, relative)).fetchone()
                item = json.loads(row['document']) if row and row['signature'] == signature else {'attempts': 0, 'retry_after': 0}
                item.update(first_observed_at=prior[2], stable_at=prior[4], is_backfill=prior[3])
                raw = json.dumps(item)
                if not row or row['signature'] != signature or row['document'] != raw:
                    conn.execute('INSERT OR REPLACE INTO photo_ingest_queue VALUES(?,?,?,?)', (camera, relative, signature, raw))
        if queue_full:
            self.update(status='waiting_queue')
            return
        for relative, signature, written in candidates:
            prior = self.pending[relative]
            if self.clock() - prior[1] < STABLE_SECONDS:
                continue
            if self.stop.is_set():
                return
            with self.core['db']() as conn:
                item = json.loads(conn.execute('SELECT document FROM photo_ingest_queue WHERE camera=? AND path=?', (camera, relative)).fetchone()[0])
            if item.get('retry_after', 0) > time.time():
                continue
            try:
                body = self.core['SavedPhotoRequest'](image_path=relative)
                job = self.core['read_saved_panel'](body, trigger='voice_photo_directory',
                    observation={'first_observed_at': prior[2], 'stable_at': prior[4],
                                 'is_backfill': prior[3], 'watch_poll_seconds': POLL_SECONDS,
                                 'file_stable_seconds': STABLE_SECONDS,
                                 'ingest_retry_count': item.get('attempts', 0),
                                 'ingest_last_error': item.get('last_error'),
                                 'ingest_first_failed_at': item.get('first_failed_at'),
                                 'ingest_last_failed_at': item.get('last_failed_at')})
                status, detail, job_id = 'submitted', None, job['job_id']
            except HTTPException as exc:
                if exc.status_code in {429, 502, 503, 504} or (exc.status_code == 409 and '仍在写入' in str(exc.detail)):
                    item.update(attempts=item.get('attempts', 0)+1, retry_after=time.time()+(1 if exc.status_code==429 else 3), last_error=str(exc.detail))
                    item.setdefault('first_failed_at', self.core['now']())
                    item['last_failed_at'] = self.core['now']()
                    with self.core['db']() as conn:
                        conn.execute('UPDATE photo_ingest_queue SET document=? WHERE camera=? AND path=?', (json.dumps(item), camera, relative))
                    self.update(status='waiting_queue' if exc.status_code == 429 else 'storage_unavailable', detail=str(exc.detail))
                    return
                status, detail, job_id = 'rejected', str(exc.detail), None
            with self.core['db']() as conn:
                conn.execute('INSERT OR REPLACE INTO photo_watch_files VALUES(?,?,?,?,?,?,?)',
                             (camera, relative, signature, status, job_id, detail, self.core['now']()))
                conn.execute('DELETE FROM photo_ingest_queue WHERE camera=? AND path=?', (camera, relative))
            self.pending.pop(relative, None)
            self.update(last_photo=relative, last_job_id=job_id, last_photo_status=status, detail=detail)
            # At most one new photo per poll, with the existing global queue bound.
            return
