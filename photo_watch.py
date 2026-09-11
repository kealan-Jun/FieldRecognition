"""Read-only watch of the bound camera's existing voice photographs."""
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

    def enabled(self):
        return os.environ.get('FIELD_SAVED_PHOTO_WATCH_ENABLED', '0').lower() in {'1', 'true', 'yes'}

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
        return result | {'enabled': self.enabled(), 'configured': bool(os.environ.get('FIELD_SAVED_PHOTO_ROOT')),
                         'poll_seconds': 2, 'reads_existing_photos_only': True}

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
            self.stop.wait(2)

    def active_binding(self):
        camera = self.core['receiver_camera']
        target = camera.target if camera else os.environ.get('FIELD_CAMERA_ID')
        with self.core['db']() as conn:
            row = conn.execute('SELECT document FROM bindings WHERE camera=? AND ended IS NULL', (target,)).fetchone()
        if row:
            binding = json.loads(row['document'])
            if self.core['current_readout_binding'](binding):
                return binding
        return None

    def step(self):
        if not self.enabled() or self.stop.is_set():
            return
        binding = self.active_binding()
        if not binding:
            self.binding_id = None
            self.pending.clear()
            self.update(status='waiting_binding', binding_id=None, last_job_id=None,
                        last_photo=None, last_photo_status=None, detail=None)
            return
        if self.binding_id != binding['binding_id']:
            self.pending.clear()
            self.binding_id = binding['binding_id']
            self.update(binding_id=self.binding_id, last_job_id=None,
                        last_photo=None, last_photo_status=None, detail=None)
        configured = os.environ.get('FIELD_SAVED_PHOTO_ROOT')
        if not configured:
            self.update(status='storage_unconfigured')
            return
        root = Path(configured)
        camera = binding['camera_id']
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', camera):
            self.update(status='invalid_camera_directory')
            return
        # Never walk the NAS root or other cameras; only voice_photos/<bound camera>.
        camera_root = root / camera
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
        started = datetime.fromisoformat(binding['started_at']).astimezone(zone)
        today = datetime.now(timezone.utc).astimezone(zone).strftime('%Y-%m-%d')
        with self.core['db']() as conn:
            seen = {row['path']: row['signature'] for row in conn.execute(
                'SELECT path,signature FROM photo_watch_seen WHERE binding_id=?', (self.binding_id,))}
            if conn.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 4:
                self.update(status='waiting_queue')
                return
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
                    candidates.append((relative, signature))
        waiting = set(path for path, _ in candidates)
        self.pending = {path: value for path, value in self.pending.items() if path in waiting}
        self.update(status='watching', last_checked_at=self.core['now'](), pending_files=len(candidates))
        for relative, signature in candidates:
            prior = self.pending.get(relative)
            if not prior or prior[0] != signature:
                self.pending[relative] = (signature, self.clock())
                continue
            if self.clock() - prior[1] < 1:
                continue
            if self.stop.is_set() or not self.core['current_readout_binding'](binding):
                return
            try:
                body = self.core['SavedPhotoRequest'](binding_id=self.binding_id, image_path=relative)
                job = self.core['read_saved_panel'](body, trigger='voice_photo_directory')
                status, detail, job_id = 'submitted', None, job['job_id']
            except HTTPException as exc:
                if exc.status_code == 429:
                    self.update(status='waiting_queue')
                    return
                status, detail, job_id = 'rejected', str(exc.detail), None
            with self.core['db']() as conn:
                conn.execute('INSERT OR REPLACE INTO photo_watch_seen VALUES(?,?,?,?,?,?,?)',
                             (self.binding_id, relative, signature, status, job_id, detail, self.core['now']()))
            self.pending.pop(relative, None)
            self.update(last_photo=relative, last_job_id=job_id, last_photo_status=status, detail=detail)
            # At most one new photo per poll, with the existing global queue bound.
            return
