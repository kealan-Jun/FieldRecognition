"""Event-gated concurrent readers using only temporary photos, SQLite and writers."""

import copy
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from field_recognition.recognition import aliyun_vision, panel_readout
from tests.support.demo import app_client  # noqa: F401
from tests.support.panel_readout import refresh_recognition_config
from tests.support.unbound_readout import photo


@pytest.fixture
def parallel_context(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setenv("FIELD_VISION_PHOTO_MODE", "parallel")
    monkeypatch.setenv("FIELD_ALIYUN_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-test-key")
    refresh_recognition_config(app)
    monkeypatch.setattr(app.readout_pool, "submit", lambda *args: None)
    monkeypatch.setattr(
        aliyun_vision, "read_panel", lambda *a, **k: pytest.fail("Unmocked provider request")
    )
    return app, client


def enqueue(context, ident="parallel-photo"):
    app, _ = context
    return app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app, ident=ident)), trigger="voice_photo_directory"
    )


@contextmanager
def running(app, job, *gates):
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(panel_readout.run, vars(app), copy.deepcopy(job))
        try:
            yield future
        finally:
            for gate in gates:
                gate.set()
            future.result(timeout=5)


def observe(client, job_id, predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = client.get("/api/jobs/" + job_id).json()
        if predicate(result):
            return result
        time.sleep(0.01)
    pytest.fail("Expected reader state was never published")
