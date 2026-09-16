import sys
from types import SimpleNamespace

import numpy as np
import pytest

from ocr_runtime import configured_device, create_model, OcrDeviceUnavailable
from test_demo import app_client  # noqa: F401


@pytest.mark.parametrize('value,expected', [(None, 'cpu'), (' CPU ', 'cpu'), ('gpu:0', 'gpu:0'), ('gpu:2', 'gpu:2')])
def test_device_configuration(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv('FIELD_OCR_DEVICE', raising=False)
    else:
        monkeypatch.setenv('FIELD_OCR_DEVICE', value)
    assert configured_device() == expected


@pytest.mark.parametrize('value', ['', 'gpu', 'gpu:-1', 'gpu:0,1', 'auto'])
def test_invalid_device_is_rejected(monkeypatch, value):
    monkeypatch.setenv('FIELD_OCR_DEVICE', value)
    with pytest.raises(ValueError, match='FIELD_OCR_DEVICE'):
        configured_device()


def runtime_double(monkeypatch, *, cuda=True, count=1):
    devices, options = [], []
    monkeypatch.delenv('FLAGS_allocator_strategy', raising=False)
    monkeypatch.setitem(sys.modules, 'paddle', SimpleNamespace(
        is_compiled_with_cuda=lambda: cuda,
        device=SimpleNamespace(cuda=SimpleNamespace(device_count=lambda: count)),
        set_device=devices.append))
    monkeypatch.setitem(sys.modules, 'paddleocr', SimpleNamespace(PaddleOCR=lambda **kw: options.append(kw) or object()))
    return devices, options


def test_gpu_selects_visible_device_and_preserves_models(monkeypatch):
    devices, options = runtime_double(monkeypatch, count=2)
    create_model('gpu:1')
    assert devices == ['gpu:1']
    assert options[0]['device'] == 'gpu:1'
    assert options[0]['text_detection_model_name'] == 'PP-OCRv5_mobile_det'
    assert options[0]['text_recognition_model_name'] == 'PP-OCRv5_mobile_rec'
    import os
    assert os.environ['FLAGS_allocator_strategy'] == 'auto_growth'


@pytest.mark.parametrize('cuda,count', [(False, 1), (True, 0)])
def test_missing_gpu_never_constructs_cpu_model(monkeypatch, cuda, count):
    devices, options = runtime_double(monkeypatch, cuda=cuda, count=count)
    with pytest.raises(OcrDeviceUnavailable):
        create_model('gpu:0')
    assert not devices and not options


def test_explicit_cpu_does_not_require_cuda(monkeypatch):
    devices, options = runtime_double(monkeypatch, cuda=False, count=0)
    create_model('cpu')
    assert not devices
    assert options[0]['device'] == 'cpu'


def test_gpu_result_and_failure_keep_device_and_resident_contract(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setattr(app, 'OCR_DEVICE', 'gpu:0')
    app.ocr_state['device'] = 'gpu:0'
    for _ in range(2):
        result = app.predict_panel(np.zeros((32, 32, 3), dtype=np.uint8))
        assert result['device'] == 'gpu:0' and result['status'] == 'completed'
    state = client.get('/api/state').json()['ocr']
    assert state['device'] == 'gpu:0' and state['resident'] and state['load_count'] == 1

    app.ocr_model = None
    def fail():
        raise OcrDeviceUnavailable('Configured OCR device gpu:0 is not visible to Paddle')
    monkeypatch.setattr(app, 'create_ocr_model', fail)
    result = app.predict_panel(np.zeros((32, 32, 3), dtype=np.uint8))
    assert result['device'] == 'gpu:0' and result['status'] == 'failed'
    assert not result['actual_model_invocation']
    state = client.get('/api/state').json()['ocr']
    assert not state['resident'] and state['error'] == 'OcrDeviceUnavailable'
    assert 'not visible' in state['error_detail']


def test_offline_cached_models_are_passed_as_explicit_directories(tmp_path, monkeypatch):
    monkeypatch.setenv('PADDLE_PDX_CACHE_HOME', str(tmp_path))
    for suffix in ('det', 'rec'):
        path = tmp_path / 'official_models' / f'PP-OCRv5_mobile_{suffix}'
        path.mkdir(parents=True)
        for name in ('inference.json', 'inference.pdiparams', 'inference.yml'):
            (path / name).write_bytes(b'local-weight-fixture')
    _, options = runtime_double(monkeypatch)
    create_model('gpu:0')
    assert options[0]['text_detection_model_dir'] == str(tmp_path / 'official_models/PP-OCRv5_mobile_det')
    assert options[0]['text_recognition_model_dir'] == str(tmp_path / 'official_models/PP-OCRv5_mobile_rec')


def test_explicit_incomplete_model_never_downloads_silently(tmp_path, monkeypatch):
    from ocr_runtime import OcrModelUnavailable
    monkeypatch.setenv('FIELD_OCR_DET_MODEL_DIR', str(tmp_path))
    _, options = runtime_double(monkeypatch)
    with pytest.raises(OcrModelUnavailable):
        create_model('gpu:0')
    assert not options


def test_load_failure_backs_off_then_recovers(app_client, monkeypatch):
    app, _ = app_client
    clock = [1000.0]
    monkeypatch.setattr(app.time, 'monotonic', lambda: clock[0])
    calls = []
    def fail():
        calls.append(1)
        raise OcrDeviceUnavailable('offline')
    monkeypatch.setattr(app, 'create_ocr_model', fail)
    assert not app.warm_ocr()
    assert not app.warm_ocr()
    assert len(calls) == 1 and app.ocr_state['retry_after_seconds'] == 5
    clock[0] += 5
    assert not app.warm_ocr()
    assert len(calls) == 2 and app.ocr_state['retry_after_seconds'] == 10
    clock[0] += 10
    monkeypatch.setattr(app, 'create_ocr_model', object)
    assert app.warm_ocr()
    assert app.ocr_state['resident'] and app.ocr_state['load_failures'] == 0
