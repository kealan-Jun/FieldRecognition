import threading
import time

import cv2
import numpy as np
import pytest

from automation import AutomaticRunner
from live_scan import LEASE_SECONDS
from test_live_scan import live, label  # noqa: F401
from test_demo import app_client, enter_scene, register, scan  # noqa: F401


@pytest.fixture
def automatic(live, monkeypatch):
    app, client, camera = live
    runner = app.automatic_runner
    # Drive supervisor ticks deterministically; QR decoding still uses its real worker.
    runner.close()
    runner.stop.clear()
    info = {'service_status_available': True, 'service_status': {'online': True, 'media_session_id': 31}}
    monkeypatch.setattr(camera, 'snapshot', lambda: info | {'id': camera.target, 'configured': True,
                        'mode': 'gwhp_main', 'status': 'streaming', 'decoded_frames': camera.sequence})
    app.live_scanner.observe_service(info['service_status'])
    client.put('/api/instruments/e9434a0a-3319-414a-b988-4cc6884edce4',
               json={'name': '自动化测试仪器', 'scene': '湿实验实验台'})
    yield app, client, camera, runner, info


def wait(runner, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        runner.step()
        state = runner.snapshot()
        if predicate(state):
            return state
        time.sleep(.02)
    raise AssertionError(state)


def enable(client):
    response = client.put('/api/automation', json={'enabled': True, 'operator': '登记人员 001'})
    assert response.status_code == 200
    return response.json()


def bind_from_video(app, client, camera, runner):
    enable(client)
    camera.publish(label(app, 'Scene01'))
    wait(runner, lambda s: s['session'] and 'scene_visit' in s['session'])
    camera.publish(label(app, 'InstrumentA'))
    return wait(runner, lambda s: bool(s.get('binding_ids')))['binding_ids'][0]


def test_first_use_waits_for_human_registration(automatic, monkeypatch):
    app, client, camera, runner, info = automatic
    monkeypatch.setenv('FIELD_AUTO_RUN_ENABLED', '1')
    camera.publish(label(app, 'InstrumentA'))
    runner.step()
    state = client.get('/api/state').json()
    assert state['automation']['status'] == 'waiting_operator'
    assert state['automation']['operator'] == ''
    assert state['automation']['session'] is None
    assert not state['bindings']
    assert client.put('/api/automation', json={'enabled': True, 'operator': '   '}).status_code == 422
    assert client.put('/api/automation', json={'enabled': True, 'operator': 'x' * 81}).status_code == 422


def test_unattended_single_frame_binding_and_persistent_registration(automatic):
    app, client, camera, runner, info = automatic
    enable(client)
    scanner = app.live_scanner
    with scanner.lock:
        scanner.last_seen = time.monotonic() - LEASE_SECONDS - 5
    camera.publish(label(app, 'Scene01'))
    wait(runner, lambda s: s['session'] and 'scene_visit' in s['session'])
    assert scanner.session['owner'] == 'automation'
    camera.publish(label(app, 'InstrumentA'))
    bound = wait(runner, lambda s: bool(s.get('binding_ids')))
    assert app.ocr_warmup_future.result(timeout=2)
    assert client.get('/api/state').json()['ocr']['resident']
    # No scan-session GET was used: state/registration reads do not renew a browser lease.
    recovered = AutomaticRunner(vars(app))
    recovered.step()
    assert recovered.snapshot()['binding_ids'][0] == bound['binding_ids'][0]
    assert recovered.snapshot()['operator'] == '登记人员 001'
    assert recovered.snapshot()['registered_at']
    for _ in range(3):
        runner.step()
    with app.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM bindings').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_device_restart_rearms_and_local_restart_preserves_pause(automatic):
    app, client, camera, runner, info = automatic
    binding_id = bind_from_video(app, client, camera, runner)
    sid = runner.snapshot()['session']['session_id']
    info['service_status'] = {'online': False, 'media_session_id': 31}
    app.live_scanner.observe_service(info['service_status'])
    runner.step()
    assert runner.snapshot()['status'] == 'waiting_camera'
    assert client.get('/api/state').json()['bindings'][0]['ended_at'] is None
    info['service_status'] = {'online': True, 'media_session_id': 32}
    app.live_scanner.observe_service(info['service_status'])
    runner.step()
    assert runner.snapshot()['session']['session_id'] != sid
    assert runner.settings()['enabled']
    camera.publish(label(app, 'Scene01'))
    wait(runner, lambda s: s['session'] and 'scene_visit' in s['session'])
    camera.publish(label(app, 'InstrumentA'))
    rebound = wait(runner, lambda s: bool(s.get('binding_ids')))['binding_ids'][0]
    assert rebound != binding_id
    assert client.post('/api/bindings/' + rebound + '/end').status_code == 200
    assert runner.snapshot()['pause_reason'] == 'binding_ended'
    recovered = AutomaticRunner(vars(app))
    recovered.step()
    assert not recovered.snapshot()['enabled']
    assert recovered.snapshot()['session']['status'] not in {'scanning', 'waiting_camera'}
    enable(client)
    assert runner.snapshot()['session']['status'] in {'scanning', 'waiting_camera'}


def test_status_unknown_blocks_binding_and_does_not_end_existing_binding(automatic):
    app, client, camera, runner, info = automatic
    info['service_status_available'] = False
    enable(client)
    camera.publish(label(app, 'Scene01'))
    runner.step()
    assert runner.snapshot()['status'] == 'waiting_camera'
    assert runner.snapshot()['session'] is None
    info['service_status_available'] = True
    binding_id = bind_from_video(app, client, camera, runner)
    info['service_status_available'] = False
    runner.step()
    bindings = client.get('/api/state').json()['bindings']
    assert next(b for b in bindings if b['binding_id'] == binding_id)['ended_at'] is None
    info['service_status_available'] = True
    runner.step()
    assert runner.snapshot()['binding_ids'][0] == binding_id


def test_multi_code_binds_independently_and_keeps_scanning(automatic):
    app, client, camera, runner, info = automatic
    client.put('/api/instruments/eae17924-9fa7-4445-ac45-3987f5687be9',
               json={'name': '仪器 B', 'scene': '湿实验实验台'})
    enable(client)
    pixels = np.full((500, 1500, 3), 255, np.uint8)
    for x, name in [(50, 'InstrumentA'), (550, 'InstrumentB'), (1050, 'Scene01')]:
        pixels[50:450, x:x+400] = cv2.resize(label(app, name), (400, 400))
    camera.publish(pixels)
    result = wait(runner, lambda s: len(s.get('binding_ids', [])) == 2 and len(s['session']['scene_visits']) == 1)
    assert result['enabled'] and result['status'] == 'scanning'
    assert len(result['session']['scan']['matches']) == 2
    assert len(result['session']['scan']['scene_matches']) == 1
    camera.publish(pixels)
    wait(runner, lambda s: s['session']['frames_scanned'] >= 2)
    with app.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM bindings').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 1
    app.live_scanner.observe_service({'online': False, 'media_session_id': 31})
    assert len([b for b in client.get('/api/state').json()['bindings'] if not b['ended_at']])==2
    app.live_scanner.observe_service({'online': True, 'media_session_id': 31})
    assert len([b for b in client.get('/api/state').json()['bindings'] if not b['ended_at']])==2
    assert len(client.get('/api/state').json()['scene_visits'])==1
    app.live_scanner.observe_service({'online': True, 'media_session_id': 32})
    assert not [b for b in client.get('/api/state').json()['bindings'] if not b['ended_at']]
    assert not client.get('/api/state').json()['scene_visits']


def test_pause_during_decode_prevents_late_binding(automatic, monkeypatch):
    import live_scan
    app, client, camera, runner, info = automatic
    started, release = threading.Event(), threading.Event()
    original = live_scan.decode_qr

    def delayed(frame, **kwargs):
        started.set()
        assert release.wait(3)
        return original(frame, **kwargs)

    monkeypatch.setattr(live_scan, 'decode_qr', delayed)
    enable(client)
    camera.publish(label(app, 'Scene01'))
    try:
        assert started.wait(2)
        assert client.put('/api/automation', json={'enabled': False}).status_code == 200
    finally:
        release.set()
        app.live_scanner.worker.join(3)
    with app.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 0
    assert not runner.settings()['enabled']


def test_operator_change_requires_ending_current_binding(automatic):
    app, client, camera, runner, info = automatic
    binding_id = bind_from_video(app, client, camera, runner)
    assert client.put('/api/automation', json={'enabled': True, 'operator': '登记人员 002'}).status_code == 409
    assert client.post('/api/camera/relations/end', json={'camera_id': runner.target()}).status_code == 200
    assert client.put('/api/automation', json={'enabled': True, 'operator': '登记人员 002'}).status_code == 200
    history = client.get('/api/state').json()['bindings']
    assert history[0]['operator'] == '登记人员 001'
    assert runner.settings()['operator'] == '登记人员 002'


def test_pause_preserves_manual_binding_without_inventing_registration(automatic):
    app, client, camera, runner, info = automatic
    photo = scan(client)
    register(client, photo['matches'][0]['id'])
    response = client.post('/api/bindings', json={'scan_id': photo['scan_id'],
                           'instrument_id': photo['matches'][0]['id'], 'operator': '手动登记人员'})
    assert response.status_code == 200
    assert client.put('/api/automation', json={'enabled': False}).status_code == 200
    assert runner.settings()['operator'] == ''
    assert not client.get('/api/state').json()['bindings'][0]['ended_at']
