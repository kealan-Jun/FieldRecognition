"""Reusable aliyun_vision fixtures and doubles."""

import json

import pytest

import field_recognition.recognition.aliyun_vision as vision


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("FIELD_ALIYUN_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-secret")
    monkeypatch.setenv("FIELD_ALIYUN_MODEL", "qwen3.8-max")
    monkeypatch.setenv("FIELD_ALIYUN_BASE_URL", vision.DEFAULT_BASE_URL)
    monkeypatch.setenv("FIELD_ALIYUN_NO_DIGITS_SECONDS", "5")


def response(answer):
    return {
        "id": "test-request",
        "model": "qwen3.8-max",
        "usage": {"total_tokens": 20},
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
    }
