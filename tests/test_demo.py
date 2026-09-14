import importlib
import io
import json
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv('FIELD_DEMO_DATA', str(tmp_path))
    monkeypatch.setenv('FIELD_ALIYUN_FALLBACK_ENABLED', '0')
    monkeypatch.setenv('FIELD_SAVED_PHOTO_WATCH_ENABLED', '0')
    monkeypatch.setenv('FIELD_VIDEO_OCR_ENABLED', '0')
    monkeypatch.setenv('FIELD_ARCHIVE_ENABLED', '0')
    monkeypatch.setenv('FIELD_PANEL_DETECTOR_ENABLED', '0')
    monkeypatch.delenv('FIELD_ARCHIVE_ROOT', raising=False)
    monkeypatch.delenv('FIELD_ARCHIVE_MOUNT', raising=False)
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    sys.modules.pop('app', None)
    app = importlib.import_module('app')
    # Binding preloads OCR now. All automated tests use a model double, never PaddleOCR.
    class TestOcr:
        def predict(self, panel):
            return []
    monkeypatch.setattr(app, 'create_ocr_model', TestOcr)
    with TestClient(app.app) as client:
        yield app, client
    app.readout_pool.shutdown(wait=True)
    app.ocr_pool.shutdown(wait=True)


def scan(client, name='InstrumentA', camera='TestCamera'):
    path = Path(__file__).parents[1] / 'static' / 'labels' / f'{name}.png'
    result = client.post('/api/scans', files={'file': (path.name, path.read_bytes(), 'image/png')}, data={'camera_id': camera})
    assert result.status_code == 200
    return result.json()


def wait_for_job(client, job_id, timeout=3):
    """Wait for this job's state, independent of request executor concurrency."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get('/api/jobs/' + job_id).json()
        if result['status'] not in {'queued', 'running'}:
            return result
        time.sleep(.01)
    pytest.fail('Readout did not reach a terminal status')


def enter_scene(client, camera='TestCamera', scene_id=None):
    import cv2
    scene = client.get('/api/state').json()['scenes'][0]
    payload = json.dumps({'v': 1, 'type': 'scene', 'id': scene_id or scene['id']}, separators=(',', ':'))
    qr = cv2.QRCodeEncoder_create().encode(payload)
    qr = cv2.copyMakeBorder(qr, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    qr = cv2.resize(qr, None, fx=10, fy=10, interpolation=cv2.INTER_NEAREST)
    _, png = cv2.imencode('.png', qr)
    result = client.post('/api/scans', files={'file': ('scene.png', png.tobytes(), 'image/png')}, data={'camera_id': camera}).json()
    return client.post('/api/scene/enter', json={'scan_id': result['scan_id'], 'scene_id': scene_id or scene['id']})


def register(client, instrument_id, camera='TestCamera'):
    assert client.put(f'/api/instruments/{instrument_id}', json={'name': '测试仪器', 'scene': '湿实验实验台'}).status_code == 200
    assert enter_scene(client, camera).status_code == 200


def test_real_qr_decode_registration_binding_and_evidence(app_client):
    app, client = app_client
    first = scan(client)
    assert first['matches'][0]['id'] == 'e9434a0a-3319-414a-b988-4cc6884edce4'
    body = {'scan_id': first['scan_id'], 'instrument_id': first['matches'][0]['id'], 'operator': '测试员'}
    assert client.post('/api/bindings', json=body).status_code == 409
    register(client, body['instrument_id'])
    binding = client.post('/api/bindings', json=body).json()
    assert binding['instrument']['scene'] == '湿实验实验台'
    assert client.get(first['image_url']).status_code == 200
    assert len(first['image_sha256']) == 64
    second = scan(client, 'InstrumentB')
    second_id = second['matches'][0]['id']
    assert second_id != body['instrument_id']
    register(client, second_id)
    assert client.post('/api/bindings', json=body).json()['binding_id'] == binding['binding_id']
    request2 = body | {'scan_id': second['scan_id'], 'instrument_id': second_id}
    assert client.post('/api/bindings', json=request2).status_code == 200
    client.post('/api/bindings/' + binding['binding_id'] + '/end')
    binding2 = client.post('/api/bindings', json=request2).json()
    exported = client.get('/api/export').json()
    assert next(b for b in exported['bindings'] if b['binding_id'] == binding['binding_id'])['ended_at']
    assert not binding2['ended_at']


def test_cannot_bind_an_instrument_absent_from_scan(app_client):
    _, client = app_client
    first = scan(client)
    result = client.post('/api/bindings', json={'scan_id': first['scan_id'], 'instrument_id': 'eae17924-9fa7-4445-ac45-3987f5687be9', 'operator': 'Test'})
    assert result.status_code == 409


def test_blank_photo_never_creates_instrument_match(app_client):
    _, client = app_client
    out = io.BytesIO()
    Image.new('RGB', (500, 500), 'white').save(out, format='PNG')
    result = client.post('/api/scans', files={'file': ('blank.png', out.getvalue(), 'image/png')}).json()
    assert result['status'] == 'no_qr'
    assert not result['matches']


def test_invalid_media_and_paths(app_client):
    _, client = app_client
    assert client.post('/api/scans', files={'file': ('bad.png', b'not an image', 'image/png')}).status_code == 422
    assert client.get('/api/images/not-a-uuid').status_code == 422
    assert client.get(f'/api/images/{uuid.uuid4()}').status_code == 404


def test_ocr_association_crop_and_async_output(app_client, monkeypatch):
    app, client = app_client
    first = scan(client)
    aid = first['matches'][0]['id']
    register(client, aid)
    binding = client.post('/api/bindings', json={'scan_id': first['scan_id'], 'instrument_id': aid, 'operator': '测试员'}).json()
    request = {'binding_id': binding['binding_id'], 'capture_id': first['capture_id']}
    assert client.post('/api/ocr', json=request | {'crop': [900, 1000, 900, 900]}).status_code == 422
    second = scan(client, 'InstrumentB')
    assert client.post('/api/ocr', json=request | {'capture_id': second['capture_id']}).status_code == 409
    other_camera = scan(client, camera='OtherCamera')
    assert client.post('/api/ocr', json=request | {'capture_id': other_camera['capture_id']}).status_code == 409
    # Only exercise dispatch here; real model validation has a separate receipt.
    submitted = []
    monkeypatch.setattr(app.readout_pool, 'submit', lambda fn, doc: submitted.append(doc))
    result = client.post('/api/ocr', json=request | {'crop': [0, 0, 500, 500]})
    assert result.status_code == 202
    assert len(submitted) == 1
    job = client.get('/api/jobs/' + result.json()['job_id']).json()
    assert job['image_sha256'] == first['image_sha256']
    assert job['status'] == 'queued'
    client.post('/api/bindings/' + binding['binding_id'] + '/end')
    assert client.post('/api/ocr', json=request).status_code == 409


def test_unconfigured_neck_camera_is_explicit(app_client, monkeypatch):
    _, client = app_client
    monkeypatch.delenv('FIELD_CAMERA_SNAPSHOT_URL', raising=False)
    assert client.post('/api/camera/capture').status_code == 409


def test_history_keeps_instrument_scene_snapshot(app_client):
    _, client = app_client
    first = scan(client)
    aid = first['matches'][0]['id']
    register(client, aid)
    client.post('/api/bindings', json={'scan_id': first['scan_id'], 'instrument_id': aid, 'operator': 'Test'})
    client.put('/api/instruments/' + aid, json={'name': '已移动仪器', 'scene': '另一场景'})
    assert client.get('/api/state').json()['bindings'][0]['instrument']['scene'] == '湿实验实验台'
