"""Reusable direct_access fixtures and doubles."""

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

from field_recognition.runtime.database import Database, apply_migrations


@pytest.fixture(params=["test", "production"])
def direct(tmp_path, monkeypatch, request):
    for key, value in {
        "FIELD_DEMO_DATA": str(tmp_path),
        "FIELD_DATABASE_PATH": str(tmp_path / "Demo.sqlite3"),
        "FIELD_PRODUCTION_ENABLED": "1",
        "FIELD_SERVICE_ROLE": "api",
        "FIELD_RECORD_MODE": request.param,
        "FIELD_REGISTRY_POLICY": "sample",
        "FIELD_CAMERA_ID": "cam-a",
        "FIELD_ALIYUN_FALLBACK_ENABLED": "0",
        "FIELD_ARCHIVE_ENABLED": "0",
        "FIELD_SAVED_PHOTO_WATCH_ENABLED": "0",
        "FIELD_VIDEO_OCR_ENABLED": "0",
        "FIELD_PANEL_DETECTOR_ENABLED": "0",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("FIELD_AUTH_ENABLED", "FIELD_RECEIVER_URL", "FIELD_CAMERA_SNAPSHOT_URL"):
        monkeypatch.delenv(key, raising=False)
    db = Database(tmp_path / "Demo.sqlite3")
    apply_migrations(db)
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    try:
        with TestClient(module.app) as client:
            yield module, client
    finally:
        sys.modules.pop("app", None)
