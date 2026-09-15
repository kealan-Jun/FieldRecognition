import json
import uuid

from instrument_ownership import install
from test_demo import app_client, scan, wait_for_job  # noqa: F401
from test_unbound_readout import photo


def own(client, label, camera, operator):
    capture = scan(client, label, camera)
    iid = capture['matches'][0]['id']
    client.put('/api/instruments/' + iid, json={'name': label, 'scene': '湿实验实验台'})
    response = client.post('/api/bindings', json={'scan_id': capture['scan_id'], 'instrument_id': iid, 'operator': operator})
    assert response.status_code == 200, response.text
    return response.json()


def test_two_users_cannot_take_over_and_handoff_requires_both_decisions(app_client):
    app, client = app_client
    old = own(client, 'InstrumentA', 'CameraAlpha', '甲')
    second = own(client, 'InstrumentB', 'CameraBeta', '乙')
    target = scan(client, 'InstrumentA', 'CameraBeta')
    ordinary = {'scan_id': target['scan_id'], 'instrument_id': old['instrument']['id'], 'operator': '乙'}
    blocked = client.post('/api/bindings', json=ordinary)
    assert blocked.status_code == 409
    assert blocked.json()['detail']['operator'] == '甲'
    assert blocked.json()['detail']['started_at'] == old['started_at']
    body = {'request_id': str(uuid.uuid4()), 'binding_id': old['binding_id'],
        'recipient_scan_id': target['scan_id'], 'recipient_operator': '乙'}
    pending = client.post('/api/handoffs', json=body).json()
    assert client.post('/api/handoffs', json=body).json() == pending
    hid = pending['handoff_id']
    decision = {'actor': '乙', 'camera_id': 'CameraBeta', 'revision': pending['revision']}
    assert client.post(f'/api/handoffs/{hid}/accept', json=decision).status_code == 409
    assert client.post(f'/api/handoffs/{hid}/release', json=decision).status_code == 409
    release = {'actor': '甲', 'camera_id': 'CameraAlpha', 'revision': pending['revision']}
    handed = client.post(f'/api/handoffs/{hid}/release', json=release).json()
    assert handed['status'] == 'released'
    assert client.post(f'/api/handoffs/{hid}/release', json=release).json() == handed
    assert client.post('/api/bindings', json=ordinary).json()['detail']['code'] == 'handoff_reserved'
    install(vars(app))  # Reservation survives service/module recreation.
    accepted = client.post(f'/api/handoffs/{hid}/accept', json=decision | {'revision': handed['revision']}).json()
    assert accepted['status'] == 'completed'
    with app.db() as conn:
        active = [json.loads(r[0]) for r in conn.execute('SELECT document FROM bindings WHERE ended IS NULL')]
        assert len(active) == 2 and {b['camera_id'] for b in active} == {'CameraBeta'}
        assert second['binding_id'] in {b['binding_id'] for b in active}
        assert conn.execute("SELECT count(*) FROM archive_outbox WHERE entity='binding_handoffs'").fetchone()[0] == 3


def test_late_old_photo_keeps_capture_time_owner_after_transfer(app_client, monkeypatch):
    app, client = app_client
    old = own(client, 'InstrumentA', 'CameraAlpha', '甲')
    old_time = app.now()
    target = scan(client, 'InstrumentA', 'CameraBeta')
    pending = client.post('/api/handoffs', json={'request_id': str(uuid.uuid4()), 'binding_id': old['binding_id'],
        'recipient_scan_id': target['scan_id'], 'recipient_operator': '乙'}).json()
    hid = pending['handoff_id']
    handed = client.post(f'/api/handoffs/{hid}/release', json={'actor': '甲', 'camera_id': 'CameraAlpha', 'revision': 1}).json()
    gap_time = app.now()
    new = client.post(f'/api/handoffs/{hid}/accept', json={'actor': '乙', 'camera_id': 'CameraBeta', 'revision': handed['revision']}).json()
    raw = (app.BASE / 'static/labels/InstrumentA.png').read_bytes()
    args = {'photo': photo(app, old_time, raw, 'late-old') | {'camera_id': 'CameraAlpha'}}
    late = client.post('/api/ocr/photo-result', json=args)
    assert late.status_code == 202, late.text
    late = late.json()
    assert late['binding_id'] == old['binding_id'] and late['operator'] == '甲'
    assert late['all_binding_snapshots'][0]['ended_at'] == handed['released_at']
    assert wait_for_job(client, late['job_id'])['status'] == 'completed'
    # A retransmission after the transfer never reattributes to the new holder.
    assert client.post('/api/ocr/photo-result', json=args).json()['job_id'] == late['job_id']
    explicit = client.post('/api/ocr/photo-result', json={'binding_id': old['binding_id'],
        'photo': args['photo'] | {'capture_id': 'late-old-explicit'}})
    assert explicit.status_code == 202 and explicit.json()['operator'] == '甲'
    gap = client.post('/api/ocr/photo-result', json={'photo': photo(app, gap_time, raw, 'gap') | {'camera_id': 'CameraAlpha'}}).json()
    assert gap['binding_id'] is None and gap['operator'] != '乙'
    current = client.post('/api/ocr/photo-result', json={'photo': photo(app, raw=raw, ident='new-owner') | {'camera_id': 'CameraBeta'}}).json()
    assert current['binding_id'] == new['new_binding_id'] and current['operator'] == '乙'
    before = client.post('/api/ocr/photo-result', json={'photo':photo(app, old_time, raw, 'before-transfer') | {'camera_id':'CameraBeta'}}).json()
    assert before['binding_id'] is None
    assert client.post('/api/ocr', json={'capture_id':before['capture_id'], 'binding_id':new['new_binding_id']}).status_code == 409
