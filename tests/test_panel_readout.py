from concurrent.futures import Future

import cv2
import numpy as np
import pytest

import aliyun_vision as vision
import panel_readout
from test_demo import app_client, register, scan  # noqa: F401


def local_result(text=None):
    return {'status': 'completed', 'lines': [] if text is None else [
        {'text': text, 'confidence': .1, 'polygon': [[1, 2], [3, 2], [3, 4], [1, 4]],
         'numeric_candidates': ['12.34']}], 'model': 'test-ocr', 'device': 'cpu',
        'actual_model_invocation': True, 'wall_seconds': .01}


def cloud_result():
    return {'status': 'completed', 'attempted': True, 'actual_model_invocation': True,
            'model': 'qwen3.8-max', 'wall_seconds': .01, 'answer': {
                'status': 'readable', 'readings': [{'text': '12.34 g', 'value': '12.34', 'unit': 'g'}]}}


class Clock:
    def __init__(self):
        self.value = 0
        self.on_pause = None

    def __call__(self):
        return self.value

    def pause(self, duration):
        self.value += duration
        if self.on_pause:
            self.on_pause(self.value)


@pytest.fixture
def job_context(app_client, monkeypatch):
    app, client = app_client
    picture = scan(client)
    aid = picture['matches'][0]['id']
    register(client, aid)
    binding = client.post('/api/bindings', json={'scan_id': picture['scan_id'], 'instrument_id': aid, 'operator': 'Test'}).json()
    assert app.ocr_warmup_future.result(timeout=2)
    queued = []
    monkeypatch.setattr(app.readout_pool, 'submit', lambda fn, doc: queued.append(doc))
    args = {'binding_id': binding['binding_id'], 'capture_id': picture['capture_id'], 'crop': [10, 20, 160, 140]}
    job = client.post('/api/ocr', json=args).json()
    monkeypatch.setenv('FIELD_ALIYUN_FALLBACK_ENABLED', '1')
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test-only-secret')
    monkeypatch.setenv('FIELD_ALIYUN_NO_DIGITS_SECONDS', '5')
    monkeypatch.setattr(vision, 'read_panel', lambda panel: pytest.fail('Unexpected cloud invocation'))
    return app, client, job, args


def execute(app, job, local_future, clock):
    class Cpu:
        def submit(self, function, panel, x, y):
            assert panel.shape[:2] == (140, 160)
            return local_future
    panel_readout.run(vars(app) | {'ocr_pool': Cpu()}, job, clock=clock, pause=clock.pause)
    return app.get_job(job['job_id'])


def complete(value):
    future = Future()
    future.set_result(value)
    return future


def test_readout_returns_digits_without_cloud_even_at_low_score(job_context):
    app, _, job, _ = job_context
    clock = Clock()
    result = execute(app, job, complete(local_result('12.34 g')), clock)
    assert result['status'] == 'completed' and clock.value == 0
    assert result['lines'][0]['text'] == '12.34 g'
    assert 'fallback' not in result


def test_empty_ocr_waits_five_seconds_and_sends_same_crop_once(job_context, monkeypatch):
    app, _, job, _ = job_context
    clock, calls = Clock(), []
    original = cv2.imread(str(app.DATA / 'Images' / (job['capture_id'] + '.png')))
    def cloud(panel):
        assert clock.value >= 5
        assert np.array_equal(panel, original[20:160, 10:170])
        calls.append(1)
        return cloud_result()
    monkeypatch.setattr(vision, 'read_panel', cloud)
    result = execute(app, job, complete(local_result()), clock)
    assert result['status'] == 'completed' and result['lines'][0]['text'] == '12.34 g'
    assert result['lines'][0]['confidence'] is None and result['lines'][0]['polygon'] is None
    assert result['local_ocr']['lines'] == [] and len(calls) == 1
    assert result['fallback']['trigger_elapsed_seconds'] == 5
    assert result['image_sha256'] == job['image_sha256'] and len(result['crop_image_sha256']) == 64


def test_cpu_recovery_before_deadline_prevents_fallback(job_context):
    app, _, job, _ = job_context
    future, clock = Future(), Clock()
    def recover(value):
        if value >= 4 and not future.done():
            future.set_result(local_result('12.34 g'))
    clock.on_pause = recover
    result = execute(app, job, future, clock)
    assert 4 <= clock.value < 5
    assert result['status'] == 'completed' and 'fallback' not in result


def test_slow_cpu_does_not_block_fallback_and_late_result_is_audited(job_context, monkeypatch):
    app, _, job, _ = job_context
    future = Future()
    future.set_running_or_notify_cancel()
    monkeypatch.setattr(vision, 'read_panel', lambda panel: cloud_result())
    result = execute(app, job, future, Clock())
    assert result['fallback']['trigger'] == 'local_ocr_timeout'
    assert result['lines'][0]['text'] == '12.34 g'
    future.set_result(local_result('99.9 g'))
    audited = app.get_job(job['job_id'])
    assert audited['local_ocr_finished_late']
    assert audited['local_ocr']['lines'][0]['text'] == '99.9 g'
    assert audited['lines'][0]['text'] == '12.34 g'


def test_binding_end_during_wait_cancels_without_cloud(job_context):
    app, client, job, args = job_context
    clock = Clock()
    def end(value):
        if value >= 2:
            client.post('/api/bindings/' + args['binding_id'] + '/end')
    clock.on_pause = end
    result = execute(app, job, complete(local_result()), clock)
    assert result['status'] == 'cancelled' and result['lines'] == []
    assert 'fallback' not in result


def test_binding_end_during_cloud_keeps_receipt_but_does_not_publish_reading(job_context, monkeypatch):
    app, client, job, args = job_context
    def cloud(panel):
        client.post('/api/bindings/' + args['binding_id'] + '/end')
        return cloud_result()
    monkeypatch.setattr(vision, 'read_panel', cloud)
    result = execute(app, job, complete(local_result()), Clock())
    assert result['status'] == 'cancelled' and result['lines'] == []
    assert result['fallback']['answer']['readings'][0]['value'] == '12.34'


def test_failure_has_no_invented_reading_and_cooldown_survives_new_request(job_context, monkeypatch):
    app, client, job, args = job_context
    calls = []
    def cloud(panel):
        calls.append(1)
        return {'status': 'failed', 'error': 'rate_or_quota_limit', 'attempted': True}
    monkeypatch.setattr(vision, 'read_panel', cloud)
    first = execute(app, job, complete(local_result()), Clock())
    second = client.post('/api/ocr', json=args).json()
    second = execute(app, second, complete(local_result()), Clock())
    assert first['lines'] == second['lines'] == [] and len(calls) == 1
    assert second['fallback']['reason'] == 'cooldown'


def test_busy_vision_slot_does_not_reserve_or_invoke(job_context, monkeypatch):
    app, _, job, _ = job_context
    monkeypatch.setattr(app, 'reserve_fallback', lambda scope: pytest.fail('Busy slot must not consume cooldown'))
    with app.vision_call_lock:
        result = execute(app, job, complete(local_result()), Clock())
    assert result['fallback'] == {'status': 'skipped', 'reason': 'busy'}
    assert result['local_ocr']['actual_model_invocation']


def test_next_photo_finishes_while_previous_photo_waits_for_cloud(app_client, monkeypatch):
    """Prove scheduling with gates, not a timing benchmark or real OCR call."""
    import threading
    app, client = app_client
    capture = scan(client)
    entered, release, second_finished = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setenv('FIELD_ALIYUN_FALLBACK_ENABLED', '1')
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test-only-secret')
    monkeypatch.setenv('FIELD_ALIYUN_NO_DIGITS_SECONDS', '.1')
    monkeypatch.setattr(app, 'predict_panel', lambda panel, x, y: local_result('12.34 g' if x else None))
    original_run = app.run_ocr
    def run(job):
        original_run(job)
        if job['crop'][0] == 1:
            second_finished.set()
    monkeypatch.setattr(app, 'run_ocr', run)
    calls = []
    def cloud(panel):
        calls.append(1); entered.set()
        assert release.wait(3)
        return cloud_result()
    monkeypatch.setattr(vision, 'read_panel', cloud)
    try:
        first = client.post('/api/ocr', json={'capture_id': capture['capture_id'], 'crop': [0, 0, 100, 100]}).json()
        assert entered.wait(2)
        second = client.post('/api/ocr', json={'capture_id': capture['capture_id'], 'crop': [1, 0, 100, 100]}).json()
        assert second_finished.wait(2), 'Another photo must not wait for the first vision response'
        result = app.get_job(second['job_id'])
        assert result['lines'][0]['text'] == '12.34 g' and 'fallback' not in result
        assert app.get_job(first['job_id'])['status'] == 'running'
        assert len(calls) == 1
    finally:
        release.set()
        app.readout_pool.shutdown(wait=True)


def test_parallel_requests_keep_single_ocr_worker_and_four_job_limit(app_client, monkeypatch):
    import threading
    app, client = app_client
    capture = scan(client)
    entered, release = threading.Event(), threading.Event()
    guard, active, peak = threading.Lock(), 0, 0
    def predict(panel, x, y):
        nonlocal active, peak
        with guard:
            active += 1; peak = max(peak, active)
        entered.set()
        try:
            assert release.wait(3)
            return local_result('12.34 g')
        finally:
            with guard:
                active -= 1
    monkeypatch.setattr(app, 'predict_panel', predict)
    try:
        jobs = [client.post('/api/ocr', json={'capture_id': capture['capture_id'], 'crop': [x, 0, 100, 100]}) for x in range(4)]
        assert all(response.status_code == 202 for response in jobs)
        assert entered.wait(1)
        assert client.post('/api/ocr', json={'capture_id': capture['capture_id'], 'crop': [4, 0, 100, 100]}).status_code == 429
    finally:
        release.set()
        app.readout_pool.shutdown(wait=True)
    assert peak == 1
    assert all(app.get_job(response.json()['job_id'])['status'] == 'completed' for response in jobs)
