import base64
import json

import cv2
import httpx
import numpy as np
import pytest

import aliyun_vision as vision


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv('FIELD_ALIYUN_FALLBACK_ENABLED', '1')
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test-only-secret')
    monkeypatch.setenv('FIELD_ALIYUN_MODEL', 'qwen3.8-max')
    monkeypatch.setenv('FIELD_ALIYUN_BASE_URL', vision.DEFAULT_BASE_URL)
    monkeypatch.setenv('FIELD_ALIYUN_NO_DIGITS_SECONDS', '5')


def response(answer):
    return {'id': 'test-request', 'model': 'qwen3.8-max', 'usage': {'total_tokens': 20},
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]}


def test_cloud_receives_only_exact_panel_and_structured_readout(enabled, monkeypatch):
    panel = np.full((120, 230, 3), 160, np.uint8)
    sent = []

    def request(payload, key):
        sent.append(payload)
        assert key == 'test-only-secret'
        assert 'test-only-secret' not in json.dumps(payload)
        image_url = payload['messages'][1]['content'][0]['image_url']['url']
        image = cv2.imdecode(np.frombuffer(base64.b64decode(image_url.split(',')[1]), np.uint8), cv2.IMREAD_COLOR)
        assert np.array_equal(panel, image)
        return response({'status': 'readable', 'readings': [{'text': '12.34 g', 'value': '12.34', 'label': None, 'unit': 'g'}], 'notes': ''})

    monkeypatch.setattr(vision, '_request', request)
    result = vision.read_panel(panel)
    assert result['status'] == 'completed' and result['actual_model_invocation']
    assert result['answer']['readings'][0]['value'] == '12.34'
    assert len(sent) == 1 and sent[0]['model'] == 'qwen3.8-max'
    assert not any(k in json.dumps(sent) for k in ['camera_id', 'binding_id', 'operator'])


@pytest.mark.parametrize('answer', [
    {'status': 'unreadable', 'readings': [{'text': '12', 'value': '12'}]},
    {'status': 'readable', 'readings': [{'text': '12?', 'value': '123'}]},
    {'status': 'readable', 'readings': [{'text': '12?', 'value': '12?'}]},
    {'status': 'readable', 'readings': []},
])
def test_inconsistent_or_guessed_structure_is_rejected(enabled, monkeypatch, answer):
    monkeypatch.setattr(vision, '_request', lambda *a: response(answer))
    result = vision.read_panel(np.zeros((80, 100, 3), np.uint8))
    assert result['status'] == 'failed' and result['error'] == 'invalid_answer'
    assert 'answer' not in result


def test_unreadable_cloud_result_does_not_invent_a_readout(enabled, monkeypatch):
    monkeypatch.setattr(vision, '_request', lambda *a: response({'status': 'unreadable', 'readings': [], 'notes': '画面模糊'}))
    result = vision.read_panel(np.zeros((80, 100, 3), np.uint8))
    assert result['answer']['readings'] == []


def test_missing_key_or_unapproved_endpoint_never_sends_image(enabled, monkeypatch):
    def reject(*a, **kw):
        raise AssertionError('Unexpected network request')
    monkeypatch.setattr(vision, '_request', reject)
    monkeypatch.delenv('DASHSCOPE_API_KEY')
    assert vision.read_panel(np.zeros((80, 100, 3), np.uint8))['error'] == 'missing_api_key'
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test-only-secret')
    monkeypatch.setenv('FIELD_ALIYUN_BASE_URL', 'https://unapproved.invalid/compatible-mode/v1')
    assert vision.read_panel(np.zeros((80, 100, 3), np.uint8))['error'] == 'invalid_aliyun_endpoint'


@pytest.mark.parametrize('status,code', [(401, 'authentication_failed'), (429, 'rate_or_quota_limit'), (500, 'provider_http_error')])
def test_http_error_is_redacted_and_never_retried(enabled, monkeypatch, status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {'message': 'test-only-secret'}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(httpx, 'stream', lambda method, url, **kw: client.stream(method, url, headers=kw['headers'], json=kw['json']))
        result = vision.read_panel(np.zeros((80, 100, 3), np.uint8))
    assert result['error'] == code and result['http_status'] == status
    assert len(calls) == 1
    assert 'test-only-secret' not in json.dumps(result)


def test_incomplete_reply_is_not_a_reading(enabled, monkeypatch):
    partial = response({'status': 'unreadable', 'readings': []})
    partial['choices'][0]['finish_reason'] = 'length'
    monkeypatch.setattr(vision, '_request', lambda *a: partial)
    result = vision.read_panel(np.zeros((80, 100, 3), np.uint8))
    assert result['error'] == 'incomplete_answer'


def test_arrearage_has_actionable_code_without_provider_message(enabled, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={'error': {'code': 'Arrearage', 'message': 'test-only-secret'}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(httpx, 'stream', lambda method, url, **kw: client.stream(method, url, headers=kw['headers'], json=kw['json']))
        result = vision.read_panel(np.zeros((80, 100, 3), np.uint8))
    assert result['error'] == 'account_arrearage' and result['provider_code'] == 'Arrearage'
    assert result['http_status'] == 400 and not result['actual_model_invocation']
    assert len(calls) == 1 and 'test-only-secret' not in json.dumps(result)
