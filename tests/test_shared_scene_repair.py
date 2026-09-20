"""A decoded scene is shared location evidence; camera and instrument ownership stay scoped."""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from live_scan import LiveScanner
from qr_decode import decode_qr
from test_demo import app_client, scan  # noqa: F401
from test_live_scan import Camera, label
from test_production_regressions import managed  # noqa: F401


def scene_body(client, camera_id, operator):
    picture = scan(client, 'Scene01', camera=camera_id)
    assert picture['qr_diagnostics']['identity_inferred'] is False
    assert len(picture['scene_matches']) == 1 and not picture['matches']
    return {'scan_id': picture['scan_id'], 'scene_id': picture['scene_matches'][0]['id'], 'operator': operator}


def active_visits(app):
    with app.db() as conn:
        return [json.loads(row[0]) for row in conn.execute('SELECT document FROM scene_visits WHERE ended IS NULL')]


def test_two_cameras_enter_same_decoded_scene_idempotently_and_end_independently(app_client):
    app, client = app_client
    first_body = scene_body(client, 'CameraA', '甲')
    second_body = scene_body(client, 'CameraB', '乙')
    assert first_body['scene_id'] == second_body['scene_id']
    first = client.post('/api/scene/enter', json=first_body)
    second = client.post('/api/scene/enter', json=second_body)
    assert first.status_code == second.status_code == 200
    first, second = first.json(), second.json()
    assert first['visit_id'] != second['visit_id']
    assert first['operator'] == '甲' and second['operator'] == '乙'
    assert first['camera_id'] == 'CameraA' and second['camera_id'] == 'CameraB'
    assert first['scene']['id'] == second['scene']['id'] == first_body['scene_id']
    assert client.post('/api/scene/enter', json=first_body).json()['visit_id'] == first['visit_id']
    assert client.post('/api/scene/enter', json=second_body).json()['visit_id'] == second['visit_id']
    assert len(active_visits(app)) == 2
    ended = client.post('/api/camera/relations/end', json={'camera_id': 'CameraA'})
    assert ended.status_code == 200
    assert first['visit_id'] in ended.json()['ended_ids']
    assert second['visit_id'] not in ended.json()['ended_ids']
    assert [visit['visit_id'] for visit in active_visits(app)] == [second['visit_id']]
    with app.db() as conn:
        old = json.loads(conn.execute('SELECT document FROM scene_visits WHERE id=?', (first['visit_id'],)).fetchone()[0])
    assert old['operator'] == '甲' and old['ended_at']
    assert client.post('/api/scene/enter', json=second_body).json()['visit_id'] == second['visit_id']


def concurrent_entries(app, bodies):
    ready = threading.Barrier(len(bodies))

    def enter(body):
        ready.wait(timeout=5)
        return app.enter_scene(app.SceneEntry(**body))

    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        futures = [pool.submit(enter, body) for body in bodies]
        return [future.result(timeout=15) for future in futures]


def test_real_concurrent_transactions_allow_two_cameras_in_one_scene(app_client):
    app, client = app_client
    bodies = [scene_body(client, 'CameraA', '甲'), scene_body(client, 'CameraB', '乙')]
    assert bodies[0]['scene_id'] == bodies[1]['scene_id']
    results = concurrent_entries(app, bodies)
    assert len({result['visit_id'] for result in results}) == 2
    assert {result['camera_id'] for result in results} == {'CameraA', 'CameraB'}
    assert {result['operator'] for result in results} == {'甲', '乙'}
    assert len(active_visits(app)) == 2


def test_concurrent_repeated_entry_of_same_camera_creates_one_visit(app_client):
    app, client = app_client
    body = scene_body(client, 'CameraA', '甲')
    results = concurrent_entries(app, [body, body])
    assert len({result['visit_id'] for result in results}) == 1
    assert len(active_visits(app)) == 1
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM scene_visits').fetchone()[0] == 1


def test_mixed_frame_enters_shared_scene_while_occupied_instrument_remains_exclusive(app_client, monkeypatch):
    app, client = app_client
    first_scene = scene_body(client, 'CameraA', '甲')
    first_visit = client.post('/api/scene/enter', json=first_scene)
    assert first_visit.status_code == 200
    picture = scan(client, 'InstrumentA', camera='CameraA')
    instrument_id = picture['matches'][0]['id']
    assert client.put('/api/instruments/' + instrument_id,
        json={'name': '隔离测试仪器', 'scene': first_visit.json()['scene']['name']}).status_code == 200
    first_binding = client.post('/api/bindings', json={'scan_id': picture['scan_id'],
        'instrument_id': instrument_id, 'operator': '甲'})
    assert first_binding.status_code == 200, first_binding.text

    # Both identities below must be decoded from this composite image.
    pixels = np.full((500, 1000, 3), 255, np.uint8)
    for x, name in ((50, 'Scene01'), (550, 'InstrumentA')):
        pixels[50:450, x:x+400] = cv2.resize(label(app, name), (400, 400))
    decoded = decode_qr(pixels)
    matches = app.qr_matches(*decoded[:2])
    assert {match['id'] for match in matches['scene_matches']} == {first_scene['scene_id']}
    assert {match['id'] for match in matches['matches']} == {instrument_id}
    assert decoded[2]['identity_inferred'] is False

    camera = Camera()
    camera.target = 'CameraB'
    camera.publish(pixels)
    monkeypatch.setattr(app, 'receiver_camera', camera)
    scanner = LiveScanner(vars(app))
    scanner.session = {'session_id': str(uuid.uuid4()), 'operator': '乙', 'status': 'scanning',
                       'camera_id': camera.target, 'bindings': [], 'scene_visits': []}
    frame, metadata = camera.frame()
    with scanner.lock:
        scanner._accept(frame, metadata, decoded, matches)
    assert scanner.session['status'] == 'scanning'
    assert scanner.session['scene_visit']['scene']['id'] == first_scene['scene_id']
    assert scanner.session['scene_visit']['camera_id'] == 'CameraB'
    assert scanner.session['scene_visit']['operator'] == '乙'
    assert scanner.session['binding_errors']
    assert scanner.session['bindings'] == []
    assert len(active_visits(app)) == 2
    with app.db() as conn:
        bindings = [json.loads(row[0]) for row in conn.execute('SELECT document FROM bindings WHERE ended IS NULL')]
    assert len(bindings) == 1
    assert bindings[0]['binding_id'] == first_binding.json()['binding_id']
    assert bindings[0]['camera_id'] == 'CameraA' and bindings[0]['operator'] == '甲'
    # Direct binding retries are rejected too; accepting the scene did not grant the instrument.
    denied = client.post('/api/bindings', json={'scan_id': scanner.session['scan']['scan_id'],
        'instrument_id': instrument_id, 'operator': '乙'})
    assert denied.status_code == 409


def test_authenticated_distinct_personnel_share_scene_without_cross_camera_access(managed, monkeypatch):
    app, client, auth, admin, first = managed
    second = auth.create_user('bob', '乙', 'operator', 'second-person-password', admin.id)
    with app.db() as conn:
        conn.execute('INSERT INTO camera_users VALUES(?,?)', ('cam-b', second.id))
    path = Path(__file__).parents[1] / 'static' / 'labels' / 'Scene01.png'
    bodies, visits, headers_by_user = [], [], []
    for camera_id, username, password, operator in (
        ('cam-a', 'alice', 'test-password', '甲'), ('cam-b', 'bob', 'second-person-password', '乙')):
        token, _ = auth.login(username, password)
        headers = {'Authorization': 'Bearer ' + token.id}
        headers_by_user.append(headers)
        scanned = client.post('/api/scans', files={'file': (path.name, path.read_bytes(), 'image/png')},
                              data={'camera_id': camera_id}, headers=headers)
        assert scanned.status_code == 200, scanned.text
        scanned = scanned.json()
        assert scanned['qr_diagnostics']['identity_inferred'] is False
        body = {'scan_id': scanned['scan_id'], 'scene_id': scanned['scene_matches'][0]['id'], 'operator': operator}
        bodies.append(body)
        entered = client.post('/api/scene/enter', json=body, headers=headers)
        assert entered.status_code == 200, entered.text
        visits.append(entered.json())
    assert visits[0]['scene']['id'] == visits[1]['scene']['id']
    assert {visit['wearer_id'] for visit in visits} == {first.id, second.id}
    assert len(active_visits(app)) == 2
    forbidden = client.post('/api/scene/enter', json=bodies[1] | {'operator': '甲'}, headers=headers_by_user[0])
    assert forbidden.status_code == 403
    assert client.post('/api/camera/relations/end', json={'camera_id': 'cam-b'}, headers=headers_by_user[0]).status_code == 403
    # The managed fixture has no camera worker. Isolate only that external pause RPC;
    # request authorization, relation closure and camera scoping still run normally.
    pauses = []
    monkeypatch.setattr(app.automatic_runner, 'pause', lambda reason: pauses.append(reason))
    ended = client.post('/api/camera/relations/end', json={'camera_id': 'cam-a'}, headers=headers_by_user[0])
    assert ended.status_code == 200
    assert pauses == ['binding_ended']
    assert [visit['visit_id'] for visit in active_visits(app)] == [visits[1]['visit_id']]
