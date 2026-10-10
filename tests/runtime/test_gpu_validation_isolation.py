"""A synthetic GPU probe must not inherit production inputs or records."""

import os

import pytest

from scripts.verify_gpu_ocr import validation_runtime


def test_probe_ignores_production_environment(tmp_path, monkeypatch):
    production = tmp_path / "production"
    isolated = tmp_path / "probe"
    hostile = {
        "FIELD_DEMO_DATA": str(production),
        "FIELD_DATABASE_PATH": str(production / "Live.sqlite3"),
        "FIELD_PRODUCTION_ENABLED": "1",
        "FIELD_AUTH_ENABLED": "1",
        "FIELD_RECEIVER_URL": "http://receiver.invalid",
        "FIELD_CAMERA_SNAPSHOT_URL": "http://camera.invalid",
        "FIELD_ARCHIVE_ENABLED": "1",
        "FIELD_ARCHIVE_ROOT": str(production / "archive"),
        "FIELD_SAVED_PHOTO_WATCH_ENABLED": "1",
        "FIELD_SAVED_PHOTO_ROOT": str(production / "photos"),
        "FIELD_AUTO_RUN_ENABLED": "1",
        "FIELD_VIDEO_OCR_ENABLED": "1",
        "FIELD_OCR_SERVICE_URL": "https://ocr.invalid",
        "FIELD_VISION_PROVIDER": "nianfeng",
        "FIELD_VISION_ENABLED": "1",
        "FIELD_VISION_API_KEY": "test-only-placeholder",
        "FIELD_ALIYUN_FALLBACK_ENABLED": "1",
        "DASHSCOPE_API_KEY": "test-only-placeholder",
        "FIELD_PANEL_DETECTOR_ENABLED": "1",
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)

    with validation_runtime(isolated) as runtime:
        assert runtime.data_dir == isolated
        assert runtime.database.path == isolated / "Demo.sqlite3"
        assert runtime.service_role == "combined"
        assert runtime["RUNTIME_ENABLED"] is False
        assert runtime["AUTH_ENABLED"] is False
        assert runtime["receiver_camera"] is None
        assert runtime["config"].camera_snapshot_url is None
        assert runtime["config"].recognition.ocr_service_url is None
        assert runtime["config"].recognition.enabled is False
        assert runtime["config"].recognition.api_key == ""
        assert runtime["archive_store"].enabled() is False
        assert runtime["saved_photo_watcher"].enabled() is False
        assert runtime["automatic_runner"].settings()["enabled"] is False
        assert runtime["video_ocr"].enabled() is False
        assert runtime["panel_detector"].enabled() is False
        assert runtime["ocr_model"] is None
        assert runtime["ocr_state"]["load_count"] == 0
        assert not production.exists()

    assert runtime["stopping"].is_set()
    assert os.environ["FIELD_PANEL_DETECTOR_ENABLED"] == "1"
    assert os.environ["FIELD_DATABASE_PATH"] == hostile["FIELD_DATABASE_PATH"]


def test_probe_restores_driver_setting_after_constructor_failure(tmp_path, monkeypatch):
    from field_recognition.runtime import factory

    def fail(_config):
        raise RuntimeError("synthetic construction failure")

    monkeypatch.setattr(factory, "create_runtime", fail)
    monkeypatch.delenv("FIELD_PANEL_DETECTOR_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="synthetic construction failure"):
        with validation_runtime(tmp_path):
            pytest.fail("construction should fail before yielding")
    assert "FIELD_PANEL_DETECTOR_ENABLED" not in os.environ
