import base64

import cv2
import numpy as np

from test_demo import app_client, scan, wait_for_job  # noqa: F401
from test_unbound_readout import photo
from reading_results import build_readings


def two_bindings(app, client):
    bindings = []
    for name in ('InstrumentA', 'InstrumentB'):
        capture = scan(client, name)
        aid = capture['matches'][0]['id']
        client.put('/api/instruments/' + aid, json={'name': name, 'scene': '湿实验实验台'})
        b = client.post('/api/bindings', json={'scan_id': capture['scan_id'], 'instrument_id': aid, 'operator': '甲'})
        assert b.status_code == 200
        bindings.append(b.json())
    return bindings


def test_single_photo_resolves_visible_qr_among_multiple_bindings(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setenv('FIELD_CAMERA_ID', 'TestCamera')
    bindings = two_bindings(app, client)
    raw = (app.BASE / 'static/labels/InstrumentB.png').read_bytes()
    j = app.read_saved_panel(app.SavedPhotoRequest(photo=photo(app, raw=raw)), trigger='voice_photo_directory')
    assert j['binding_id'] == bindings[1]['binding_id']
    assert j['binding_ids'] == [bindings[1]['binding_id']]
    assert j['instrument_candidates'][0]['basis'] == 'same_image_qr'
    assert wait_for_job(client, j['job_id'])['status'] == 'completed'


def test_no_qr_with_two_bindings_never_picks_first_and_keeps_both_readings(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setenv('FIELD_CAMERA_ID', 'TestCamera')
    bindings = two_bindings(app, client)
    monkeypatch.setattr(app, 'predict_panel', lambda *args: {'status': 'completed', 'device': 'cpu',
        'lines': [{'text': '0.0000 g', 'polygon': [[10, 20], [110, 20], [110, 40], [10, 40]]},
                  {'text': '200 rpm', 'polygon': [[210, 30], [310, 30], [310, 50], [210, 50]]}]})
    j = app.read_saved_panel(app.SavedPhotoRequest(photo=photo(app)), trigger='voice_photo_directory')
    done = wait_for_job(client, j['job_id'])
    assert done['binding_id'] is done['instrument'] is None
    assert set(done['binding_ids']) == {b['binding_id'] for b in bindings}
    assert [r['value'] for r in done['readings']] == ['0.0000', '200']
    assert [r['unit'] for r in done['readings']] == ['g', 'rpm']
    assert all(r['instrument'] is None and r['association_basis'] == 'ambiguous' for r in done['readings'])
    assert len({r['reading_id'] for r in done['readings']}) == 2
    page = client.get('/api/readouts').json()['items'][0]
    assert len(page['readings']) == 2 and '归属待确认' in page['target']
    grouped = client.get('/api/workbenches').json()
    bench = next(g for g in grouped['workbenches'] if g['name'] == '湿实验实验台')
    assert len(bench['instruments']) == 2
    assert all(not i['readings'] for i in bench['instruments'])
    assert len(bench['unassigned_readings']) == 2
    assert bench['reading_count'] == 2 and bench['photo_count'] == 1
    assert not grouped['measurement_values_summed'] and not grouped['activity_inferred']


def test_same_image_two_instruments_can_be_read_without_conflicting_binding(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setenv('FIELD_CAMERA_ID', 'TestCamera')
    bindings = two_bindings(app, client)
    pixels = np.full((500, 1000, 3), 255, np.uint8)
    for x, name in [(50, 'InstrumentA'), (550, 'InstrumentB')]:
        pixels[50:450, x:x+400] = cv2.resize(cv2.imread(str(app.BASE/'static/labels'/f'{name}.png')), (400,400))
    ok, raw = cv2.imencode('.png', pixels)
    assert ok
    j = app.read_saved_panel(app.SavedPhotoRequest(photo=photo(app, raw=raw.tobytes())), trigger='voice_photo_directory')
    assert j['binding_id'] is None and len(j['qr_matches']) == len(j['binding_ids']) == 2
    assert wait_for_job(client, j['job_id'])['status'] == 'completed'


def test_decimal_ambiguity_is_retained_without_inventing_decimal():
    from aliyun_vision import fallback_reason
    assert fallback_reason([{'text': '00000'}]) == 'decimal_uncertain'
    d = {'job_id': 'job', 'capture_id': 'capture', 'lines': [{'text': '00000'}]}
    r = build_readings(d)[0]
    assert r['value'] == '00000' and r['quality_issue'] == 'decimal_uncertain'
    assert not r['human_verified']


def test_registered_photo_qr_identifies_instrument_without_session_binding(app_client, monkeypatch):
    app, client = app_client
    aid = 'e9434a0a-3319-414a-b988-4cc6884edce4'
    client.put('/api/instruments/'+aid, json={'name': '仪器 A', 'scene': '湿实验实验台'})
    monkeypatch.setattr(app, 'predict_panel', lambda *args: {'status': 'completed', 'device': 'cpu', 'lines': [{'text': '1.23 g'}]})
    raw = (app.BASE/'static/labels/InstrumentA.png').read_bytes()
    response = client.post('/api/ocr/photo-result', json={'photo': photo(app, raw=raw)})
    assert response.status_code == 202
    done = wait_for_job(client, response.json()['job_id'])
    assert done['instrument']['id'] == aid and done['binding_id'] is None
    assert done['instrument_identity_basis'] == 'decoded_photo_qr'
    assert done['readings'][0]['instrument']['id'] == aid
    assert done['readings'][0]['association_basis'] == 'same_image_qr'
    assert not client.get('/api/state').json()['bindings']
    assert client.get('/api/readouts').json()['items'][0]['target'] == '仪器 A'
    bench = next(g for g in client.get('/api/workbenches').json()['workbenches'] if g['name'] == '湿实验实验台')
    assert next(i for i in bench['instruments'] if i['id'] == aid)['readings'][0]['text'] == '1.23 g'
