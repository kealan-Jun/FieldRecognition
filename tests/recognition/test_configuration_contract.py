"""Startup validation and each runtime use the same immutable inference settings."""

import json
from concurrent.futures import Future
import subprocess
import sys
from threading import Lock

import numpy as np
import pytest
from pydantic import ValidationError

from field_recognition.measurements import measurement_records, photo_measurements, readout_rules
from field_recognition.measurements.reading_results import build_readings
from field_recognition.recognition import aliyun_vision, panel_readout
from field_recognition.recognition.config import RecognitionConfig
from field_recognition.recognition.pipeline_context import ReadoutPipeline
from field_recognition.runtime.production_config import ProductionConfig


def test_parallel_photo_mode_is_preserved_in_the_immutable_public_config(monkeypatch):
    config = RecognitionConfig.from_environment({"FIELD_VISION_PHOTO_MODE": "parallel"})
    monkeypatch.setenv("FIELD_VISION_PHOTO_MODE", "primary")
    assert aliyun_vision.public_config(config)["photo_mode"] == "parallel"
    assert not aliyun_vision.primary_photos(config)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", False), (" false ", False), ("NO", False), ("1", True), ("true", True), ("yes", True)],
)
def test_thinking_mode_uses_one_immutable_startup_snapshot(monkeypatch, value, expected):
    monkeypatch.setenv("FIELD_ALIYUN_ENABLE_THINKING", value)
    config = ProductionConfig.from_environment()
    monkeypatch.setenv("FIELD_ALIYUN_ENABLE_THINKING", "1" if not expected else "0")
    assert config.recognition.enable_thinking is expected
    assert aliyun_vision.public_config(config.recognition)["enable_thinking"] is expected


@pytest.mark.parametrize("value", ["", "sometimes", "2"])
def test_invalid_environment_thinking_mode_is_rejected(value):
    with pytest.raises(ValueError, match="FIELD_ALIYUN_ENABLE_THINKING"):
        RecognitionConfig.from_environment({"FIELD_ALIYUN_ENABLE_THINKING": value})


@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_explicit_thinking_mode_requires_a_boolean(value):
    with pytest.raises(ValueError, match="enable_thinking"):
        RecognitionConfig(enable_thinking=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.1", 0.1),
        (".25", 0.25),
        ("5", 5.0),
        ("60.5", 60.5),
        ("120", 120.0),
        ("0", 5.0),
        (".09", 5.0),
        ("120.1", 5.0),
        ("bad", 5.0),
        ("", 5.0),
        ("nan", 5.0),
        ("inf", 5.0),
        ("-inf", 5.0),
    ],
)
def test_environment_threshold_has_one_startup_and_inference_interpretation(
    monkeypatch, value, expected
):
    monkeypatch.setenv("FIELD_ALIYUN_NO_DIGITS_SECONDS", value)
    startup = ProductionConfig.from_environment()
    assert startup.aliyun_no_digits_seconds == expected
    assert startup.recognition.no_digits_seconds == expected
    assert aliyun_vision.no_digits_seconds() == expected
    assert aliyun_vision.public_config(startup.recognition)["no_digits_seconds"] == expected


@pytest.mark.parametrize("value", [0, 0.099, 120.001, float("nan"), float("inf")])
def test_explicit_configuration_rejects_unbounded_threshold(value):
    with pytest.raises((ValidationError, ValueError)):
        ProductionConfig(aliyun_no_digits_seconds=value)
    with pytest.raises(ValueError):
        RecognitionConfig(no_digits_seconds=value)


def test_explicit_compatibility_fields_propagate_to_inference():
    config = ProductionConfig(
        aliyun_no_digits_seconds=0.25,
        aliyun_fallback_enabled=True,
        dashscope_api_key="synthetic-test-key",
        aliyun_model="test-model",
    )
    public = aliyun_vision.public_config(config.recognition)
    assert public["available"] and public["model"] == "test-model"
    assert public["no_digits_seconds"] == 0.25
    assert "synthetic-test-key" not in repr(config)
    nested = ProductionConfig(recognition=RecognitionConfig(no_digits_seconds=120))
    assert nested.aliyun_no_digits_seconds == 120


def test_injected_provider_snapshot_survives_environment_changes(monkeypatch):
    config = RecognitionConfig.from_environment(
        {
            "FIELD_ALIYUN_FALLBACK_ENABLED": "1",
            "DASHSCOPE_API_KEY": "synthetic-test-key",
            "FIELD_ALIYUN_MODEL": "snapshot-model",
            "FIELD_ALIYUN_NO_DIGITS_SECONDS": ".25",
        }
    )
    monkeypatch.setenv("FIELD_VISION_PROVIDER", "invalid-new-provider")
    monkeypatch.setenv("FIELD_ALIYUN_NO_DIGITS_SECONDS", "120")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "different-test-key")
    observed = []

    def request(payload, key, *, config):
        observed.append((payload["model"], key, config.no_digits_seconds))
        assert aliyun_vision.base_url(config) == aliyun_vision.DEFAULT_BASE_URL
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "status": "unreadable",
                                "readings": [],
                                "notes": "synthetic stub",
                            }
                        )
                    },
                }
            ]
        }

    monkeypatch.setattr(aliyun_vision, "_request", request)
    result = aliyun_vision.read_panel(np.zeros((8, 8, 3), dtype=np.uint8), config=config)
    assert result["status"] == "completed"
    assert observed == [("snapshot-model", "synthetic-test-key", 0.25)]
    assert "synthetic-test-key" not in json.dumps(result)


def test_readout_deadline_uses_injected_config_without_rereading_environment(tmp_path, monkeypatch):
    clock = [0.0]
    image_root = tmp_path / "Images"
    image_root.mkdir()
    (image_root / "job.png").write_bytes(b"synthetic crop bytes")
    config = ProductionConfig(
        aliyun_no_digits_seconds=0.25,
        aliyun_fallback_enabled=True,
        dashscope_api_key="synthetic-test-key",
    )
    monkeypatch.setenv("FIELD_ALIYUN_NO_DIGITS_SECONDS", "120")
    monkeypatch.setenv("FIELD_ALIYUN_FALLBACK_ENABLED", "0")
    monkeypatch.setattr(panel_readout.cv2, "imread", lambda _: np.zeros((8, 8, 3), dtype=np.uint8))
    monkeypatch.setattr(panel_readout.cv2, "imwrite", lambda *_: True)
    monkeypatch.setattr(panel_readout, "save", lambda *_: None)
    future = Future()
    future.set_result({"status": "completed", "lines": [], "model": "stub", "device": "cpu"})

    class Pool:
        def submit(self, *_):
            return future

    seen = []

    def cloud(_, *, config):
        seen.append((clock[0], config.no_digits_seconds))
        return {"status": "completed", "model": "stub", "answer": {"readings": []}}

    monkeypatch.setattr(aliyun_vision, "read_panel", cloud)
    core = {
        "config": config,
        "DATA": tmp_path,
        "now": lambda: "2026-10-10T01:00:00Z",
        "current_readout_binding": lambda _: True,
        "ocr_pool": Pool(),
        "predict_panel": lambda *_: None,
        "ocr_state": {"engine": "stub"},
        "vision_call_lock": Lock(),
        "reserve_fallback": lambda _: True,
    }
    job = {
        "job_id": "job",
        "capture_id": "capture",
        "camera_id": "test-camera",
        "crop": [0, 0, 8, 8],
        "submitted_at": core["now"](),
        "status": "queued",
    }
    panel_readout.run(
        core,
        job,
        clock=lambda: clock[0],
        pause=lambda duration: clock.__setitem__(0, clock[0] + duration),
    )
    assert seen == [(0.25, 0.25)]
    assert job["no_digits_timeout_seconds"] == 0.25
    assert job["fallback"]["trigger_elapsed_seconds"] == 0.25
    assert job["status"] == "completed"


def test_measurement_grammar_is_shared_without_a_cloud_dependency():
    assert measurement_records.UNITS is readout_rules.UNITS
    assert photo_measurements.UNITS is readout_rules.UNITS
    assert aliyun_vision.READOUT is readout_rules.READOUT
    assert readout_rules.fallback_reason([{"text": "0012"}]) == "decimal_uncertain"
    assert readout_rules.fallback_reason([{"text": "-12.5 °C"}]) is None


def test_numeric_reading_generation_does_not_import_the_cloud_provider():
    script = """
import json, sys
sys.modules['field_recognition.recognition.aliyun_vision'] = None
from field_recognition.measurements.reading_results import build_readings
readings = build_readings({'job_id':'job', 'capture_id':'capture',
    'lines':[{'text':'24.5 °C'}]})
assert readings[0]['value'] == '24.5'
assert readings[0]['unit'] == '°C'
assert readings[0]['instrument'] is None
print(len(readings))
"""
    result = subprocess.check_output([sys.executable, "-c", script], text=True)
    assert result.strip() == "1"


def test_confirmed_display_rules_use_runtime_snapshot(monkeypatch):
    rule = {
        "instrument_type": "balance",
        "measurement_name": "质量",
        "unit": "g",
        "decimal_places": 4,
        "fixed_decimal_display": True,
        "confirmed_by_user": True,
    }
    settings = RecognitionConfig.from_environment(
        {"FIELD_VISION_DISPLAY_RULES": json.dumps([rule]), "FIELD_VISION_PHOTO_MODE": "primary"}
    )
    pipeline = ReadoutPipeline(lambda _: None, lambda *_: True, settings)
    exposed_rules = pipeline.display_rules()
    exposed_rules[0]["decimal_places"] = 2
    monkeypatch.setenv("FIELD_VISION_DISPLAY_RULES", "[]")
    assert pipeline.display_rules() == [rule]
    observed = []

    def cloud(_, *, display_rules, config):
        observed.append((display_rules, config))
        return {
            "status": "completed",
            "model": "stub",
            "answer": {
                "readings": [{"text": "144062 g", "value": "144062", "unit": "g", "label": "质量"}]
            },
        }

    monkeypatch.setattr(aliyun_vision, "read_panel", cloud)
    monkeypatch.setattr(panel_readout, "save", lambda *_: None)
    core = {
        "config": ProductionConfig(recognition=settings),
        "vision_call_lock": Lock(),
        "now": lambda: "2026-10-10T01:00:00Z",
    }
    job = {
        "submitted_at": core["now"](),
        "status": "running",
        "job_id": "job",
        "capture_id": "capture",
    }
    panel_readout.primary_unbound_photo(
        core,
        job,
        np.zeros((8, 8, 3), dtype=np.uint8),
        valid=lambda: True,
        clock=lambda: 0,
        pause=lambda _: None,
        started=0,
    )
    assert observed == [([rule], settings)]
    assert job["lines"][0]["value"] == "14.4062"
    assert job["lines"][0]["raw_value"] == "144062"
    assert build_readings(job)[0]["instrument"] is None


def test_runtime_assembly_settings_are_captured_once(monkeypatch):
    monkeypatch.setenv("FIELD_SERVICE_ROLE", "capture")
    monkeypatch.setenv("FIELD_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("FIELD_DEMO_DATA", "/synthetic-data")
    config = ProductionConfig.from_environment()
    monkeypatch.setenv("FIELD_SERVICE_ROLE", "ocr")
    monkeypatch.setenv("FIELD_PRODUCTION_ENABLED", "0")
    monkeypatch.setenv("FIELD_DEMO_DATA", "/different-synthetic-data")
    assert config.service_role == "capture"
    assert config.production_enabled is True
    assert config.data_dir == "/synthetic-data"
    assert config.database_path == "/synthetic-data/Demo.sqlite3"
