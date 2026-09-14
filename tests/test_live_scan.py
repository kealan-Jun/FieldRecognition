import json
import threading
import time

import cv2
import numpy as np
import pytest

from live_scan import LEASE_SECONDS, LiveScanner, preview_part
from test_demo import app_client, enter_scene, register, scan  # noqa: F401


class Camera:
    target = 'TestCamera'

    def __init__(self):
        self.lock = threading.Lock()
        self.sequence = 0
        self.pixels = None
        self.offline = False

    def publish(self, pixels):
        with self.lock:
            self.sequence += 1
            self.pixels = pixels
            self.offline = False

    def frame(self):
        with self.lock:
            if self.pixels is None or self.offline:
                raise ValueError('offline or stale')
            return self.pixels.copy(), {'decoded_frame_id': self.sequence, 'sequence': self.sequence,
                                        'timestamp_us': 123000 + self.sequence,
                                        'frame_age_ms': 0, 'sender_id': 'test', 'camera_id': 'cam01'}

    def snapshot(self):
        return {'id': self.target, 'configured': True, 'mode': 'gwhp_main', 'status': 'streaming'}

    def close(self):
        pass


@pytest.fixture
def live(app_client, monkeypatch):
    app, client = app_client
    camera = Camera()
    monkeypatch.setattr(app, 'receiver_camera', camera)
    return app, client, camera


def wait_for(client, sid, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get('/api/camera/scan-sessions/' + sid).json()
        if predicate(result):
            return result
        time.sleep(.02)
    raise AssertionError(result)


def label(app, name):
    return cv2.imread(str(app.BASE / 'static' / 'labels' / (name + '.png')))


def start(client):
    response = client.post('/api/camera/scan-sessions', json={'operator': '测试员'})
    assert response.status_code == 200
    return response.json()['session_id']


def test_live_blank_scene_then_single_hit_binds_once_with_original_evidence(live):
    app, client, camera = live
    aid = 'e9434a0a-3319-414a-b988-4cc6884edce4'
    client.put('/api/instruments/' + aid, json={'name': '称量仪器 A', 'scene': '湿实验实验台'})
    camera.publish(np.full((300, 400, 3), 255, np.uint8))
    sid = start(client)
    wait_for(client, sid, lambda r: r['frames_scanned'] == 1)
    assert client.post('/api/camera/scan-sessions', json={'operator': '测试员'}).status_code == 409
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM scans').fetchone()[0] == 0
    camera.publish(label(app, 'Scene01'))
    wait_for(client, sid, lambda r: 'scene_visit' in r)
    camera.publish(label(app, 'InstrumentA'))
    result = wait_for(client, sid, lambda r: bool(r.get('bindings')))
    binding = result['binding']
    assert app.ocr_warmup_future.result(timeout=2)
    assert client.get('/api/state').json()['ocr']['resident']
    assert binding['instrument']['id'] == aid
    assert binding['identity_basis'] == 'unsigned_qr_and_continuous_scan_opt_in'
    assert result['scan']['frame_metadata']['sequence'] == 3
    assert result['scan']['qr_diagnostics']['identity_inferred'] is False
    image = client.get(result['scan']['image_url'])
    pixels = cv2.imdecode(np.frombuffer(image.content, np.uint8), cv2.IMREAD_COLOR)
    assert np.array_equal(pixels, label(app, 'InstrumentA'))
    # The next frame has no QR: a single decoded hit was enough, and only one bind exists.
    camera.publish(np.zeros((300, 400, 3), np.uint8))
    assert client.post('/api/camera/scan-sessions', json={'operator': '测试员'}).status_code == 409
    wait_for(client, sid, lambda r: r['frames_scanned'] >= 4)
    body = {'operator': '测试员', 'instrument_id': aid, 'scan_id': result['scan']['scan_id']}
    assert client.post('/api/bindings', json=body).json()['binding_id'] == binding['binding_id']
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM bindings').fetchone()[0] == 1


def test_live_unregistered_instrument_keeps_scanning(live):
    app, client, camera = live
    camera.publish(label(app, 'InstrumentA'))
    sid = start(client)
    result = wait_for(client, sid, lambda r: r['scan'] is not None)
    assert result['status'] == 'scanning'
    assert not client.get('/api/state').json()['bindings']
    count = result['frames_scanned']
    time.sleep(.05)
    assert client.get('/api/camera/scan-sessions/' + sid).json()['frames_scanned'] == count
    assert client.delete('/api/camera/scan-sessions/' + sid).json()['status'] == 'stopped'


def test_multiple_unconfigured_instruments_keep_scanning_without_binding(live):
    app, client, camera = live
    frame = np.full((500, 1000, 3), 255, np.uint8)
    for x, name in [(50, 'InstrumentA'), (550, 'InstrumentB')]:
        frame[50:450, x:x+400] = cv2.resize(label(app, name), (400, 400))
    camera.publish(frame)
    result = wait_for(client, start(client), lambda r: bool(r.get('binding_errors')))
    assert len(result['scan']['matches']) == 2
    assert not client.get('/api/state').json()['bindings']


def test_cancel_during_decode_prevents_late_save(live, monkeypatch):
    import live_scan
    app, client, camera = live
    started, release = threading.Event(), threading.Event()
    decode = live_scan.decode_qr

    def delayed(frame, **kwargs):
        started.set()
        assert release.wait(3)
        return decode(frame, **kwargs)

    monkeypatch.setattr(live_scan, 'decode_qr', delayed)
    camera.publish(label(app, 'InstrumentA'))
    sid = start(client)
    try:
        assert started.wait(2)
        assert client.delete('/api/camera/scan-sessions/' + sid).json()['status'] == 'stopped'
    finally:
        release.set()
        app.live_scanner.worker.join(3)
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM scans').fetchone()[0] == 0


def test_unwatched_session_expires_and_offline_preview_replaces_old_image(live):
    app, client, camera = live
    sid = start(client)
    wait_for(client, sid, lambda r: r['status'] == 'waiting_camera')
    with app.live_scanner.lock:
        app.live_scanner.last_seen = time.monotonic() - LEASE_SECONDS - 1
    app.live_scanner.worker.join(2)
    assert client.get('/api/camera/scan-sessions/' + sid).json()['status'] == 'stopped'
    camera.publish(label(app, 'Scene01'))
    part, token = preview_part(camera, None)
    assert b'Content-Type: image/jpeg' in part
    assert preview_part(camera, token)[0] is None
    camera.offline = True
    offline_part, offline_token = preview_part(camera, token)
    assert offline_part != part and offline_token == 'offline'


def test_service_session_lifetime_survives_app_recreation_then_invalidates_evidence(live):
    app, client, camera = live
    scanner = app.live_scanner
    scanner.observe_service({'online': True, 'media_session_id': 13})
    picture = scan(client)
    aid = picture['matches'][0]['id']
    register(client, aid)
    args = {'scan_id': picture['scan_id'], 'instrument_id': aid, 'operator': '测试员'}
    bound = client.post('/api/bindings', json=args).json()
    # Recreating the local manager or polling the same upstream session preserves binding.
    recreated = LiveScanner(vars(app))
    recreated.observe_service({'online': True, 'media_session_id': 13})
    assert client.post('/api/bindings', json=args).json()['binding_id'] == bound['binding_id']
    # Another camera's binding must survive this camera's service restart.
    other = scan(client, camera='OtherCamera')
    enter_scene(client, camera='OtherCamera')
    other_bound = client.post('/api/bindings', json=args | {'scan_id': other['scan_id']}).json()
    recreated.observe_service({'online': False, 'media_session_id': 13})
    preserved=next(b for b in client.get('/api/state').json()['bindings'] if b['binding_id']==bound['binding_id'])
    assert not preserved['ended_at']
    assert not app.current_readout_binding(preserved)  # Suspended, not unbound.
    recreated.observe_service({'online': True, 'media_session_id': 13})
    assert app.current_readout_binding(preserved)
    assert client.post('/api/bindings',json=args).json()['binding_id']==bound['binding_id']
    recreated.observe_service({'online': False, 'media_session_id': 0})
    recreated=LiveScanner(vars(app))  # Persists across a local service restart.
    recreated.observe_service({'online': True, 'media_session_id': 13})
    assert app.current_readout_binding(preserved)
    recreated.observe_service({'online': True, 'media_session_id': 14})
    bindings = client.get('/api/state').json()['bindings']
    assert next(b for b in bindings if b['binding_id'] == bound['binding_id'])['ended_at']
    assert not next(b for b in bindings if b['binding_id'] == other_bound['binding_id'])['ended_at']
    assert client.post('/api/bindings', json=args).status_code == 409
    assert client.post('/api/ocr', json={'binding_id': bound['binding_id'], 'capture_id': picture['capture_id']}).status_code == 409
    register(client, aid)
    fresh = scan(client)
    new = client.post('/api/bindings', json=args | {'scan_id': fresh['scan_id']}).json()
    assert new['binding_id'] != bound['binding_id']
    # A fast service restart between polls is detected from a new ingress session too.
    recreated.observe_service({'online': True, 'media_session_id': 15})
    assert client.post('/api/bindings', json=args | {'scan_id': fresh['scan_id']}).status_code == 409


def test_live_requires_operator_and_main_stream(app_client):
    _, client = app_client
    assert client.post('/api/camera/scan-sessions', json={'operator': ' '}).status_code == 422
    assert client.post('/api/camera/scan-sessions', json={'operator': 'Tester'}).status_code == 409
    assert client.get('/api/camera/preview.mjpg').status_code == 409


def test_single_instrument_video_frame_binds_without_scene(live):
    app, client, camera = live
    aid = 'e9434a0a-3319-414a-b988-4cc6884edce4'
    client.put('/api/instruments/' + aid, json={'name': '称量仪器 A', 'scene': '湿实验实验台'})
    sid = start(client)
    camera.publish(label(app, 'InstrumentA'))
    result = wait_for(client, sid, lambda r: bool(r.get('bindings')))
    assert result['binding']['instrument']['id'] == aid
    assert result['binding']['scene_visit_id'] is None
    assert result['binding']['scene_qr_verified'] is False
    assert result['scan']['operator'] == '测试员'
    assert result['scan']['scan_session_id'] == sid
    app.live_scanner.session = None
    assert client.get('/api/state').json()['last_camera_scan']['scan_id'] == result['scan']['scan_id']
    assert not client.get('/api/state').json()['scene_visits']
    assert app.ocr_warmup_future.result(timeout=2)
    with app.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM bindings').fetchone()[0] == 1


def test_fresh_frame_clears_waiting_camera_message_without_saving_blank_frames(live):
    app, client, camera = live
    sid = start(client)
    wait_for(client, sid, lambda r: r['status'] == 'waiting_camera')
    camera.publish(np.full((240, 320, 3), 255, np.uint8))
    result = wait_for(client, sid, lambda r: r['frames_scanned'] == 1)
    assert result['status'] == 'scanning'
    assert '等待相机新画面' not in result['message']
    with app.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 0
