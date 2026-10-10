"""Reusable photo_measurements fixtures and doubles."""

import base64
import copy
import io

import pytest
from PIL import Image

from field_recognition.measurements.measurement_records import build_records
from field_recognition.measurements.reading_results import build_readings
from field_recognition.recognition.panel_readout import save
from tests.support.demo import app_client, register, scan  # noqa: F401


@pytest.fixture
def setup(app_client, monkeypatch):
    app, client = app_client
    capture = scan(client)
    iid = capture["matches"][0]["id"]
    register(client, iid)
    binding = client.post(
        "/api/bindings",
        json={"scan_id": capture["scan_id"], "instrument_id": iid, "operator": "Reader"},
    ).json()
    queued = []
    monkeypatch.setattr(app, "RECORD_MODE", "production")
    monkeypatch.setattr(app.readout_pool, "submit", lambda fn, doc: queued.append(doc))
    return app, client, binding, queued


def photos(app, count=3):
    result = []
    for i in range(count):
        out = io.BytesIO()
        Image.new("RGB", (400, 300), (i * 20, 100, 70)).save(out, format="PNG")
        result.append(
            {
                "capture_id": "burst-photo-" + str(i),
                "camera_id": "TestCamera",
                "captured_at": app.now(),
                "source_ref": "nas://photo/" + str(i),
                "image_base64": base64.b64encode(out.getvalue()).decode(),
            }
        )
    return result


def enqueue(setup, count=3):
    app, client, binding, queued = setup
    request = {
        "burst_id": "burst-1",
        "photos": photos(app, count),
        "experiment_context_ref": "experiment://001/step-2",
        "instrument_ids": [binding["instrument"]["id"]],
    }
    response = client.post("/api/ocr/photo-burst", json=request)
    assert response.status_code == 202, response.text
    return response.json(), request


def complete(setup, index, text="51", unit="°C", qr=True, *, publish=True):
    app, _, binding, queued = setup
    job = copy.deepcopy(queued[index])
    snapshots = copy.deepcopy([binding])
    if not qr:
        snapshots[0].pop("qr_hash", None)
    job.update(
        status="completed",
        finished_at=app.now(),
        all_binding_snapshots=snapshots,
        model="OCR-test-double",
        device="cpu",
        panel_detection={"status": "completed", "weights_sha256": "a" * 64},
        panel_regions=[
            {
                "panel_id": "a-temperature",
                "instrument": binding["instrument"],
                "class_id": 0,
                "instrument_id": binding["instrument"]["id"],
                "binding_id": binding["binding_id"],
                "association_basis": "panel_detector_session_binding",
                "measurement_name": "温度",
                "bbox": [10, 10, 110, 45],
            }
        ],
        lines=[
            {
                "panel_id": "a-temperature",
                "text": text,
                "unit": unit,
                "confidence": 0.91,
                "polygon": [[10, 10], [110, 10], [110, 45], [10, 45]],
            }
        ],
    )
    job["local_ocr"] = {"lines": copy.deepcopy(job["lines"]), "model": "OCR-test-double"}
    job["readings"] = build_readings(job)
    job["measurement_records"] = build_records(job)
    if publish:
        save(vars(app), job)
    return job


def get_draft(client, mid):
    return client.get("/api/photo-measurements/" + mid).json()
