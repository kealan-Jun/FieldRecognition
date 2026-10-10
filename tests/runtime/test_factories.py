"""Contracts for import safety, isolated factories and the legacy ASGI entry point."""

import importlib
import os
import subprocess
import sys
import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from field_recognition.api.application import create_app
from field_recognition.runtime.factory import create_runtime
from field_recognition.runtime.paths import PROJECT_ROOT
from field_recognition.runtime.production_config import ProductionConfig


def disable_sources(monkeypatch):
    for name in (
        "FIELD_PRODUCTION_ENABLED",
        "FIELD_AUTH_ENABLED",
        "FIELD_ARCHIVE_ENABLED",
        "FIELD_SAVED_PHOTO_WATCH_ENABLED",
        "FIELD_VIDEO_OCR_ENABLED",
        "FIELD_PANEL_DETECTOR_ENABLED",
        "FIELD_ALIYUN_FALLBACK_ENABLED",
        "FIELD_AUTO_RUN_ENABLED",
    ):
        monkeypatch.setenv(name, "0")
    for name in (
        "FIELD_RECEIVER_URL",
        "FIELD_CAMERA_SNAPSHOT_URL",
        "FIELD_DATABASE_PATH",
        "FIELD_ARCHIVE_ROOT",
        "FIELD_ARCHIVE_MOUNT",
        "FIELD_SAVED_PHOTO_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FIELD_SERVICE_ROLE", "combined")


def test_canonical_imports_do_not_initialize_runtime(tmp_path):
    target = tmp_path / "not-created"
    environment = dict(
        os.environ, FIELD_DEMO_DATA=str(target), FIELD_DATABASE_PATH=str(target / "test.sqlite3")
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import threading
from field_recognition.runtime.database import Database
def unexpected(*args, **kwargs):
    raise AssertionError('Canonical import opened a database')
Database.__init__ = unexpected
before = set(threading.enumerate())
import field_recognition.api.application
import field_recognition.runtime.factory
import field_recognition.runtime.app_production
import field_recognition.runtime.ocr_worker
import field_recognition.runtime.archive_worker
import field_recognition.runtime.capture_worker
import field_recognition.runtime.camera_supervisor
assert set(threading.enumerate()) == before
assert 'app' not in sys.modules
assert 'paddle' not in sys.modules and 'paddleocr' not in sys.modules
""",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert not target.exists()


def test_runtime_factories_keep_database_state_and_models_independent(tmp_path, monkeypatch):
    disable_sources(monkeypatch)
    runtimes = [
        create_runtime(
            ProductionConfig(database_path=str(tmp_path / name / "test.sqlite3")),
            service_role="ocr",
        )
        for name in ("first", "second")
    ]
    try:
        first, second = runtimes
        assert first.database.path != second.database.path
        assert first["ocr_state"] is not second["ocr_state"]
        assert first["ocr_pool"] is not second["ocr_pool"]
        assert first["panel_detector"] is not second["panel_detector"]
        assert isinstance(first["app"], APIRouter)
        assert not isinstance(first["app"], FastAPI)
        first["ocr_state"]["status"] = "test_state"
        assert second["ocr_state"]["status"] == "not_loaded"
        assert first["ocr_model"] is second["ocr_model"] is None
        with first["db"]() as conn:
            conn.execute("UPDATE instruments SET scene='first runtime'")
        with second["db"]() as conn:
            assert not conn.execute(
                "SELECT 1 FROM instruments WHERE scene='first runtime'"
            ).fetchone()
    finally:
        for runtime in runtimes:
            runtime.close(wait=True)


def test_worker_construction_uses_runtime_without_http_entry_point(tmp_path, monkeypatch):
    disable_sources(monkeypatch)
    environment = dict(os.environ, FIELD_DEMO_DATA=str(tmp_path), FIELD_SERVICE_ROLE="api")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import sys
from pathlib import Path
from fastapi import APIRouter, FastAPI
from field_recognition.runtime.production_config import ProductionConfig
from field_recognition.runtime.ocr_worker import OCRWorker
from field_recognition.runtime.archive_worker import ArchiveWorker
config = ProductionConfig(database_path=str(Path(os.environ['FIELD_DEMO_DATA']) / 'workers.sqlite3'))
workers = [OCRWorker('test-ocr', config), ArchiveWorker('test-archive', config)]
try:
    assert os.environ['FIELD_SERVICE_ROLE'] == 'api'
    assert 'app' not in sys.modules
    assert 'field_recognition.api.application' not in sys.modules
    assert workers[0].core.service_role == 'ocr'
    assert workers[1].core.service_role == 'archive'
    for worker in workers:
        assert isinstance(worker.core['app'], APIRouter)
        assert not isinstance(worker.core['app'], FastAPI)
        assert worker.core['receiver_camera'] is None
        assert worker.core['ocr_model'] is None
finally:
    for worker in workers:
        worker.core.close(wait=True)
""",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_explicit_configuration_is_not_replaced_by_environment(tmp_path, monkeypatch):
    disable_sources(monkeypatch)
    config = ProductionConfig(
        database_path=str(tmp_path / "snapshot.sqlite3"),
        data_dir=str(tmp_path / "photos"),
        camera_id="snapshot-camera",
        service_role="ocr",
        production_enabled=False,
    )
    monkeypatch.setenv("FIELD_SERVICE_ROLE", "api")
    monkeypatch.setenv("FIELD_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("FIELD_DEMO_DATA", str(tmp_path / "unwanted"))
    monkeypatch.setenv("FIELD_CAMERA_ID", "other-camera")
    runtime = create_runtime(config)
    try:
        assert runtime.service_role == "ocr"
        assert runtime["RUNTIME_ENABLED"] is False
        assert runtime.data_dir == tmp_path / "photos"
        assert runtime.config.data_dir == str(runtime.data_dir)
        assert runtime["receiver_camera"] is None
        assert runtime["state"]()["camera"]["id"] == "snapshot-camera"
        assert not (tmp_path / "unwanted").exists()
    finally:
        runtime.close(wait=True)


def test_remote_transport_settings_are_captured_without_reading_token(tmp_path, monkeypatch):
    from field_recognition.recognition.config import RecognitionConfig
    from field_recognition.recognition.remote_ocr import RemoteOcr

    disable_sources(monkeypatch)
    spool = tmp_path / "spool"
    spool.mkdir()
    token_file = tmp_path / "token-not-read"
    for key, value in {
        "FIELD_OCR_SERVICE_TOKEN_FILE": str(token_file),
        "FIELD_OCR_SERVICE_SCOPE": "captured-scope",
        "FIELD_OCR_SERVICE_STORAGE": "storage",
        "FIELD_OCR_SERVICE_MODEL_REVISION": "model-revision",
        "FIELD_OCR_SERVICE_SPOOL": str(spool),
    }.items():
        monkeypatch.setenv(key, value)
    config = ProductionConfig(
        database_path=str(tmp_path / "remote.sqlite3"),
        service_role="ocr",
        recognition=RecognitionConfig(ocr_service_url="https://ocr.invalid"),
    )
    runtime = create_runtime(config)
    model = None
    try:
        monkeypatch.setenv("FIELD_OCR_SERVICE_URL", "https://other.invalid")
        monkeypatch.setenv("FIELD_OCR_SERVICE_SCOPE", "changed-scope")
        monkeypatch.setenv("FIELD_OCR_SERVICE_TOKEN_FILE", str(tmp_path / "changed-token"))
        model = runtime["create_ocr_model"]()
        assert isinstance(model, RemoteOcr)
        assert model.url == "https://ocr.invalid"
        assert model.scope == "captured-scope"
        assert model.token_file == token_file
        assert not token_file.exists()
    finally:
        if model is not None:
            model.client.close()
        runtime.close(wait=True)


def test_explicit_local_runtime_ignores_ambient_remote_url(tmp_path, monkeypatch):
    from field_recognition.recognition import service
    from field_recognition.recognition.remote_ocr import RemoteOcrConfig

    disable_sources(monkeypatch)
    monkeypatch.setenv("FIELD_OCR_SERVICE_URL", "https://ambient.invalid")
    config = ProductionConfig(database_path=str(tmp_path / "local.sqlite3"), service_role="ocr")
    runtime = create_runtime(config)
    calls = []
    monkeypatch.setattr(
        service, "create_model", lambda device, **options: calls.append((device, options))
    )
    try:
        assert runtime["ocr_model"] is None
        assert runtime["ocr_service_config"] is None
        assert RemoteOcrConfig.from_environment(service_url=None).service_url is None
        assert RemoteOcrConfig.from_environment().service_url == "https://ambient.invalid"
        runtime["create_ocr_model"]()
        assert calls == [("cpu", {"remote_config": None})]
    finally:
        runtime.close(wait=True)


def test_legacy_entry_point_is_fresh_and_monkeypatch_reaches_routes(tmp_path, monkeypatch):
    disable_sources(monkeypatch)
    modules = []
    try:
        for name in ("first", "second"):
            monkeypatch.setenv("FIELD_DEMO_DATA", str(tmp_path / name))
            sys.modules.pop("app", None)
            modules.append(importlib.import_module("app"))
        first, second = modules
        assert first.runtime is not second.runtime
        assert first.app is not second.app
        assert first.DATA != second.DATA
        monkeypatch.setattr(first, "now", lambda: "2026-10-10T00:00:00+00:00")
        with TestClient(first.app) as client:
            assert client.get("/api/export").json()["exported_at"] == first.now()
            assert client.get("/api/v1/export").json()["exported_at"] == first.now()
        assert vars(first)["now"] is first.runtime["now"]
        assert second.runtime["now"] is not first.runtime["now"]
        schema = second.app.openapi()
        assert "_runtime" not in str(schema["paths"]["/api/ocr"])
        assert schema["paths"]["/api/ocr"]["post"]["responses"].get("202")
        assert create_app(second.runtime) is second.app
    finally:
        for module in modules:
            module.runtime.close(wait=True)


def test_production_compatibility_helper_reuses_existing_managed_core(tmp_path, monkeypatch):
    from field_recognition.runtime.app_production import get_production_core
    from field_recognition.runtime.database import Database, apply_migrations

    disable_sources(monkeypatch)
    monkeypatch.setenv("FIELD_DEMO_DATA", str(tmp_path))
    monkeypatch.setenv("FIELD_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("FIELD_SERVICE_ROLE", "api")
    apply_migrations(Database(tmp_path / "Demo.sqlite3"))
    sys.modules.pop("app", None)
    app = importlib.import_module("app")
    try:
        legacy = importlib.import_module("app_production")
        assert legacy.get_production_core() is app.runtime
        assert legacy.get_production_core() is app.runtime
        assert get_production_core(app.runtime) is app.runtime
        with pytest.raises(TypeError, match="explicit runtime"):
            get_production_core()
    finally:
        app.runtime.close(wait=True)
