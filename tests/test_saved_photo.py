import base64
import hashlib
import io
from datetime import datetime, timezone, timedelta

import pytest
from PIL import Image

from saved_photo import load_saved_photo
from test_demo import app_client, register, scan  # noqa: F401


@pytest.fixture
def saved(app_client, monkeypatch):
    app, client = app_client
    picture = scan(client)
    aid = picture['matches'][0]['id']
    register(client, aid)
    bound = client.post('/api/bindings', json={'scan_id': picture['scan_id'], 'instrument_id': aid, 'operator': 'AgentTest'}).json()
    app.ocr_warmup_future.result(timeout=2)
    queued = []
    monkeypatch.setattr(app.readout_pool, 'submit', lambda fn, job: queued.append(job))
    monkeypatch.setattr(app, 'camera_capture', lambda: pytest.fail('Must reuse saved photo, never recapture'))
    raw = io.BytesIO()
    Image.new('RGB', (400, 300), 'gray').save(raw, format='PNG')
    photo = {'capture_id': 'original-agent-photo', 'camera_id': 'TestCamera',
             'captured_at': app.now(), 'source_ref': 'nas://voice_photos/TestCamera/photo.png',
             'image_base64': base64.b64encode(raw.getvalue()).decode(),
             'sha256': hashlib.sha256(raw.getvalue()).hexdigest()}
    return app, client, bound, photo, queued


def test_existing_photo_tool_preserves_receipt_and_reuses_even_completed_job(saved):
    app, client, bound, photo, queued = saved
    assert client.get('/api/state').json()['jobs'] == []  # Binding remains preload-only.
    args = {'binding_id': bound['binding_id'], 'photo': photo}
    first = client.post('/api/tools/read_saved_panel', json=args).json()['result']
    repeated = client.post('/api/tools/read_saved_panel', json=args).json()['result']
    assert len(queued) == 1 and first['job_id'] == repeated['job_id']
    assert first['external_photo']['capture_id'] == photo['capture_id']
    assert first['external_photo']['captured_at'] == photo['captured_at'].replace('+00:00', 'Z')
    assert first['external_photo']['source_ref'] == photo['source_ref']
    assert first['external_photo']['source_sha256'] == photo['sha256']
    assert 'image_base64' not in str(first)
    app.run_ocr(dict(first))
    finished = client.post('/api/tools/read_saved_panel', json=args).json()['result']
    assert finished['job_id'] == first['job_id'] and finished['status'] == 'completed'
    assert len(queued) == 1
    changed = photo | {'image_base64': base64.b64encode(b'changed').decode(), 'sha256': None}
    assert client.post('/api/ocr/photo-result', json=args | {'photo': changed}).status_code == 409
    client.post('/api/bindings/' + bound['binding_id'] + '/end')
    # A late retransmission belongs to its capture-time binding even after handoff/end.
    late = client.post('/api/ocr/photo-result', json=args)
    assert late.status_code == 202 and late.json()['job_id'] == first['job_id']


@pytest.mark.parametrize('changes,status', [
    ({'camera_id': 'AnotherCamera'}, 409),
    ({'captured_at': '2020-01-01T00:00:00+00:00'}, 409),
    ({'captured_at': '2026-09-11T00:00:00'}, 422),
    ({'sha256': '0' * 64}, 422),
    ({'image_base64': '!invalid'}, 422),
])
def test_mismatched_or_old_photo_is_rejected_without_inference(saved, changes, status):
    _, client, bound, photo, queued = saved
    response = client.post('/api/ocr/photo-result', json={'binding_id': bound['binding_id'], 'photo': photo | changes})
    assert response.status_code == status and not queued


def test_nas_specific_file_adapter_and_path_boundaries(saved, monkeypatch, tmp_path):
    app, client, bound, _, queued = saved
    root = tmp_path / 'voice_photos'
    captured = datetime.now(timezone(timedelta(hours=8))) + timedelta(seconds=1)
    relative = 'TestCamera/' + captured.strftime('%Y-%m-%d/%H-%M-%S/%Y%m%d_%H%M%S_001.jpg')
    path = root / relative
    path.parent.mkdir(parents=True)
    Image.new('RGB', (400, 300), 'gray').save(path)
    monkeypatch.setenv('FIELD_SAVED_PHOTO_ROOT', str(root))
    monkeypatch.setenv('FIELD_SAVED_PHOTO_TIMEZONE', 'Asia/Shanghai')
    args = {'binding_id': bound['binding_id'], 'image_path': relative}
    result = client.post('/api/tools/read_saved_panel', json=args).json()['result']
    assert len(queued) == 1 and result['source'] == 'agent_saved_photo'
    assert result['external_photo']['source_ref'] == str(path)
    assert result['external_photo']['timestamp_basis'] == 'nas_filename_local_time_not_hardware_verified'
    assert client.post('/api/ocr/photo-result', json=args | {'image_path': str(path)}).json()['job_id'] == result['job_id']
    outside = tmp_path / 'secret.jpg'
    Image.new('RGB', (100, 100)).save(outside)
    assert client.post('/api/ocr/photo-result', json=args | {'image_path': str(outside)}).status_code == 422
    assert client.post('/api/ocr/photo-result', json=args | {'image_path': str(path.parent)}).status_code == 422
    with pytest.raises(Exception) as exc:
        load_saved_photo(relative, expected_camera='AnotherCamera', max_bytes=app.MAX_BYTES)
    assert exc.value.status_code == 409
    link = path.parent / captured.strftime('%Y%m%d_%H%M%S_002.jpg')
    link.symlink_to(outside)
    assert client.post('/api/ocr/photo-result', json=args | {'image_path': str(link)}).status_code == 422
