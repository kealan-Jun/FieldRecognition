from test_demo import wait_for_job
import importlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from test_demo import app_client, register, scan  # noqa: F401


def binding_request(client):
    picture = scan(client)
    aid = picture['matches'][0]['id']
    register(client, aid)
    return {'scan_id': picture['scan_id'], 'instrument_id': aid, 'operator': 'ResidentTester'}


def test_failed_binding_does_not_load_ocr(app_client):
    app, client = app_client
    picture = scan(client)
    response = client.post('/api/bindings', json={
        'scan_id': picture['scan_id'], 'instrument_id': picture['matches'][0]['id'], 'operator': 'Test'})
    assert response.status_code == 409
    assert app.ocr_warmup_future is None
    assert client.get('/api/state').json()['ocr']['resident'] is False


def test_binding_loads_in_background_once_and_predictions_reuse_resident_model(app_client, monkeypatch):
    app, client = app_client
    started, release = threading.Event(), threading.Event()
    constructions, predictions = [], []

    class Model:
        def predict(self, panel):
            predictions.append(panel.shape)
            return []

    model = Model()

    def create():
        constructions.append(1)
        started.set()
        assert release.wait(5)
        return model

    monkeypatch.setattr(app, 'create_ocr_model', create)
    args = binding_request(client)
    try:
        response = client.post('/api/bindings', json=args)
        assert response.status_code == 200  # Returns while construction is still blocked.
        binding = response.json()
        assert started.wait(2)
        state = client.get('/api/state').json()
        assert state['ocr']['status'] == 'loading' and state['ocr']['resident'] is False
        assert state['bindings'][0]['binding_id'] == binding['binding_id']
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = list(pool.map(lambda _: app.queue_ocr_warmup(), range(12)))
        assert all(f is app.ocr_warmup_future for f in futures)
        assert client.post('/api/bindings', json=args).json()['binding_id'] == binding['binding_id']
        assert len(constructions) == 1
    finally:
        release.set()
    assert app.ocr_warmup_future.result(timeout=2)
    assert app.ocr_model is model
    ready = client.get('/api/state').json()['ocr']
    assert ready['status'] == 'ready' and ready['resident'] and ready['load_count'] == 1
    assert ready['loaded_at']
    assert not predictions  # Preloading does not submit any panel-reading job.
    assert client.get('/api/state').json()['jobs'] == []

    for _ in range(2):
        job = client.post('/api/ocr', json={'binding_id': binding['binding_id'], 'capture_id': args['scan_id']}).json()
        wait_for_job(client, job['job_id'])
        assert client.get('/api/jobs/' + job['job_id']).json()['status'] == 'completed'
    assert len(predictions) == 2 and len(constructions) == 1
    client.post('/api/bindings/' + binding['binding_id'] + '/end')
    next_binding = client.post('/api/bindings', json=args).json()
    assert next_binding['binding_id'] != binding['binding_id']
    assert app.ocr_model is model and len(constructions) == 1


def test_preload_failure_keeps_binding_and_can_retry_without_duplicate(app_client, monkeypatch):
    app, client = app_client
    attempts = []
    model = object()

    def create():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('test load failure')
        return model

    monkeypatch.setattr(app, 'create_ocr_model', create)
    args = binding_request(client)
    binding = client.post('/api/bindings', json=args).json()
    assert app.ocr_warmup_future.result(timeout=2) is False
    state = client.get('/api/state').json()
    assert state['ocr']['status'] == 'error' and not state['ocr']['resident']
    assert state['bindings'][0]['ended_at'] is None
    assert state['ocr']['retry_after_seconds'] == 5
    monkeypatch.setattr(app, 'ocr_retry_at', 0)  # retry after the bounded backoff
    assert client.post('/api/bindings', json=args).json()['binding_id'] == binding['binding_id']
    assert app.ocr_warmup_future.result(timeout=2)
    assert app.ocr_model is model and len(attempts) == 2
    assert len(client.get('/api/state').json()['bindings']) == 1


def test_local_service_start_reloads_model_for_existing_binding(app_client, monkeypatch):
    app, client = app_client
    binding = client.post('/api/bindings', json=binding_request(client)).json()
    assert app.ocr_warmup_future.result(timeout=2)
    app.ocr_pool.shutdown(wait=True)
    sys.modules.pop('app', None)
    restarted = importlib.import_module('app')
    model = object()
    monkeypatch.setattr(restarted, 'create_ocr_model', lambda: model)
    with TestClient(restarted.app) as new_client:
        assert restarted.ocr_warmup_future.result(timeout=2)
        state = new_client.get('/api/state').json()
        assert state['bindings'][0]['binding_id'] == binding['binding_id']
        assert state['ocr']['resident'] and state['ocr']['load_count'] == 1
        assert restarted.ocr_model is model
