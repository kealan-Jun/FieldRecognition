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

    def active_binding(self):
        with self.core['db']() as conn:
            row = conn.execute('SELECT document FROM bindings WHERE camera=? AND ended IS NULL',
                               (self.camera().target,)).fetchone()
        return json.loads(row['document']) if row else None

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
                            'message': '连续扫码中：先对准场景码，再对准仪器码',
                            'scan': None, 'binding': None}
            if owner == 'automation':
                self.session['after_frame_id'] = camera.snapshot().get('decoded_frames', 0)
            self.last_seen = time.monotonic()
            existing = self.active_binding()
            if existing:
                self.session.update(status='bound', binding=existing,
                                    message='当前采集服务已有绑定，继续沿用；不会重复绑定')
            else:
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
                existing = self.active_binding()
                if existing:
                    self.session.update(status='bound', binding=existing, message='已有有效绑定，继续沿用')
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
                    # Slow decoding must not commit an old frame as a new observation.
                    if time.monotonic() - acquired + metadata.get('frame_age_ms', 0) / 1000 > 1:
                        continue
                    if not (matches['matches'] or matches['scene_matches']):
                        if matches['unknown']:
                            self.session['message'] = '读到了未登记二维码，请对准场景或仪器码'
                        continue
                    visits = self.core['scene_visits']()
                    visit = next((v for v in visits if v['camera_id'] == self.camera().target), None)
                    if (not matches['matches'] and not matches['unknown'] and len(matches['scene_matches']) == 1
                            and visit and visit['scene']['id'] == matches['scene_matches'][0]['id']):
                        self.session.update(scene_visit=visit, message=f'已进入 {visit["scene"]["name"]}，请对准仪器二维码')
                        continue
                    key = (tuple(m['id'] for m in matches['matches']),
                           tuple(s['id'] for s in matches['scene_matches']), visit['visit_id'] if visit else None)
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
        result = self.core['save_camera_scan'](frame, metadata, decoded=decoded)
        self.session['scan'] = result
        instruments, scenes = matches['matches'], matches['scene_matches']
        if len(instruments) > 1 or len(scenes) > 1 or matches['unknown']:
            self.session.update(status='needs_selection', message='画面有多个候选或未登记码，已暂停；请核对并手动选择')
            return
        if instruments and scenes and instruments[0]['scene'] != scenes[0]['name']:
            self.session.update(status='needs_selection', message='场景码与仪器所属场景不一致，已暂停，请核对')
            return
        try:
            if scenes:
                visit = self.core['enter_scene'](self.core['SceneEntry'](scan_id=result['scan_id'], scene_id=scenes[0]['id']), automatic=True)
                self.session.update(scene_visit=visit, message=f'已进入 {visit["scene"]["name"]}，请对准仪器二维码')
            if instruments:
                binding = self.core['bind'](self.core['BindingRequest'](
                    scan_id=result['scan_id'], instrument_id=instruments[0]['id'], operator=self.session['operator']), automatic=True)
                self.session.update(status='bound', binding=binding,
                                    message=f'已绑定 {binding["instrument"]["name"]}；采集服务运行期间持续有效，扫码已停止')
        except HTTPException as exc:
            self.session['message'] = str(exc.detail)

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
                transition = (previous is not None and previous['online'] != observation['online']) or (previous is None and not observation['online'])
                if changed or transition:
                    reason = 'device_media_session_changed' if changed else 'device_service_online' if observation['online'] else 'device_service_offline'
                    for table in ('bindings', 'scene_visits'):
                        for record in conn.execute(f'SELECT * FROM {table} WHERE camera=? AND ended IS NULL', (camera.target,)).fetchall():
                            doc = json.loads(record['document']) | {'ended_at': timestamp, 'end_reason': reason,
                                                                   'service_observation': observation}
                            conn.execute(f'UPDATE {table} SET ended=?,document=? WHERE id=?', (timestamp, json.dumps(doc), record['id']))
                    conn.execute('INSERT OR REPLACE INTO camera_resets VALUES(?,?)', (camera.target, timestamp))
                    if self.session and self.session['status'] in ACTIVE | {'bound'}:
                        self.session.update(status='stopped', binding=None,
                                            message='设备采集服务已离线或会话已变化，重新启动后请重新扫码绑定')
                # Preserve a known session marker through an offline status with a zero marker.
                stored = observation | {'media_session_id': new_id or old_id}
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
                part, previous = await run_in_threadpool(preview_part, camera, previous)
                if part:
                    yield part
                await asyncio.sleep(1 / 15)

        return StreamingResponse(frames(), media_type='multipart/x-mixed-replace; boundary=frame',
                                 headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

    if core['receiver_camera']:
        core['receiver_camera'].on_service_status = scanner.observe_service
    return scanner
