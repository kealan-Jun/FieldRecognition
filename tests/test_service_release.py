"""Device/service loss releases ownership without any browser handoff decisions."""
import io
import json
import time
import uuid

import numpy as np
import pytest

from live_scan import LiveScanner
from receiver import ReceiverCamera
from test_demo import app_client, enter_scene, scan  # noqa: F401
from test_live_scan import Camera, live, label, start, wait_for  # noqa: F401
from test_instrument_ownership import own
from test_archive_store import archive  # noqa: F401


@pytest.mark.parametrize('failure', [
    {'online': False},
    {'online': False, 'failure_reason': 'receiver_status_unavailable'},
    {'online': False, 'failure_reason': 'camera_stream_error'},
    {'online': True, 'media_session_id': 2},
])
def test_blocked_second_camera_binds_automatically_after_source_failure(live, failure):
    app, client, target = live
    source = Camera(); source.target = 'SourceCamera'
    source_scanner = LiveScanner(vars(app) | {'receiver_camera': source})
    source_scanner.observe_service({'online': True, 'media_session_id': 1})
    old = own(client, 'InstrumentA', source.target, '甲')
    retained = own(client, 'InstrumentB', target.target, '乙')
    source_scene = enter_scene(client, camera=source.target).json()
    scene_scan = scan(client, 'Scene01', target.target)
    target_scene = client.post('/api/scene/enter', json={'scan_id': scene_scan['scan_id'],
        'scene_id': scene_scan['scene_matches'][0]['id'], 'operator': '乙'}).json()
    target.publish(label(app, 'InstrumentA'))
    sid = client.post('/api/camera/scan-sessions', json={'operator': '乙'}).json()['session_id']
    blocked = wait_for(client, sid, lambda r: bool(r.get('binding_errors')))
    assert len(blocked['bindings']) == 1 and '占用' in blocked['message']
    source_scanner.observe_service(failure)
    target.publish(label(app, 'InstrumentA'))
    accepted = wait_for(client, sid, lambda r: len(r.get('bindings', [])) == 2)
    assert not accepted['binding_errors']
    assert retained['binding_id'] in {b['binding_id'] for b in accepted['bindings']}
    assert old['binding_id'] not in {b['binding_id'] for b in accepted['bindings']}
    state = client.get('/api/state').json()
    assert [v['visit_id'] for v in state['scene_visits']] == [target_scene['visit_id']]
    with app.db() as conn:
        assert conn.execute('SELECT ended FROM scene_visits WHERE id=?', (source_scene['visit_id'],)).fetchone()[0]
        assert conn.execute('SELECT count(*) FROM binding_handoffs').fetchone()[0] == 0
    assert client.get(old['image_url']).status_code == 200


def test_recovery_rejects_offline_scans_and_repeated_offline_is_idempotent(live):
    app, client, camera = live
    scanner = app.live_scanner
    scanner.observe_service({'online': True, 'media_session_id': 1})
    old = own(client, 'InstrumentA', camera.target, '甲')
    scanner.observe_service({'online': False})
    with app.db() as conn:
        count = conn.execute("SELECT count(*) FROM archive_outbox WHERE entity='bindings'").fetchone()[0]
        cutoff = conn.execute('SELECT confirmed_at FROM camera_resets WHERE camera=?', (camera.target,)).fetchone()[0]
    scanner.observe_service({'online': False})
    during = scan(client, 'InstrumentA', camera.target)
    with app.db() as conn:
        assert conn.execute("SELECT count(*) FROM archive_outbox WHERE entity='bindings'").fetchone()[0] == count
        assert conn.execute('SELECT confirmed_at FROM camera_resets WHERE camera=?', (camera.target,)).fetchone()[0] == cutoff
    scanner.observe_service({'online': True, 'media_session_id': 1})
    for stale in (old['scan_id'], during['scan_id']):
        assert client.post('/api/bindings', json={'scan_id': stale,
            'instrument_id': old['instrument']['id'], 'operator': '甲'}).status_code == 409
    fresh = scan(client, 'InstrumentA', camera.target)
    rebound = client.post('/api/bindings', json={'scan_id': fresh['scan_id'],
        'instrument_id': old['instrument']['id'], 'operator': '甲'})
    assert rebound.status_code == 200 and rebound.json()['binding_id'] != old['binding_id']


def test_failed_camera_cannot_leave_a_manual_transfer_reservation(live):
    app, client, camera = live
    old = own(client, 'InstrumentA', camera.target, '甲')
    target = scan(client, 'InstrumentA', 'NextCamera')
    transfer = client.post('/api/handoffs', json={'request_id': str(uuid.uuid4()), 'binding_id': old['binding_id'],
        'recipient_scan_id': target['scan_id'], 'recipient_operator': '乙'}).json()
    client.post(f"/api/handoffs/{transfer['handoff_id']}/release", json={
        'actor': '甲', 'camera_id': camera.target, 'revision': transfer['revision']})
    app.live_scanner.observe_service({'online': False})
    assert client.get('/api/handoffs').json()['items'][0]['status'] == 'expired'
    third = scan(client, 'InstrumentA', 'ThirdCamera')
    assert client.post('/api/bindings', json={'scan_id': third['scan_id'],
        'instrument_id': old['instrument']['id'], 'operator': '丙'}).status_code == 200


@pytest.mark.parametrize('failure', ['capture_service_exited', 'capture_service_unresponsive'])
def test_supervisor_releases_only_failed_camera_and_archives_end(archive, failure):
    from camera_supervisor import release_camera
    app, client, root = archive
    old = own(client, 'InstrumentA', 'FailedCamera', '甲')
    retained = own(client, 'InstrumentB', 'OtherCamera', '乙')
    visit = enter_scene(client, camera='FailedCamera').json()
    app.archive_store.step(100)
    release_camera(app.database, 'FailedCamera', failure)
    app.archive_store.step(100)
    docs = [json.loads(p.read_text()) for p in root.glob('*/Bindings/*/Binding.json')]
    saved = next(d for d in docs if d['binding'].get('binding_id') == old['binding_id'])
    assert saved['status'] == 'ended' and saved['binding']['end_reason'] == failure
    assert saved['binding']['started_at'] == old['started_at']
    assert next(d for d in docs if d['binding'].get('visit_id') == visit['visit_id'])['status'] == 'ended'
    assert next(d for d in docs if d['binding'].get('binding_id') == retained['binding_id'])['status'] == 'active'
    assert len(saved['receipt_versions']) >= 3  # scan, start, end remain linked


@pytest.mark.parametrize('document,reason', [
    ({'cameras': []}, 'camera_not_found'),
    ({'receiver_admin_stale': True}, 'receiver_status_unavailable'),
    ({'cameras': [{'camera_key': 'TestCamera', 'status_live': True, 'media_live': False}]}, 'camera_service_offline'),
    (None, 'receiver_status_unavailable'),
])
def test_receiver_failure_releases_real_scanner_session(live, monkeypatch, document, reason):
    import urllib.request
    app, client, _ = live
    old = own(client, 'InstrumentA', 'TestCamera', '甲')
    receiver = ReceiverCamera('http://unused.invalid', 'TestCamera')
    receiver.on_service_status = app.live_scanner.observe_service

    class Opener:
        def open(self, *args, **kwargs):
            receiver.stop.set()
            if document is None:
                raise TimeoutError('receiver unavailable')
            return io.BytesIO(json.dumps(document).encode())

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: Opener())
    receiver._status_loop()
    ended = next(b for b in client.get('/api/state').json()['bindings'] if b['binding_id'] == old['binding_id'])
    assert ended['ended_at'] and ended['end_reason'] == reason


def test_healthy_poll_preserves_frame_but_failure_and_recovery_discard_it():
    receiver = ReceiverCamera('http://unused.invalid', 'TestCamera')
    healthy = {'online': True, 'media_session_id': 1}
    receiver._observe_service(healthy)
    receiver.latest = (np.zeros((2, 2, 3), np.uint8), {}, time.monotonic())
    receiver.on_service_status = lambda _: receiver.frame()  # Healthy callbacks cannot transiently go offline.
    receiver._observe_service(healthy)
    receiver.on_service_status = None
    receiver._observe_service({'online': False})
    with pytest.raises(ValueError): receiver.frame()
    receiver._observe_service(healthy)
    with pytest.raises(ValueError): receiver.frame()


def test_delayed_status_cannot_restore_failed_session_or_end_a_fresh_binding(live):
    app, client, camera = live
    scanner = app.live_scanner
    initial = app.now()
    scanner.observe_service({'online': True, 'observed_at': initial})
    old = own(client, 'InstrumentA', camera.target, '甲')
    failed = app.now()
    scanner.observe_service({'online': False, 'observed_at': failed})
    scanner.observe_service({'online': True, 'observed_at': initial})
    with app.db() as conn:
        assert not json.loads(conn.execute('SELECT document FROM camera_service_state WHERE camera=?', (camera.target,)).fetchone()[0])['online']
    scanner.observe_service({'online': True, 'observed_at': app.now()})
    new = own(client, 'InstrumentA', camera.target, '甲')
    scanner.observe_service({'online': False, 'observed_at': failed})
    assert app.current_readout_binding(new)
    assert not app.current_readout_binding(old)


def test_stalled_frames_release_binding_even_if_receiver_claims_online(live, monkeypatch):
    import urllib.request
    app, client, _ = live
    old = own(client, 'InstrumentA', 'TestCamera', '甲')
    receiver = ReceiverCamera('http://unused.invalid', 'TestCamera')
    receiver.on_service_status = app.live_scanner.observe_service
    receiver.latest = (np.zeros((2, 2, 3), np.uint8), {}, time.monotonic() - 2)

    class Opener:
        def open(self, *args, **kwargs):
            receiver.stop.set()
            return io.BytesIO(json.dumps({'cameras': [{'camera_key': 'TestCamera',
                'status_live': True, 'media_live': True}]}).encode())

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: Opener())
    receiver._status_loop()
    ended = next(b for b in client.get('/api/state').json()['bindings'] if b['binding_id'] == old['binding_id'])
    assert ended['ended_at'] and ended['end_reason'] == 'camera_frames_stale'
    assert receiver.latest is None
