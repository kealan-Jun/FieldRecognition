from tests.support.aliyun_vision import enabled, response  # noqa: F401
import base64
import json

import cv2
import httpx
import numpy as np
import pytest

import field_recognition.recognition.aliyun_vision as vision
from field_recognition.recognition.config import RecognitionConfig


@pytest.mark.parametrize("thinking", [None, "0", "1"])
def test_flash_request_uses_snapshot_thinking_mode_and_retains_reading(monkeypatch, thinking):
    environment = {
        "FIELD_VISION_PROVIDER": "aliyun",
        "FIELD_ALIYUN_FALLBACK_ENABLED": "1",
        "DASHSCOPE_API_KEY": "test-only-secret",
        "FIELD_ALIYUN_MODEL": "qwen3.8-flash",
        "FIELD_VISION_PHOTO_MODE": "primary",
    }
    if thinking is not None:
        environment["FIELD_ALIYUN_ENABLE_THINKING"] = thinking
    config = RecognitionConfig.from_environment(environment)
    monkeypatch.setenv("FIELD_ALIYUN_ENABLE_THINKING", "1" if thinking == "0" else "0")
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == vision.DEFAULT_BASE_URL + "/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-only-secret"
        return httpx.Response(
            200,
            json=response(
                {
                    "status": "readable",
                    "readings": [{"text": "-0.0123 g", "value": "-0.0123", "unit": "g"}],
                }
            )
            | {"model": "qwen3.8-flash"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            httpx,
            "stream",
            lambda method, url, **kw: client.stream(
                method, url, headers=kw["headers"], json=kw["json"]
            ),
        )
        result = vision.read_panel(np.zeros((40, 50, 3), np.uint8), config=config)

    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    expected = thinking != "0"
    assert payload["enable_thinking"] is expected
    assert ("thinking_budget" in payload) is expected
    assert payload["max_completion_tokens"] == 4096
    assert payload["model"] == "qwen3.8-flash"
    assert result["status"] == "completed"
    assert result["answer"]["readings"][0]["value"] == "-0.0123"
    assert result["enable_thinking"] is expected
    assert result["returned_model"] == "qwen3.8-flash"
    assert result["human_verified"] is False
    assert "test-only-secret" not in json.dumps(result)


def test_cloud_receives_only_exact_panel_and_structured_readout(enabled, monkeypatch):
    panel = np.full((120, 230, 3), 160, np.uint8)
    sent = []

    def request(payload, key):
        sent.append(payload)
        assert key == "test-only-secret"
        assert "test-only-secret" not in json.dumps(payload)
        image_url = payload["messages"][1]["content"][0]["image_url"]["url"]
        image = cv2.imdecode(
            np.frombuffer(base64.b64decode(image_url.split(",")[1]), np.uint8), cv2.IMREAD_COLOR
        )
        assert np.array_equal(panel, image)
        return response(
            {
                "status": "readable",
                "readings": [{"text": "12.34 g", "value": "12.34", "label": None, "unit": "g"}],
                "notes": "",
            }
        )

    monkeypatch.setattr(vision, "_request", request)
    result = vision.read_panel(panel)
    assert result["status"] == "completed" and result["actual_model_invocation"]
    assert result["answer"]["readings"][0]["value"] == "12.34"
    assert len(sent) == 1 and sent[0]["model"] == "qwen3.8-max"
    assert not any(k in json.dumps(sent) for k in ["camera_id", "binding_id", "operator"])


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "unreadable", "readings": [{"text": "12", "value": "12"}]},
        {"status": "readable", "readings": [{"text": "12?", "value": "123"}]},
        {"status": "readable", "readings": [{"text": "12?", "value": "12?"}]},
        {"status": "readable", "readings": []},
    ],
)
def test_inconsistent_or_guessed_structure_is_rejected(enabled, monkeypatch, answer):
    monkeypatch.setattr(vision, "_request", lambda *a: response(answer))
    result = vision.read_panel(np.zeros((80, 100, 3), np.uint8))
    assert result["status"] == "failed" and result["error"] == "invalid_answer"
    assert "answer" not in result


def test_unreadable_cloud_result_does_not_invent_a_readout(enabled, monkeypatch):
    monkeypatch.setattr(
        vision,
        "_request",
        lambda *a: response({"status": "unreadable", "readings": [], "notes": "画面模糊"}),
    )
    result = vision.read_panel(np.zeros((80, 100, 3), np.uint8))
    assert result["answer"]["readings"] == []


def test_missing_key_or_unapproved_endpoint_never_sends_image(enabled, monkeypatch):
    def reject(*a, **kw):
        raise AssertionError("Unexpected network request")

    monkeypatch.setattr(vision, "_request", reject)
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    assert vision.read_panel(np.zeros((80, 100, 3), np.uint8))["error"] == "missing_api_key"
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-secret")
    monkeypatch.setenv("FIELD_ALIYUN_BASE_URL", "https://unapproved.invalid/compatible-mode/v1")
    assert vision.read_panel(np.zeros((80, 100, 3), np.uint8))["error"] == "invalid_aliyun_endpoint"


@pytest.mark.parametrize(
    "status,code",
    [(401, "authentication_failed"), (429, "rate_or_quota_limit"), (500, "provider_http_error")],
)
def test_http_error_is_redacted_and_never_retried(enabled, monkeypatch, status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "test-only-secret"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            httpx,
            "stream",
            lambda method, url, **kw: client.stream(
                method, url, headers=kw["headers"], json=kw["json"]
            ),
        )
        result = vision.read_panel(np.zeros((80, 100, 3), np.uint8))
    assert result["error"] == code and result["http_status"] == status
    assert len(calls) == 1
    assert "test-only-secret" not in json.dumps(result)


def test_incomplete_reply_is_not_a_reading(enabled, monkeypatch):
    partial = response({"status": "unreadable", "readings": []})
    partial["choices"][0]["finish_reason"] = "length"
    monkeypatch.setattr(vision, "_request", lambda *a: partial)
    result = vision.read_panel(np.zeros((80, 100, 3), np.uint8))
    assert result["error"] == "incomplete_answer"


def test_arrearage_has_actionable_code_without_provider_message(enabled, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            400, json={"error": {"code": "Arrearage", "message": "test-only-secret"}}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            httpx,
            "stream",
            lambda method, url, **kw: client.stream(
                method, url, headers=kw["headers"], json=kw["json"]
            ),
        )
        result = vision.read_panel(np.zeros((80, 100, 3), np.uint8))
    assert result["error"] == "account_arrearage" and result["provider_code"] == "Arrearage"
    assert result["http_status"] == 400 and not result["actual_model_invocation"]
    assert len(calls) == 1 and "test-only-secret" not in json.dumps(result)
