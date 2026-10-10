"""Reusable production_regressions fixtures and doubles."""

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

from field_recognition.runtime.auth import AuthService
from field_recognition.runtime.database import Database, apply_migrations


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "State.sqlite3")
    apply_migrations(db)
    return db


@pytest.fixture
def managed(tmp_path, monkeypatch):
    for key, value in {
        "FIELD_DEMO_DATA": str(tmp_path),
        "FIELD_DATABASE_PATH": str(tmp_path / "Demo.sqlite3"),
        "FIELD_PRODUCTION_ENABLED": "1",
        "FIELD_AUTH_ENABLED": "1",
        "FIELD_SERVICE_ROLE": "api",
        "FIELD_RECORD_MODE": "test",
        "FIELD_REGISTRY_POLICY": "sample",
        "FIELD_CAMERA_ID": "cam-a",
        "FIELD_ALIYUN_FALLBACK_ENABLED": "0",
        "FIELD_ARCHIVE_ENABLED": "0",
        "FIELD_SAVED_PHOTO_WATCH_ENABLED": "0",
        "FIELD_VIDEO_OCR_ENABLED": "0",
        "FIELD_PANEL_DETECTOR_ENABLED": "0",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FIELD_RECEIVER_URL", raising=False)
    db = Database(tmp_path / "Demo.sqlite3")
    apply_migrations(db)
    auth = AuthService(db)
    admin = auth.create_initial_admin("admin", "管理员", "correct horse battery")
    operator = auth.create_user("alice", "甲", "operator", "test-password", admin.id)
    with db.connection() as c:
        c.execute("INSERT INTO camera_users VALUES(?,?)", ("cam-a", operator.id))
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    with TestClient(module.app) as client:
        yield module, client, auth, admin, operator
    sys.modules.pop("app", None)
