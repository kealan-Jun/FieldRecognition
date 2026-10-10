"""Reusable demo fixtures and doubles."""

import importlib
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

from field_recognition.runtime.paths import PROJECT_ROOT


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("FIELD_DEMO_DATA", str(tmp_path))
    monkeypatch.setenv("FIELD_ALIYUN_FALLBACK_ENABLED", "0")
    monkeypatch.setenv("FIELD_SAVED_PHOTO_WATCH_ENABLED", "0")
    monkeypatch.setenv("FIELD_VIDEO_OCR_ENABLED", "0")
    monkeypatch.setenv("FIELD_ARCHIVE_ENABLED", "0")
    monkeypatch.setenv("FIELD_PANEL_DETECTOR_ENABLED", "0")
    monkeypatch.delenv("FIELD_ARCHIVE_ROOT", raising=False)
    monkeypatch.delenv("FIELD_ARCHIVE_MOUNT", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    sys.modules.pop("app", None)
    app = importlib.import_module("app")

    # Binding preloads OCR now. All automated tests use a model double, never PaddleOCR.
    class TestOcr:
        def predict(self, panel):
            return []

    monkeypatch.setattr(app, "create_ocr_model", TestOcr)
    with TestClient(app.app) as client:
        yield app, client
    app.readout_pool.shutdown(wait=True)
    app.ocr_pool.shutdown(wait=True)


def scan(client, name="InstrumentA", camera="TestCamera"):
    path = PROJECT_ROOT / "static" / "labels" / f"{name}.png"
    result = client.post(
        "/api/scans",
        files={"file": (path.name, path.read_bytes(), "image/png")},
        data={"camera_id": camera},
    )
    assert result.status_code == 200
    return result.json()


def wait_for_job(client, job_id, timeout=3):
    """Wait for this job's state, independent of request executor concurrency."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get("/api/jobs/" + job_id).json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.01)
    pytest.fail("Readout did not reach a terminal status")


def enter_scene(client, camera="TestCamera", scene_id=None):
    import cv2

    scene = client.get("/api/state").json()["scenes"][0]
    payload = json.dumps(
        {"v": 1, "type": "scene", "id": scene_id or scene["id"]}, separators=(",", ":")
    )
    qr = cv2.QRCodeEncoder_create().encode(payload)
    qr = cv2.copyMakeBorder(qr, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    qr = cv2.resize(qr, None, fx=10, fy=10, interpolation=cv2.INTER_NEAREST)
    _, png = cv2.imencode(".png", qr)
    result = client.post(
        "/api/scans",
        files={"file": ("scene.png", png.tobytes(), "image/png")},
        data={"camera_id": camera},
    ).json()
    return client.post(
        "/api/scene/enter", json={"scan_id": result["scan_id"], "scene_id": scene_id or scene["id"]}
    )


def register(client, instrument_id, camera="TestCamera"):
    assert (
        client.put(
            f"/api/instruments/{instrument_id}", json={"name": "测试仪器", "scene": "湿实验实验台"}
        ).status_code
        == 200
    )
    assert enter_scene(client, camera).status_code == 200
