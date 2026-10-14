"""OFF is a state, never a numeric zero or evidence of instrument identity."""

import json

import numpy as np
import pytest

from field_recognition.recognition import aliyun_vision as vision
from tests.support.aliyun_vision import enabled, response  # noqa: F401
from tests.support.configuration import refresh_runtime_config
from tests.support.demo import app_client  # noqa: F401
from tests.support.multi_readout import two_bindings
from tests.support.panel_readout import refresh_recognition_config
from tests.support.panel_regions import BOXES, configure
from tests.support.unbound_readout import photo


def test_separate_numeric_regions_and_off_survive_adapter(enabled, monkeypatch):
    answer = {
        "status": "readable",
        "readings": [{"text": "900", "value": "900", "label": "转速"}],
        "display_states": [{"text": "OFF", "state": "OFF", "label": "温度"}],
    }
    monkeypatch.setattr(vision, "_request", lambda *a: response(answer))
    result = vision.read_panel(np.zeros((30, 40, 3), np.uint8))
    assert result["status"] == "completed"
    assert result["answer"]["display_states"][0]["state"] == "OFF"
    lines = vision.answer_lines(result["answer"])
    assert lines[0]["value"] == "900"
    assert lines[1]["value"] is None and lines[1]["numeric_candidates"] == []
    assert lines[1]["display_state"] == "OFF"
    assert "test-only-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "state",
    [
        {"text": "0FF", "state": "OFF"},
        {"text": "OFF OFF", "state": "OFF"},
        {"text": "OFF 900", "state": "OFF"},
        {"text": "OFF", "state": "OFF", "value": "0"},
        {"text": "ON", "state": "ON"},
    ],
)
def test_ambiguous_or_invented_state_is_rejected(enabled, monkeypatch, state):
    monkeypatch.setattr(
        vision,
        "_request",
        lambda *a: response(
            {
                "status": "readable",
                "readings": [],
                "display_states": [state],
            }
        ),
    )
    result = vision.read_panel(np.zeros((30, 40, 3), np.uint8))
    assert result["error"] == "invalid_answer" and result["raw_answer"]


def test_unreadable_cannot_contain_off(enabled, monkeypatch):
    monkeypatch.setattr(
        vision,
        "_request",
        lambda *a: response(
            {
                "status": "unreadable",
                "readings": [],
                "display_states": [{"text": "OFF", "state": "OFF"}],
            }
        ),
    )
    assert vision.read_panel(np.zeros((30, 40, 3), np.uint8))["error"] == "invalid_answer"


def test_primary_unbound_off_remains_unassigned_not_zero(app_client, enabled, monkeypatch):
    app, client = app_client
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    monkeypatch.setenv("FIELD_VISION_PHOTO_MODE", "primary")
    monkeypatch.setenv(
        "FIELD_VISION_DISPLAY_RULES",
        json.dumps(
            [
                {
                    "instrument_type": "balance",
                    "measurement_name": "质量",
                    "unit": "g",
                    "decimal_places": 4,
                    "fixed_decimal_display": True,
                    "confirmed_by_user": True,
                }
            ]
        ),
    )
    refresh_runtime_config(app, "camera_id")
    refresh_recognition_config(app)
    monkeypatch.setattr(app.readout_pool, "submit", lambda *a, **kw: None)
    monkeypatch.setattr(
        vision,
        "_request",
        lambda *a, **kw: response(
            {
                "status": "readable",
                "readings": [],
                "display_states": [{"text": "OFF", "state": "OFF"}],
            }
        ),
    )
    job = client.post("/api/ocr/photo-result", json={"photo": photo(app)}).json()
    app.run_ocr(job)
    result = app.get_job(job["job_id"])
    assert result["status"] == "completed" and result["outcome"] == "display_state"
    assert result["lines"][0]["display_state"] == "OFF"
    assert result["lines"][0]["value"] is None
    assert result["readings"] == result["measurement_records"] == []
    assert result["instrument"] is result["binding_id"] is None


@pytest.mark.parametrize("conflict", [None, "numeric", "label", "multiple", "native"])
def test_localized_off_respects_field_conflicts_and_native_rejection(
    app_client, enabled, monkeypatch, conflict
):
    app, client = app_client
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    monkeypatch.setenv("FIELD_VISION_PHOTO_MODE", "primary")
    refresh_runtime_config(app, "camera_id")
    refresh_recognition_config(app)
    bindings = two_bindings(app, client)
    configure(app, monkeypatch, BOXES[1:])
    monkeypatch.setattr(app.readout_pool, "submit", lambda *a, **kw: None)
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app)), trigger="voice_photo_directory"
    )
    local = app.predict_readout(np.zeros((300, 400, 3), np.uint8), binding_snapshots=bindings)
    local["panel_regions"][0]["measurement_name"] = "质量"
    if conflict == "native":
        local["panel_regions"][0]["local_ocr"]["display_state_rejection"] = {
            "basis": "native_conflict"
        }
    job["precomputed_local"] = local
    state = {"text": "OFF", "state": "OFF", "label": "转速" if conflict == "label" else None}
    answer = {"status": "readable", "readings": [], "display_states": [state]}
    if conflict == "multiple":
        answer["display_states"].append(dict(state))
    if conflict == "numeric":
        answer["readings"] = [{"text": "900", "value": "900"}]
    monkeypatch.setattr(vision, "_request", lambda *a, **kw: response(answer))
    app.run_ocr(job)
    result = app.get_job(job["job_id"])
    if conflict:
        # Existing whole-photo recovery may preserve unassigned candidates. It
        # cannot recover the rejected localized field's physical attribution.
        region = result["localized_attempt"]["panel_regions"][0]
        assert not region.get("display_state")
        assert region["display_state_rejection"]
        assert result["measurement_records"] == []
        assert all(r["instrument"] is None and r["binding_id"] is None for r in result["readings"])
        assert result["instrument"] is result["binding_id"] is None
        return
    region = result["panel_regions"][0]
    value = result["measurement_records"][0]["record"]["values"][0]
    assert result["status"] == "completed"
    assert value["value"] is None and result["readings"] == []
    assert result["fallback"]["answer"]["display_states"]
    assert value["display_state"] == "OFF"
    assert region["display_state"]["human_verified"] is False
    assert result["display_fields"][0]["status"] == "display_state"
