import base64
import io
from datetime import datetime, timezone, timedelta

from PIL import Image

from test_demo import app_client, scan  # noqa: F401
from readout_timing import update_timing


def photo(app, captured_at=None, raw=None, ident='saved-photo'):
    if raw is None:
        image = io.BytesIO()
        Image.new('RGB', (400, 300), 'gray').save(image, 'PNG')
        raw = image.getvalue()
    return {'capture_id': ident, 'camera_id': 'TestCamera', 'captured_at': captured_at or app.now(),
            'source_ref': 'nas://original-photo', 'image_base64': base64.b64encode(raw).decode()}


def bind(app, client):
    capture = scan(client)
    aid = capture['matches'][0]['id']
    client.put('/api/instruments/' + aid, json={'name': '仪器 A', 'scene': '湿实验实验台'})
    return client.post('/api/bindings', json={'scan_id': capture['scan_id'], 'instrument_id': aid, 'operator': '甲'}).json()


def test_unbound_readout_keeps_null_identity_through_later_binding_and_retransmission(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setenv('FIELD_CAMERA_ID', 'TestCamera')
    queued = []
    monkeypatch.setattr(app.readout_pool, 'submit', lambda fn, job: queued.append(job))
    args = {'photo': photo(app)}
    response = client.post('/api/ocr/photo-result', json=args)
    assert response.status_code == 202
    job = response.json()
    assert job['binding_id'] is job['instrument'] is job['scene'] is None
    assert job['operator'] is None and job['instrument_association'] == 'unbound_photo'
    bound = bind(app, client)
    client.post('/api/bindings/' + bound['binding_id'] + '/end')
    app.run_ocr(job)
    assert app.get_job(job['job_id'])['status'] == 'completed'
    assert client.post('/api/ocr/photo-result', json=args).json()['job_id'] == job['job_id']
    assert len(queued) == 1


def test_automatic_photo_before_current_binding_is_read_without_backdating_identity(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setenv('FIELD_CAMERA_ID', 'TestCamera')
    monkeypatch.setattr(app.readout_pool, 'submit', lambda *args: None)
    old = photo(app, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    client.put('/api/automation', json={'enabled': False, 'operator': '后来登记的人'})
    bound = bind(app, client)
    job = app.read_saved_panel(app.SavedPhotoRequest(photo=old), trigger='voice_photo_directory')
    assert job['binding_id'] is None and job['operator'] is None
    fresh = photo(app, ident='fresh')
    job = app.read_saved_panel(app.SavedPhotoRequest(photo=fresh), trigger='voice_photo_directory')
    assert job['binding_id'] == bound['binding_id']


def test_optional_conflicting_binding_does_not_prevent_photo_readout(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setenv('FIELD_CAMERA_ID', 'TestCamera')
    monkeypatch.setattr(app.readout_pool, 'submit', lambda *args: None)
    bind(app, client)
    other = photo(app, raw=(app.BASE/'static/labels/InstrumentB.png').read_bytes())
    job = app.read_saved_panel(app.SavedPhotoRequest(photo=other), trigger='voice_photo_directory')
    assert job['binding_id'] is None
    assert job['instrument']['id'] == 'eae17924-9fa7-4445-ac45-3987f5687be9'
    assert job['instrument_identity_basis'] == 'decoded_photo_qr'
    app.run_ocr(job)
    assert app.get_job(job['job_id'])['status'] == 'completed'


def test_latency_separates_nas_detection_queue_models_and_clock_skew():
    doc = {'submitted_at': '2026-09-14T03:00:01.200+00:00',
           'started_at': '2026-09-14T03:00:01.300+00:00', 'finished_at': '2026-09-14T03:00:03.000+00:00',
           'timing': {'source_written_at': '2026-09-14T03:00:00.000+00:00',
                      'first_observed_at': '2026-09-14T03:00:00.400+00:00', 'stable_at': '2026-09-14T03:00:00.900+00:00'},
           'local_ocr': {'ocr_started_at': '2026-09-14T03:00:01.400+00:00',
                         'ocr_finished_at': '2026-09-14T03:00:01.900+00:00'},
           'fallback': {'attempted_at': '2026-09-14T03:00:02.000+00:00', 'finished_at': '2026-09-14T03:00:02.800+00:00'}}
    timing = update_timing(doc)
    values = timing['durations_ms']
    assert values['write_to_detect_ms'] == 400 and values['file_stability_ms'] == 500
    assert values['queue_wait_ms'] == 100 and values['ocr_ms'] == 500 and values['vision_ms'] == 800
    assert values['write_to_result_ms'] == 3000 and values['write_to_ocr_ms'] == 1400
    timing['source_written_at'] = '2026-09-14T04:00:00+00:00'
    timing = update_timing(doc)
    assert timing['source_clock_ahead'] and timing['durations_ms']['write_to_detect_ms'] is None
