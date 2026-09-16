import json
from datetime import datetime

import pytest

from binding_policy import contains, next_midnight, sweep
from readout_context import at_capture
from test_demo import app_client, scan, register  # noqa: F401


@pytest.mark.parametrize('start,deadline', [
    ('2026-09-15T15:59:00+00:00', '2026-09-15T16:00:00+00:00'),
    ('2026-09-15T16:01:00+00:00', '2026-09-16T16:00:00+00:00'),
    ('2026-12-31T23:59:00+08:00', '2026-12-31T16:00:00+00:00'),
])
def test_expiry_is_beijing_midnight_not_24_hours(start, deadline):
    assert next_midnight(start).isoformat() == deadline


def test_day_change_requires_fresh_codes_retains_late_photo_and_old_evidence(app_client, monkeypatch):
    app, client = app_client
    clock = ['2026-09-15T15:58:00+00:00']
    monkeypatch.setattr(app, 'now', lambda: clock[0])
    old_scan = scan(client)
    iid = old_scan['matches'][0]['id']
    register(client, iid)
    body = {'scan_id':old_scan['scan_id'], 'instrument_id':iid, 'operator':'Reader'}
    first = client.post('/api/bindings', json=body).json()
    assert first['valid_until'] == '2026-09-15T16:00:00+00:00'
    assert client.post('/api/bindings', json=body).json()['binding_id'] == first['binding_id']
    with app.db() as conn:
        scene = json.loads(conn.execute('SELECT document FROM scene_visits WHERE ended IS NULL').fetchone()[0])
    clock[0] = '2026-09-15T16:00:00+00:00'
    # Explicit epoch validation also prevents yesterday's QR from re-authorizing.
    rejected = client.post('/api/bindings', json=body)
    assert rejected.status_code == 409 and '跨日' in rejected.text
    state = client.get('/api/state').json()
    assert not [b for b in state['bindings'] if not b['ended_at']]
    assert not state['scene_visits']
    with app.db() as conn:
        ended = json.loads(conn.execute('SELECT document FROM bindings WHERE id=?',(first['binding_id'],)).fetchone()[0])
        old_scene = json.loads(conn.execute('SELECT document FROM scene_visits WHERE id=?',(scene['visit_id'],)).fetchone()[0])
        assert old_scene['ended_at'] == ended['ended_at'] == first['valid_until']
        assert ended['end_reason'] == 'daily_qr_expired'
        photo = {'camera_id':'TestCamera', 'received_at':clock[0], 'external_photo':{'captured_at':'2026-09-15T15:59:59+00:00'}}
        context = at_capture(vars(app), conn, photo, automatic=True)
        assert context['binding_ids'] == [first['binding_id']]
        assert at_capture(vars(app), conn, photo | {'external_photo':{'captured_at':clock[0]}}, automatic=True)['binding_ids'] == []
    assert sweep(vars(app)) == []
    assert client.get(old_scan['image_url']).status_code == 200
    new_scan = scan(client)
    second = client.post('/api/bindings', json=body | {'scan_id':new_scan['scan_id']}).json()
    assert second['binding_id'] != first['binding_id'] and second['binding_date'] == '2026-09-16'
    assert not second['scene_qr_verified']  # yesterday's scene cannot count today
    assert client.post('/api/bindings', json=body | {'scan_id':new_scan['scan_id']}).json()['binding_id'] == second['binding_id']


def test_background_expiry_runs_even_when_automation_paused_and_camera_absent(app_client, monkeypatch):
    app, client = app_client
    clock = ['2026-09-15T15:59:00+00:00']
    monkeypatch.setattr(app, 'now', lambda:clock[0])
    source = scan(client);iid=source['matches'][0]['id'];register(client,iid)
    first=client.post('/api/bindings',json={'scan_id':source['scan_id'],'instrument_id':iid,'operator':'Reader'}).json()
    clock[0]='2026-09-16T02:00:00+00:00'
    app.automatic_runner.step()
    with app.db() as conn:
        ended=json.loads(conn.execute('SELECT document FROM bindings WHERE id=?',(first['binding_id'],)).fetchone()[0])
    assert ended['ended_at']=='2026-09-15T16:00:00+00:00'
    assert not app.current_readout_binding(first)
    assert contains(ended,'2026-09-15T15:59:01+00:00')
    assert not contains(ended,clock[0])


def test_yesterdays_released_handoff_cannot_be_accepted(app_client,monkeypatch):
    import uuid
    from test_instrument_ownership import own
    app,client=app_client
    clock=['2026-09-15T15:59:00+00:00'];monkeypatch.setattr(app,'now',lambda:clock[0])
    old=own(client,'InstrumentA','CameraAlpha','甲');target=scan(client,'InstrumentA','CameraBeta')
    request={'request_id':str(uuid.uuid4()),'binding_id':old['binding_id'],'recipient_scan_id':target['scan_id'],'recipient_operator':'乙'}
    transfer=client.post('/api/handoffs',json=request).json();hid=transfer['handoff_id']
    released=client.post(f'/api/handoffs/{hid}/release',json={'actor':'甲','camera_id':'CameraAlpha','revision':1}).json()
    clock[0]='2026-09-15T16:00:00+00:00'
    assert client.post(f'/api/handoffs/{hid}/accept',json={'actor':'乙','camera_id':'CameraBeta','revision':released['revision']}).status_code==409
    assert client.get('/api/handoffs').json()['items'][0]['status']=='expired'
    fresh=scan(client,'InstrumentA','CameraBeta')
    result=client.post('/api/bindings',json={'scan_id':fresh['scan_id'],'instrument_id':old['instrument']['id'],'operator':'乙'})
    assert result.status_code==200
    assert result.json()['binding_date']=='2026-09-16'
