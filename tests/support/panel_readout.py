"""Reusable panel_readout fixtures and doubles."""

from concurrent.futures import Future

import pytest

import field_recognition.recognition.aliyun_vision as vision
import field_recognition.recognition.panel_readout as panel_readout
from field_recognition.runtime.production_config import ProductionConfig
from tests.support.demo import app_client, register, scan  # noqa: F401


def refresh_recognition_config(app):
    """Test settings change explicitly; initialized runtimes do not reread env."""
    app.config = ProductionConfig.from_environment()


def local_result(text=None):
    return {
        "status": "completed",
        "lines": []
        if text is None
        else [
            {
                "text": text,
                "confidence": 0.1,
                "polygon": [[1, 2], [3, 2], [3, 4], [1, 4]],
                "numeric_candidates": ["12.34"],
            }
        ],
        "model": "test-ocr",
        "device": "cpu",
        "actual_model_invocation": True,
        "wall_seconds": 0.01,
    }


def cloud_result():
    return {
        "status": "completed",
        "attempted": True,
        "actual_model_invocation": True,
        "model": "qwen3.8-max",
        "wall_seconds": 0.01,
        "answer": {
            "status": "readable",
            "readings": [{"text": "12.34 g", "value": "12.34", "unit": "g"}],
        },
    }


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
    aid = picture["matches"][0]["id"]
    register(client, aid)
    binding = client.post(
        "/api/bindings",
        json={"scan_id": picture["scan_id"], "instrument_id": aid, "operator": "Test"},
    ).json()
    assert app.ocr_warmup_future.result(timeout=2)
    queued = []
    monkeypatch.setattr(app.readout_pool, "submit", lambda fn, doc: queued.append(doc))
    args = {
        "binding_id": binding["binding_id"],
        "capture_id": picture["capture_id"],
        "crop": [10, 20, 160, 140],
    }
    job = client.post("/api/ocr", json=args).json()
    monkeypatch.setenv("FIELD_ALIYUN_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-secret")
    monkeypatch.setenv("FIELD_ALIYUN_NO_DIGITS_SECONDS", "5")
    refresh_recognition_config(app)
    monkeypatch.setattr(
        vision, "read_panel", lambda panel, **kwargs: pytest.fail("Unexpected cloud invocation")
    )
    return app, client, job, args


def execute(app, job, local_future, clock):
    class Cpu:
        def submit(self, function, panel, x, y):
            assert panel.shape[:2] == (140, 160)
            return local_future

    panel_readout.run(vars(app) | {"ocr_pool": Cpu()}, job, clock=clock, pause=clock.pause)
    return app.get_job(job["job_id"])


def complete(value):
    future = Future()
    future.set_result(value)
    return future
