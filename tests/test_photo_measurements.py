import base64
import copy
import hashlib
import io
import json
import sqlite3
import uuid

from PIL import Image
import pytest

from test_demo import app_client, register, scan  # noqa: F401
from panel_readout import save
from reading_results import build_readings
from measurement_records import build_records


@pytest.fixture
def setup(app_client, monkeypatch):
    app, client = app_client
    capture = scan(client)
    iid = capture['matches'][0]['id']
    register(client, iid)
    binding = client.post('/api/bindings', json={'scan_id': capture['scan_id'],
        'instrument_id': iid, 'operator': 'Reader'}).json()
    queued = []
    monkeypatch.setattr(app, 'RECORD_MODE', 'production')
    monkeypatch.setattr(app.readout_pool, 'submit', lambda fn, doc: queued.append(doc))
    return app, client, binding, queued


def photos(app, count=3):
    result = []
    for i in range(count):
        out = io.BytesIO()
        Image.new('RGB', (400, 300), (i * 20, 100, 70)).save(out, format='PNG')
        result.append({'capture_id': 'burst-photo-' + str(i), 'camera_id': 'TestCamera',
            'captured_at': app.now(), 'source_ref': 'nas://photo/' + str(i),
            'image_base64': base64.b64encode(out.getvalue()).decode()})
    return result


def enqueue(setup, count=3):
    app, client, binding, queued = setup
    request = {'burst_id': 'burst-1', 'photos': photos(app, count),
        'experiment_context_ref': 'experiment://001/step-2', 'instrument_ids': [binding['instrument']['id']]}
    response = client.post('/api/ocr/photo-burst', json=request)
    assert response.status_code == 202, response.text
    return response.json(), request


def complete(setup, index, text='51', unit='°C', qr=True, *, publish=True):
    app, _, binding, queued = setup
    job = copy.deepcopy(queued[index])
    snapshots = copy.deepcopy([binding])
    if not qr:
        snapshots[0].pop('qr_hash', None)
    job.update(status='completed', finished_at=app.now(),
        all_binding_snapshots=snapshots, model='OCR-test-double', device='cpu',
        panel_detection={'status': 'completed', 'weights_sha256': 'a' * 64},
        panel_regions=[{'panel_id': 'a-temperature', 'instrument': binding['instrument'], 'class_id': 0,
            'instrument_id': binding['instrument']['id'], 'binding_id': binding['binding_id'],
            'association_basis': 'panel_detector_session_binding', 'measurement_name': '温度',
            'bbox': [10, 10, 110, 45]}],
        lines=[{'panel_id': 'a-temperature', 'text': text, 'unit': unit, 'confidence': .91,
                'polygon': [[10, 10], [110, 10], [110, 45], [10, 45]]}])
    job['local_ocr'] = {'lines': copy.deepcopy(job['lines']), 'model': 'OCR-test-double'}
    job['readings'] = build_readings(job)
    job['measurement_records'] = build_records(job)
    if publish:
        save(vars(app), job)
    return job


def get_draft(client, mid):
    return client.get('/api/photo-measurements/' + mid).json()


def test_three_photos_one_confirmed_record_retry_and_raw_evidence(setup):
    app, client, binding, queued = setup
    reply, request = enqueue(setup)
    mid = reply['measurement_id']
    assert len(set(reply['job_ids'])) == 3
    assert len(client.get('/api/photo-measurements').json()['items']) == 1
    assert client.get('/api/experiment-records').json()['items'] == []
    assert client.post('/api/ocr/photo-burst', json=request).json()['job_ids'] == reply['job_ids']
    assert len(queued) == 3
    assert get_draft(client, mid)['status'] == 'collecting'
    complete(setup, 0)
    draft = get_draft(client, mid)
    assert client.post(f'/api/photo-measurements/{mid}/confirm', json={'actor': 'Reader', 'revision': draft['revision']}).status_code == 409
    complete(setup, 1)
    complete(setup, 2)
    draft = get_draft(client, mid)
    assert draft['status'] == 'draft' and not draft['blockers']
    field = draft['fields'][0]
    assert field['name'] == '温度' and field['value'] == 51 and field['unit'] == '°C'
    assert field['agreement']['matching_photos'] == 3 and field['agreement']['accuracy'] is None
    assert all(r['confidence_basis'] == 'model_score_not_measured_accuracy' for r in field['original_candidates'])
    for source in draft['sources']:
        raw = client.get(source['original_url'])
        assert raw.status_code == 200 and hashlib.sha256(raw.content).hexdigest() == source['source_sha256']
        assert source['versions']['rules'] == 'photo-measurement/2'
        assert source['panel_detection']['weights_sha256'] == 'a' * 64
    decision = {'actor': 'Reader', 'revision': draft['revision']}
    first = client.post(f'/api/photo-measurements/{mid}/confirm', json=decision)
    assert first.status_code == 200, first.text
    assert client.post(f'/api/photo-measurements/{mid}/confirm', json=decision).json() == first.json()
    assert len(client.get('/api/experiment-records').json()['items']) == 1
    assert first.json()['confirmed_fields'][0]['value'] == 51
    with app.db() as conn:
        assert conn.execute("SELECT count(*) FROM archive_outbox WHERE entity='experiment_records'").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('DELETE FROM experiment_records WHERE id=?', (mid,))
    assert client.get('/api/jobs/' + queued[0]['job_id']).json()['lines'][0]['text'] == '51'


@pytest.mark.parametrize('values,unit,qr,issue', [(['51', '52'], None, True, '冲突'),
    (['51', '51'], 'rpm', True, '单位'), (['51', '51'], None, False, '二维码')])
def test_conflict_unknown_qr_and_wrong_unit_require_audited_correction(setup, values, unit, qr, issue):
    app, client, binding, _ = setup
    reply, _ = enqueue(setup, 2)
    mid = reply['measurement_id']
    for i, value in enumerate(values):
        complete(setup, i, value, unit, qr)
    draft = get_draft(client, mid)
    assert any(issue in b for b in draft['blockers'])
    decision = {'actor': 'Reader', 'revision': draft['revision']}
    assert client.post(f'/api/photo-measurements/{mid}/confirm', json=decision).status_code == 409
    patch = decision | {'reason': '对照两张原始面板照片校正', 'fields': [{
        'field_id': draft['fields'][0]['field_id'], 'instrument_id': binding['instrument']['id'],
        'name': '温度', 'value': 51, 'unit': '°C'}]}
    updated = client.post(f'/api/photo-measurements/{mid}/revisions', json=patch)
    assert updated.status_code == 200, updated.text
    updated = updated.json()
    assert not updated['blockers'] and updated['fields'][0]['corrected']
    assert updated['fields'][0]['original_candidates'] == draft['fields'][0]['original_candidates']
    assert updated['corrections'][0]['before_fields'] == draft['fields']
    assert client.post(f'/api/photo-measurements/{mid}/confirm', json=decision).status_code == 409
    assert client.post(f'/api/photo-measurements/{mid}/confirm', json=decision | {'revision': updated['revision']}).status_code == 200


def test_test_mode_automatic_write_cannot_be_promoted(setup, monkeypatch):
    app, client, _, queued = setup
    monkeypatch.setattr(app, 'RECORD_MODE', 'test')
    reply, _ = enqueue(setup, 1)
    assert queued[0]['record_scope'] == 'test_only'
    complete(setup, 0)
    draft = get_draft(client, reply['measurement_id'])
    assert client.post(f"/api/photo-measurements/{reply['measurement_id']}/confirm",
        json={'actor': 'Reader', 'revision': draft['revision']}).status_code == 409
    assert client.get('/api/experiment-records').json()['items'] == []


def test_production_single_photo_has_draft_but_never_infers_time_or_context(setup):
    app, client, binding, queued = setup
    capture = scan(client)
    reply = client.post('/api/ocr', json={'capture_id': capture['capture_id']}).json()
    complete(setup, 0)
    draft = get_draft(client, reply['measurement_id'])
    assert any('采集时间' in b for b in draft['blockers']) and '缺少实验上下文引用' in draft['blockers']
    update = {'actor': 'Reader', 'revision': draft['revision'], 'reason': '补充采集回执',
        'experiment_context_ref': 'experiment://001/step-2', 'capture_times': {capture['capture_id']: app.now()}}
    bad = client.post(f"/api/photo-measurements/{reply['measurement_id']}/revisions", json=update | {
        'capture_times': {capture['capture_id']: '2026-09-14T10:00:00'}})
    assert bad.status_code == 422
    result = client.post(f"/api/photo-measurements/{reply['measurement_id']}/revisions", json=update)
    assert result.status_code == 200 and not result.json()['blockers']
    assert result.json()['sources'][0]['captured_at'] is None


def test_ambiguous_digits_cannot_be_confirmed_and_group_metadata_cannot_change(setup):
    _, client, _, _ = setup
    reply, request = enqueue(setup, 1)
    complete(setup, 0, '00051')
    draft = get_draft(client, reply['measurement_id'])
    assert draft['fields'][0]['value'] is None and draft['blockers']
    retry = request | {'experiment_context_ref': 'different-experiment'}
    assert client.post('/api/ocr/photo-burst', json=retry).status_code == 409
    assert client.post('/api/ocr/photo-burst', json=request | {'photos': request['photos'] * 2}).status_code == 422


def test_confirmation_rejects_corrupt_original_and_direct_retry_reuses_job(setup):
    app, client, _, queued = setup
    capture = scan(client)
    body = {'capture_id': capture['capture_id'], 'measurement': {'experiment_context_ref': 'experiment://1'}}
    first = client.post('/api/ocr', json=body).json()
    complete(setup, 0)
    assert client.post('/api/ocr', json=body).json()['job_id'] == first['job_id']
    assert len(queued) == 1
    draft = get_draft(client, first['measurement_id'])
    update = {'actor': 'Reader', 'revision': draft['revision'], 'reason': '补充采集回执',
        'capture_times': {capture['capture_id']: app.now()}}
    draft = client.post(f"/api/photo-measurements/{first['measurement_id']}/revisions", json=update).json()
    (app.DATA / capture['original_blob']).write_bytes(b'corrupted-test-evidence')
    assert client.post(f"/api/photo-measurements/{first['measurement_id']}/confirm",
        json={'actor': 'Reader', 'revision': draft['revision']}).status_code == 409
    assert not client.get('/api/experiment-records').json()['items']


def test_unknown_unit_is_not_guessed_from_metric_name(setup):
    _, client, _, _ = setup
    reply, _ = enqueue(setup, 1)
    complete(setup, 0, '51', None)
    draft = get_draft(client, reply['measurement_id'])
    assert draft['fields'][0]['unit'] is None
    assert draft['fields'][0]['original_candidates'][0]['unit_basis'] == 'unknown'
    assert any('单位' in issue for issue in draft['blockers'])


def test_one_measurement_separates_temperature_speed_and_other_instrument_mass(setup):
    app, client, binding, queued = setup
    other_capture = scan(client, 'InstrumentB')
    bid = other_capture['matches'][0]['id']
    register(client, bid)
    other = client.post('/api/bindings', json={'scan_id': other_capture['scan_id'], 'instrument_id': bid, 'operator': 'Reader'}).json()
    request = {'burst_id': 'multi-device', 'photos': photos(app, 2), 'experiment_context_ref': 'experiment://2',
               'instrument_ids': [bid, binding['instrument']['id']]}
    mid = client.post('/api/ocr/photo-burst', json=request).json()['measurement_id']
    for i in range(2):
        job = complete(setup, i, publish=False)
        job['all_binding_snapshots'].append(other)
        for pid, name, bound, cls, text, unit in [('a-speed', '转速', binding, 0, '390', 'rpm'),
                ('b-mass', '质量', other, 1, '32.4223', 'g')]:
            job['panel_regions'].append({'panel_id': pid, 'instrument': bound['instrument'], 'class_id': cls,
                'instrument_id': bound['instrument']['id'], 'binding_id': bound['binding_id'],
                'association_basis': 'panel_detector_session_binding', 'measurement_name': name, 'bbox': [140, 10, 240, 45]})
            job['lines'].append({'panel_id': pid, 'text': text, 'unit': unit})
        job['readings'] = build_readings(job)
        save(vars(app), job)
    draft = get_draft(client, mid)
    assert not draft['blockers']
    assert [(f['instrument']['id'], f['name'], f['value'], f['unit']) for f in draft['fields']] == [
        (binding['instrument']['id'], '温度', 51, '°C'), (binding['instrument']['id'], '转速', 390, 'rpm'), (bid, '质量', 32.4223, 'g')]
    result = client.post(f'/api/photo-measurements/{mid}/confirm', json={'actor': 'Reader', 'revision': draft['revision']})
    assert result.status_code == 200 and len(result.json()['confirmed_fields']) == 3
    assert client.get('/api/experiment-records/' + mid).json() == result.json()


def test_manual_target_correction_cannot_claim_another_cameras_occupied_device(setup):
    from test_instrument_ownership import own
    app, client, _, _ = setup
    other = own(client, 'InstrumentB', 'OtherCamera', 'OtherUser')
    request = {'burst_id': 'manual-target', 'photos': photos(app, 1), 'experiment_context_ref': 'experiment://3'}
    mid = client.post('/api/ocr/photo-burst', json=request).json()['measurement_id']
    complete(setup, 0)
    draft = get_draft(client, mid)
    changed = client.post(f'/api/photo-measurements/{mid}/revisions', json={'actor': 'Reader', 'revision': draft['revision'],
        'reason': '测试错误的人工归属', 'fields': [{'field_id': draft['fields'][0]['field_id'],
            'instrument_id': other['instrument']['id'], 'name': '质量', 'value': 51, 'unit': 'g'}]}).json()
    result = client.post(f'/api/photo-measurements/{mid}/confirm', json={'actor': 'Reader', 'revision': changed['revision']})
    assert result.status_code == 409 and 'OtherUser' in result.text
    assert not client.get('/api/experiment-records').json()['items']


def test_archive_one_group_and_originals_and_no_three_production_reading_folders(setup, tmp_path, monkeypatch):
    from archive_browse import build_views
    app, client, _, _ = setup
    reply, _ = enqueue(setup)
    for i in range(3):
        complete(setup, i)
    group = get_draft(client, reply['measurement_id'])
    client.post(f"/api/photo-measurements/{reply['measurement_id']}/confirm",
        json={'actor': 'Reader', 'revision': group['revision']})
    with app.db() as conn:
        rows = [dict(row) for row in conn.execute('SELECT * FROM archive_outbox')]
    monkeypatch.setenv('FIELD_ARCHIVE_ROOT', str(tmp_path / 'FieldRecognitionArchive'))
    for row in rows:
        row['receipt_path'] = app.archive_store.publish(row)
    views = build_views(rows)
    catalog = json.loads(views['Browse/Catalog.json'])['categories']
    assert len(catalog['ExperimentRecords']) == 1 and len(catalog['MeasurementDrafts']) == 1
    assert catalog['PanelReadings'] == []
    receipt = json.loads((tmp_path / 'FieldRecognitionArchive' / rows[-2]['receipt_path']).read_bytes())
    if receipt['entity'] != 'experiment_records':
        receipt = next(json.loads((tmp_path / 'FieldRecognitionArchive' / r['receipt_path']).read_bytes())
                       for r in rows if r['entity'] == 'experiment_records')
    assert len([k for k in receipt['artifacts'] if k.endswith('_original')]) == 3
