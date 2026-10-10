"""Reusable live_scan fixtures and doubles."""

import threading
import time

import cv2
import pytest

from tests.support.demo import app_client, enter_scene, register, scan  # noqa: F401


class Camera:
    target = "TestCamera"

    def __init__(self):
        self.lock = threading.Lock()
        self.sequence = 0
        self.pixels = None
        self.offline = False

    def publish(self, pixels):
        with self.lock:
            self.sequence += 1
            self.pixels = pixels
            self.offline = False

    def frame(self):
        with self.lock:
            if self.pixels is None or self.offline:
                raise ValueError("offline or stale")
            return self.pixels.copy(), {
                "decoded_frame_id": self.sequence,
                "sequence": self.sequence,
                "timestamp_us": 123000 + self.sequence,
                "frame_age_ms": 0,
                "sender_id": "test",
                "camera_id": "cam01",
            }

    def snapshot(self):
        return {"id": self.target, "configured": True, "mode": "gwhp_main", "status": "streaming"}

    def close(self):
        pass


@pytest.fixture
def live(app_client, monkeypatch):
    app, client = app_client
    camera = Camera()
    monkeypatch.setattr(app, "receiver_camera", camera)
    return app, client, camera


def wait_for(client, sid, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get("/api/camera/scan-sessions/" + sid).json()
        if predicate(result):
            return result
        time.sleep(0.02)
    raise AssertionError(result)


def label(app, name):
    return cv2.imread(str(app.BASE / "static" / "labels" / (name + ".png")))


def start(client):
    response = client.post("/api/camera/scan-sessions", json={"operator": "测试员"})
    assert response.status_code == 200
    return response.json()["session_id"]
