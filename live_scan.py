"""Live preview and opt-in, bounded continuous QR binding on the selected camera."""
import asyncio
import copy
import json
import threading
import time
import uuid

import cv2
import numpy as np
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from qr_decode import decode_qr

ACTIVE = {'scanning', 'waiting_camera'}
LEASE_SECONDS = 20


def frame_key(metadata):
    return tuple(metadata.get(k) for k in ('decoded_frame_id', 'sequence', 'timestamp_us', 'sender_id', 'camera_id'))


class StartScan(BaseModel):
    operator: str = Field(min_length=1, max_length=80)


class LiveScanner:
    def __init__(self, core):
        self.core = core
        self.lock = threading.RLock()
        self.session = None
        self.last_seen = 0
        self.worker = None
        self.shutdown = threading.Event()

    def camera(self):
        camera = self.core['receiver_camera']
        if camera is None:
            raise HTTPException(409, '连续视频需要配置挂脖相机主码流；当前仍可上传或拍照')
        return camera

    def active_bindings(self):
        with self.core['db']() as conn:
            rows = conn.execute('SELECT document FROM bindings WHERE camera=? AND ended IS NULL ORDER BY rowid',
                                (self.camera().target,)).fetchall()
        return [json.loads(row['document']) for row in rows]

    def active_binding(self):
        bindings = self.active_bindings()
        return bindings[0] if bindings else None

    def refresh_bindings(self):
        bindings = self.active_bindings()
        visits = [v for v in self.core['scene_visits']() if v['camera_id'] == self.camera().target]
        self.session.update(bindings=bindings, binding=bindings[0] if len(bindings) == 1 else None,
                            scene_visits=visits,
                            message=f'已关联 {len(bindings)} 台仪器、{len(visits)} 个场景；仅为新二维码建立关联')

    def start(self, operator, *, owner='browser'):
        if not operator.strip():
            raise HTTPException(422, '请先填写实验员姓名或编号')
        camera = self.camera()
        with self.lock:
            if self.session and self.session['status'] in ACTIVE:
                raise HTTPException(409, '已有连续扫码正在运行，请先停止')
            self.session = {'session_id': str(uuid.uuid4()), 'status': 'scanning',
                            'camera_id': camera.target, 'operator': operator.strip(), 'owner': owner,
                            'started_at': self.core['now'](), 'frames_scanned': 0,
                            'message': '连续扫码中：对准仪器码即可绑定；场景码可单独识别',
                            'scan': None, 'binding': None}
            if owner == 'automation':
                self.session['after_frame_id'] = camera.snapshot().get('decoded_frames', 0)
            self.last_seen = time.monotonic()
            self.refresh_bindings()
            self.worker = threading.Thread(target=self._run, args=(self.session['session_id'],),
                                           name='continuous-qr', daemon=True)
            self.worker.start()
            return copy.deepcopy(self.session)

    def read(self, session_id, *, cancel=False):
        with self.lock:
            if not self.session or self.session['session_id'] != str(session_id):
                raise HTTPException(404, '连续扫码会话不存在，请重新开始')
            self.last_seen = time.monotonic()
            if cancel and self.session['status'] in ACTIVE:
                self.session.update(status='stopped', message='连续扫码已停止，已有绑定继续保留')
            return copy.deepcopy(self.session)

    def _running(self, session_id):
        if self.shutdown.is_set() or not self.session or self.session['session_id'] != session_id:
            return False
        if self.session['status'] not in ACTIVE:
            return False
        if self.session.get('owner') != 'automation' and time.monotonic() - self.last_seen > LEASE_SECONDS:
            self.session.update(status='stopped', message='页面已断开，连续扫码已停止；已有绑定继续保留')
            return False
        return True

    def _run(self, session_id):
        previous, last_full, last_match = None, time.monotonic(), None
        while not self.shutdown.wait(.015):
            with self.lock:
                if not self._running(session_id):
                    return
            try:
                frame, metadata = self.camera().frame()
            except ValueError:
                with self.lock:
                    if self._running(session_id):
                        self.session.update(status='waiting_camera', message='等待相机新画面，恢复连接后继续扫码')
                self.shutdown.wait(.2)
                continue
            token = frame_key(metadata)
            if token == previous:
                continue
            previous = token
            acquired = time.monotonic()
            try:
                # Fast native decoding on fresh frames; periodically retry difficult labels.
                full = acquired - last_full >= 1
                decoded = decode_qr(frame, fast=not full)
                if full:
                    last_full = acquired
                matches = self.core['qr_matches'](*decoded[:2])
                with self.lock:
                    if not self._running(session_id):
                        return
                    if self.session.get('owner') == 'automation':
                        info = self.camera().snapshot()
                        if not info.get('service_status_available') or not info.get('service_status', {}).get('online'):
                            continue
                        if metadata.get('decoded_frame_id', 0) <= self.session['after_frame_id']:
                            continue
                    self.session.update(status='scanning', frames_scanned=self.session['frames_scanned'] + 1,
                                        last_frame_metadata=metadata, decode_ms=decoded[2]['elapsed_ms'])
                    if self.session['message'] == '等待相机新画面，恢复连接后继续扫码':
                        self.session['message'] = '连续扫码中：对准仪器码即可绑定；场景码可单独识别'
                    # Slow decoding must not commit an old frame as a new observation.
                    if time.monotonic() - acquired + metadata.get('frame_age_ms', 0) / 1000 > 1:
                        continue
                    if not (matches['matches'] or matches['scene_matches']):
                        if matches['unknown']:
                            self.session['message'] = '读到了未登记二维码，请对准场景或仪器码'
                        continue
                    existing_ids = {b['instrument']['id'] for b in self.active_bindings()}
                    visits = [v for v in self.core['scene_visits']() if v['camera_id'] == self.camera().target]
                    scene_ids = {v['scene']['id'] for v in visits}
                    new_instruments = [m for m in matches['matches'] if m['id'] not in existing_ids]
                    new_scenes = [m for m in matches['scene_matches'] if m['id'] not in scene_ids]
                    if not new_instruments and not new_scenes:
                        self.refresh_bindings()
                        continue
                    key = (tuple(m['id'] for m in new_instruments), tuple(m['id'] for m in new_scenes))
                    if key == last_match:
                        continue
                    last_match = key
                    self._accept(frame, metadata, decoded, matches)
            except Exception as exc:
                with self.lock:
                    if self._running(session_id):
                        self.session.update(status='failed', message=f'连续扫码失败（{type(exc).__name__}），请重新开始')
                return

    def _accept(self, frame, metadata, decoded, matches):
        # Caller holds the same lock used by stop/reset: no late write after stop returns.
        result = self.core['save_camera_scan'](frame, metadata, decoded=decoded,
                                             operator=self.session['operator'], scan_session_id=self.session['session_id'])
        self.session['scan'] = result
        errors = []
        # Every identity comes from this decoded frame; each relation is independent.
        for scene in matches['scene_matches']:
            try:
                visit = self.core['enter_scene'](self.core['SceneEntry'](
                    scan_id=result['scan_id'], scene_id=scene['id'], operator=self.session['operator']), automatic=True)
                self.session['scene_visit'] = visit
            except HTTPException as exc:
                errors.append(str(exc.detail))
        for instrument in matches['matches']:
            try:
                self.core['bind'](self.core['BindingRequest'](scan_id=result['scan_id'],
                    instrument_id=instrument['id'], operator=self.session['operator']), automatic=True)
            except HTTPException as exc:
                errors.append(str(exc.detail))
        self.refresh_bindings()
        self.session.update(status='scanning', binding_errors=errors)
        if errors:
            self.session['message'] += ' · ' + '；'.join(dict.fromkeys(errors))

    def observe_service(self, observation):
        """Persist upstream session changes across page and local app restarts."""
        camera = self.camera()
        with self.lock:
            timestamp = self.core['now']()
            with self.core['db']() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT document FROM camera_service_state WHERE camera=?', (camera.target,)).fetchone()
                previous = json.loads(row['document']) if row else None
                old_id = previous.get('media_session_id') if previous else None
                new_id = observation.get('media_session_id')
                changed = old_id not in (None, 0) and new_id not in (None, 0) and old_id != new_id
                # Liveness is transient: a missed heartbeat is not a device stop.
                # Suspend inference while offline, retaining the same bindings when
                # the same acquisition session returns. A new session invalidates.
                if changed:
                    reason = 'device_media_session_changed'
                    for table in ('bindings', 'scene_visits'):
                        for record in conn.execute(f'SELECT * FROM {table} WHERE camera=? AND ended IS NULL', (camera.target,)).fetchall():
                            doc = json.loads(record['document']) | {'ended_at': timestamp, 'end_reason': reason,
                                                                   'service_observation': observation}
                            conn.execute(f'UPDATE {table} SET ended=?,document=? WHERE id=?', (timestamp, json.dumps(doc), record['id']))
                    conn.execute('INSERT OR REPLACE INTO camera_resets VALUES(?,?)', (camera.target, timestamp))
                    if self.session and self.session['status'] in ACTIVE | {'bound'}:
                        self.session.update(status='stopped', binding=None,
                                            message='设备采集会话已变化，请重新扫码绑定')
                # Preserve a known session marker through an offline status with a zero marker.
                stored = observation | {'media_session_id': new_id or old_id}
                if not observation['online']:
                    stored['offline_since'] = (previous or {}).get('offline_since') or timestamp
                conn.execute('INSERT OR REPLACE INTO camera_service_state VALUES(?,?)', (camera.target, json.dumps(stored)))

    def close(self):
        self.shutdown.set()
        if self.worker:
            self.worker.join(timeout=3)


def preview_part(camera, previous):
    try:
        frame, metadata = camera.frame()
        token = frame_key(metadata)
        if token == previous:
            return None, token
        # Preview is scaled independently; recognition always uses the original image.
        if frame.shape[1] > 960:
            frame = cv2.resize(frame, (960, round(frame.shape[0] * 960 / frame.shape[1])))
    except ValueError:
        token = 'offline'
        if previous == token:
            return None, token
        frame = np.full((480, 768, 3), 235, np.uint8)
        cv2.putText(frame, 'Waiting for live camera...', (65, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (65, 85, 90), 2)
    ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise ValueError('Preview encoding failed')
    jpeg = encoded.tobytes()
    return b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(jpeg)).encode() + b'\r\n\r\n' + jpeg + b'\r\n', token


def install(core):
    scanner = LiveScanner(core)
    app = core['app']

    @app.post('/api/camera/scan-sessions')
    def start(body: StartScan):
        return scanner.start(body.operator)

    @app.get('/api/camera/scan-sessions/{session_id}')
    def status(session_id: uuid.UUID):
        return scanner.read(session_id)

    @app.delete('/api/camera/scan-sessions/{session_id}')
    def stop(session_id: uuid.UUID):
        with scanner.lock:
            scanner.read(session_id)  # Validate the target before changing automatic mode.
            runner = core.get('automatic_runner')
            if runner and runner.settings()['enabled']:
                runner.pause()
            return scanner.read(session_id, cancel=True)

    @app.get('/api/camera/preview.mjpg')
    async def preview(request: Request):
        camera = scanner.camera()

        async def frames():
            previous = None
            while not await request.is_disconnected():
                started = time.monotonic()
                part, previous = await run_in_threadpool(preview_part, camera, previous)
                if part:
                    yield part
                # Select the latest frame after each send; slow clients cannot
                # accumulate an application queue. Encoding time is in the budget.
                await asyncio.sleep(max(0, 1 / 30 - (time.monotonic() - started)))

        return StreamingResponse(frames(), media_type='multipart/x-mixed-replace; boundary=frame',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    if core['receiver_camera']:
        core['receiver_camera'].on_service_status = scanner.observe_service
    return scanner
