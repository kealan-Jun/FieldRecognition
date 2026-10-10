"""Portable clone defaults without cameras, model execution or runtime data."""

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from field_recognition.api.application import create_app
from field_recognition.recognition import panel_detector
from field_recognition.runtime.auth import AuthService
from field_recognition.runtime.bootstrap import import_registry, import_sample_registry
from field_recognition.runtime.database import Database, apply_migrations
from field_recognition.runtime.factory import create_runtime
from field_recognition.runtime.panel_detector_worker import inference_device
from field_recognition.runtime.paths import PROJECT_ROOT
from field_recognition.runtime.production_bootstrap import ProductionCore
from field_recognition.runtime.production_config import ProductionConfig


SAMPLE_INSTRUMENT = "e9434a0a-3319-414a-b988-4cc6884edce4"
SAMPLE_SCENE = "18a0cff5-eda6-4363-abe5-f7147e4ab2c1"


@pytest.mark.parametrize("camera_id", [None, "", "   "])
@pytest.mark.parametrize("source", ["receiver_url", "camera_snapshot_url"])
def test_camera_source_requires_explicit_identity_before_creating_data(tmp_path, camera_id, source):
    data = tmp_path / "not-created"
    config = ProductionConfig(
        database_path=str(data / "test.sqlite3"),
        **{source: "http://camera.invalid:8080"},
        camera_id=camera_id,
    )
    with pytest.raises(ValueError, match="FIELD_CAMERA_ID is required"):
        create_runtime(config)
    assert not data.exists()


def test_receiver_uses_selected_identity_without_starting_capture(tmp_path):
    runtime = create_runtime(
        ProductionConfig(
            database_path=str(tmp_path / "test.sqlite3"),
            receiver_url="http://receiver.invalid:8080",
            camera_id="selected-camera",
        )
    )
    try:
        assert runtime["receiver_camera"].target == "selected-camera"
        assert not runtime["receiver_camera"].started
        assert runtime["ocr_model"] is None
    finally:
        runtime.close(wait=True)


@pytest.mark.parametrize(
    "record_mode,policy,managed,seeded",
    [
        ("test", "auto", False, True),
        ("test", "auto", True, True),
        ("test", "none", False, False),
        ("production", "auto", False, False),
        ("production", "auto", True, False),
        ("production", "none", True, False),
        ("production", "sample", True, True),
    ],
)
def test_registry_policy_applies_to_instruments_and_scenes(
    tmp_path, record_mode, policy, managed, seeded
):
    database = Database(tmp_path / "test.sqlite3")
    if managed:
        apply_migrations(database)
    runtime = create_runtime(
        ProductionConfig(
            database_path=str(database.path),
            record_mode=record_mode,
            registry_policy=policy,
            production_enabled=managed,
            service_role="api",
        )
    )
    try:
        with runtime["db"]() as conn:
            instruments = conn.execute("SELECT id FROM instruments").fetchall()
            scenes = conn.execute("SELECT id FROM scenes").fetchall()
            assert {row[0] for row in instruments} == (
                {SAMPLE_INSTRUMENT, "eae17924-9fa7-4445-ac45-3987f5687be9"} if seeded else set()
            )
            assert {row[0] for row in scenes} == ({SAMPLE_SCENE} if seeded else set())
            assert conn.execute("SELECT count(*) FROM scans").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM qr_device_registry").fetchone()[0] == 0
        # Registering a label never supplies a decoded QR match.
        result = runtime["qr_matches"]([], np.empty((0, 4, 2)))
        assert result["matches"] == result["scene_matches"] == []
    finally:
        runtime.close(wait=True)


@pytest.mark.parametrize("policy", ["auto", "none", "sample"])
def test_production_registry_policy_preserves_existing_identity_and_history(tmp_path, policy):
    database = Database(tmp_path / "test.sqlite3")
    apply_migrations(database)
    import_sample_registry(database, PROJECT_ROOT)
    document = json.dumps({"ended_at": "2026-09-01T00:00:00+00:00", "evidence": "original"})
    with database.connection() as conn:
        conn.execute(
            "UPDATE instruments SET name=?,device_no=? WHERE id=?",
            ("现场已登记名称", "existing-serial", SAMPLE_INSTRUMENT),
        )
        conn.execute("ALTER TABLE scenes ADD COLUMN provenance TEXT DEFAULT 'original'")
        conn.execute("UPDATE scenes SET name='现场已登记场景' WHERE id=?", (SAMPLE_SCENE,))
        conn.execute(
            "INSERT INTO bindings(id,camera,ended,document) VALUES(?,?,?,?)",
            ("history", "existing-camera", "2026-09-01T00:00:00+00:00", document),
        )
    source = tmp_path / "source.png"
    source.write_bytes(b"existing source photo")
    runtime = create_runtime(
        ProductionConfig(
            database_path=str(database.path), record_mode="production", registry_policy=policy
        )
    )
    try:
        with runtime["db"]() as conn:
            instrument = conn.execute(
                "SELECT name,device_no FROM instruments WHERE id=?", (SAMPLE_INSTRUMENT,)
            ).fetchone()
            assert tuple(instrument) == ("现场已登记名称", "existing-serial")
            scene = conn.execute(
                "SELECT name,provenance FROM scenes WHERE id=?", (SAMPLE_SCENE,)
            ).fetchone()
            assert tuple(scene) == ("现场已登记场景", "original")
            assert (
                conn.execute("SELECT document FROM bindings WHERE id='history'").fetchone()[0]
                == document
            )
        assert source.read_bytes() == b"existing source photo"
    finally:
        runtime.close(wait=True)


def test_explicit_sample_import_is_idempotent_and_does_not_create_scan_evidence(tmp_path):
    database = Database(tmp_path / "labels.sqlite3")
    import_sample_registry(database, PROJECT_ROOT)
    import_sample_registry(database, PROJECT_ROOT)
    with database.connection() as conn:
        assert conn.execute("SELECT count(*) FROM instruments").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM scenes").fetchone()[0] == 1
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not tables & {"scans", "bindings", "qr_device_registry"}


def test_registry_and_session_environment_are_validated(monkeypatch):
    monkeypatch.setenv("FIELD_REGISTRY_POLICY", "none")
    monkeypatch.setenv("FIELD_SESSION_DURATION_HOURS", "24")
    config = ProductionConfig.from_environment()
    assert not config.use_sample_registry
    assert config.session_duration_hours == 24
    assert "require_device_credentials" not in ProductionConfig.model_fields
    for key, value in (
        ("FIELD_REGISTRY_POLICY", "invalid"),
        ("FIELD_SESSION_DURATION_HOURS", "73"),
    ):
        with monkeypatch.context() as local:
            local.setenv(key, value)
            with pytest.raises(ValidationError):
                ProductionConfig.from_environment()


def test_configured_session_expiry_cookie_and_auth_boundaries_match(tmp_path, monkeypatch):
    database = Database(tmp_path / "accounts.sqlite3")
    apply_migrations(database)
    AuthService(database).create_initial_admin("local-admin", "管理员", "temporary-test-password")
    config = ProductionConfig(
        database_path=str(database.path),
        service_role="api",
        auth_enabled=True,
        session_duration_hours=24,
        registry_policy="none",
    )
    # A supplied configuration controls both the session and browser lifetime.
    monkeypatch.setenv("FIELD_SESSION_DURATION_HOURS", "1")
    runtime = create_runtime(config)
    client = TestClient(create_app(runtime))
    try:
        response = client.post(
            "/api/auth/login",
            json={"username": "local-admin", "password": "temporary-test-password"},
        )
        assert response.status_code == 200
        assert "Max-Age=86400" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]
        token = client.cookies.get("field_session")
        with database.connection() as conn:
            row = conn.execute("SELECT created_at,expires_at FROM sessions").fetchone()
        assert datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(
            row["created_at"]
        ) == timedelta(hours=24)
        assert client.post("/api/auth/logout", json={}).status_code == 403
        assert (
            client.post(
                "/api/auth/logout",
                json={},
                headers={
                    "Origin": "https://other.invalid",
                    "X-CSRF-Token": response.json()["csrf_token"],
                },
            ).status_code
            == 403
        )
        # Existing explicit Bearer callers continue to work without cookie CSRF headers.
        assert (
            client.post(
                "/api/auth/logout", json={}, headers={"Authorization": "Bearer " + token}
            ).status_code
            == 200
        )
        legacy = ProductionCore(config)
        assert legacy.auth_service.session_duration_hours == 24
    finally:
        client.close()
        runtime.close(wait=True)


@pytest.mark.parametrize("hours", [0, 73, True, "8"])
def test_auth_service_rejects_invalid_explicit_duration(tmp_path, hours):
    with pytest.raises(ValueError, match="session_duration_hours"):
        AuthService(Database(tmp_path / "unused.sqlite3"), session_duration_hours=hours)


@pytest.mark.parametrize(
    "configured,visible,worker_device",
    [(None, "", "cpu"), ("gpu:0", "0", "gpu:0"), ("gpu:2", "2", "gpu:0")],
)
def test_panel_detector_default_cpu_and_selected_gpu_are_isolated(
    tmp_path, monkeypatch, configured, visible, worker_device
):
    weights = tmp_path / "stub-weights"
    weights.write_bytes(b"test fixture, never loaded")
    classes = {"0": {"name": "panel_a", "type_id": "builtin-stirrer-v1"}}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "weights": str(weights),
                "weights_sha256": hashlib.sha256(weights.read_bytes()).hexdigest(),
                "python": sys.executable,
                "classes": classes,
                "version": "fixture-only",
            }
        )
    )
    monkeypatch.setenv("FIELD_PANEL_MODEL_MANIFEST", str(manifest))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "4")
    monkeypatch.delenv("FIELD_OCR_SERVICE_URL", raising=False)
    if configured is None:
        monkeypatch.delenv("FIELD_PANEL_DEVICE", raising=False)
    else:
        monkeypatch.setenv("FIELD_PANEL_DEVICE", configured)
    calls = []

    def spawn(argv, **options):
        calls.append((argv, options))
        return SimpleNamespace(
            terminate=lambda: None, wait=lambda timeout: None, stdin=None, stdout=None
        )

    monkeypatch.setattr(panel_detector.subprocess, "Popen", spawn)
    detector = panel_detector.PanelDetector()
    monkeypatch.setattr(
        detector, "_receive", lambda timeout: {"status": "ready", "names": {"0": "panel_a"}}
    )
    try:
        detector._start()
        argv, options = calls[0]
        assert options["env"]["CUDA_VISIBLE_DEVICES"] == visible
        assert argv[-1] == worker_device
        assert detector.snapshot()["device"] == (configured or "cpu")
        assert inference_device(argv[-1]) == ("cpu" if worker_device == "cpu" else 0)
        assert os.environ["CUDA_VISIBLE_DEVICES"] == "4"
    finally:
        detector.close()


@pytest.mark.parametrize("value", ["gpu", "gpu:-1", "auto", "gpu:0,1"])
def test_panel_worker_rejects_ambiguous_device_values(value):
    with pytest.raises(ValueError, match="device must be"):
        inference_device(value)


def write_registry(tmp_path, collection, items):
    schema = (
        "FieldRecognitionQrLabels/1"
        if collection == "instruments"
        else "FieldRecognitionSceneQrLabels/1"
    )
    path = tmp_path / f"{collection}.json"
    path.write_text(json.dumps({"schema": schema, collection: items}), encoding="utf-8-sig")
    return path


def test_custom_registry_registers_only_declared_assets_without_qr_evidence(tmp_path):
    iid, sid = str(uuid.uuid4()), str(uuid.uuid4())
    payload = {"v": 1, "type": "instrument", "id": iid}
    ranges = {"温度": {"unit": "°C", "range": [-20, 100], "decimal_places": 1}}
    instruments = write_registry(
        tmp_path,
        "instruments",
        [
            {
                "instrument_id": iid.upper(),
                "label": "自有仪器",
                "scene": "自有实验台",
                "model": "declared-model",
                "device_no": "declared-serial",
                "type_id": "custom-temperature",
                "device_category": "temperature-meter",
                "measurement_ranges": ranges,
                "qr_payload": payload,
                "encoded_text": json.dumps(payload),
                "decode_verified": True,
            }
        ],
    )
    scenes = write_registry(
        tmp_path,
        "scenes",
        [
            {"scene_id": sid, "scene_name": "自有场景"},
            {"scene_id": str(uuid.uuid4()), "scene_name": None},
        ],
    )
    original = instruments.read_bytes()
    database = Database(tmp_path / "custom.sqlite3")
    import_registry(database, instruments, scenes)
    runtime = create_runtime(
        ProductionConfig(
            database_path=str(database.path), record_mode="production", registry_policy="none"
        )
    )
    try:
        with runtime["db"]() as conn:
            row = conn.execute("SELECT * FROM instruments").fetchone()
            assert (row["id"], row["name"], row["scene"], row["model"], row["device_no"]) == (
                iid,
                "自有仪器",
                "自有实验台",
                "declared-model",
                "declared-serial",
            )
            assert row["type_id"] == "custom-temperature"
            assert row["device_category"] == "temperature-meter"
            assert json.loads(row["measurement_ranges"]) == ranges
            assert [tuple(row) for row in conn.execute("SELECT id,name FROM scenes")] == [
                (sid, "自有场景")
            ]
            for table in ("scans", "bindings", "qr_device_registry"):
                assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert runtime["qr_matches"]([], np.empty((0, 4, 2)))["matches"] == []
        assert instruments.read_bytes() == original
    finally:
        runtime.close(wait=True)


def test_custom_registry_reimport_preserves_existing_rows_and_history(tmp_path):
    iid, sid = str(uuid.uuid4()), str(uuid.uuid4())
    instruments = write_registry(
        tmp_path, "instruments", [{"instrument_id": iid, "label": "原名称"}]
    )
    scenes = write_registry(tmp_path, "scenes", [{"scene_id": sid, "scene_name": "原场景"}])
    database = Database(tmp_path / "existing.sqlite3")
    import_registry(database, instruments, scenes)
    with database.connection() as conn:
        conn.execute("UPDATE instruments SET device_no='original-serial' WHERE id=?", (iid,))
        conn.execute("CREATE TABLE bindings(id TEXT PRIMARY KEY,document TEXT)")
        conn.execute("INSERT INTO bindings VALUES('history','original-evidence')")
    write_registry(
        tmp_path,
        "instruments",
        [
            {
                "instrument_id": iid,
                "label": "新名称",
                "device_no": "replacement",
                "model": "new-model",
            }
        ],
    )
    write_registry(tmp_path, "scenes", [{"scene_id": sid, "scene_name": "新场景"}])
    import_registry(database, instruments, scenes)
    with database.connection() as conn:
        assert tuple(conn.execute("SELECT name,device_no,model FROM instruments").fetchone()) == (
            "原名称",
            "original-serial",
            "",
        )
        assert conn.execute("SELECT name FROM scenes").fetchone()[0] == "原场景"
        assert conn.execute("SELECT document FROM bindings").fetchone()[0] == "original-evidence"
    write_registry(
        tmp_path, "instruments", [{"instrument_id": str(uuid.uuid4()), "label": "new identity"}]
    )
    write_registry(tmp_path, "scenes", [{"scene_id": "invalid", "scene_name": "invalid"}])
    with pytest.raises(ValueError, match="UUID"):
        import_registry(database, instruments, scenes)
    with database.connection() as conn:
        assert conn.execute("SELECT count(*) FROM instruments").fetchone()[0] == 1
        assert conn.execute("SELECT document FROM bindings").fetchone()[0] == "original-evidence"


@pytest.mark.parametrize(
    "fields",
    [
        {"instrument_id": "malformed-private-canary"},
        {"label": ""},
        {"measurement_ranges": []},
        {"measurement_ranges": {"温度": {"range": [100, 20]}}},
        {"measurement_ranges": {"温度": {"range": ["1", 20]}}},
        {"measurement_ranges": {"温度": {"decimal_places": 9}}},
        {"measurement_ranges": {"温度": {"unexpected_field": 1}}},
        {"qr_payload": {"v": 1, "type": "scene", "id": SAMPLE_INSTRUMENT}},
        {"qr_payload": {"v": 2, "type": "instrument", "id": SAMPLE_INSTRUMENT}},
        {"qr_payload": {"v": 1, "type": "instrument", "id": SAMPLE_SCENE}},
        {"encoded_text": "malformed-private-canary"},
    ],
)
def test_malformed_registry_is_fully_rejected_before_database_creation(tmp_path, fields):
    path = write_registry(
        tmp_path,
        "instruments",
        [
            {"instrument_id": str(uuid.uuid4()), "label": "valid-first"},
            {"instrument_id": SAMPLE_INSTRUMENT, "label": "valid-second"} | fields,
        ],
    )
    database = Database(tmp_path / "not-created.sqlite3")
    with pytest.raises(ValueError) as error:
        import_registry(database, instrument_registry=path)
    assert "malformed-private-canary" not in str(error.value)
    assert not database.path.exists()


def test_registry_duplicate_identity_and_late_scene_error_do_not_partially_write(tmp_path):
    iid = str(uuid.uuid4())
    instruments = write_registry(
        tmp_path,
        "instruments",
        [
            {"instrument_id": iid, "label": "first"},
            {"instrument_id": iid.upper(), "label": "duplicate"},
        ],
    )
    database = Database(tmp_path / "not-created.sqlite3")
    with pytest.raises(ValueError, match="Duplicate instrument identity"):
        import_registry(database, instrument_registry=instruments)
    assert not database.path.exists()
    write_registry(tmp_path, "instruments", [{"instrument_id": iid, "label": "valid"}])
    scenes = write_registry(tmp_path, "scenes", [{"scene_id": "invalid", "scene_name": "invalid"}])
    with pytest.raises(ValueError, match="UUID"):
        import_registry(database, instruments, scenes)
    assert not database.path.exists()


@pytest.mark.parametrize(
    "data",
    [
        b'{"schema":"wrong","instruments":[]}',
        b'{"schema":"FieldRecognitionQrLabels/1","instruments":{}}',
        b'{"schema":"FieldRecognitionQrLabels/1","instruments":[],"instruments":[]}',
        b'{"malformed-private-canary":',
        b"\xffmalformed-private-canary",
    ],
)
def test_invalid_registry_encoding_structure_and_json_have_content_free_errors(tmp_path, data):
    path = tmp_path / "invalid.json"
    path.write_bytes(data)
    database = Database(tmp_path / "not-created.sqlite3")
    with pytest.raises(ValueError) as error:
        import_registry(database, instrument_registry=path)
    assert "malformed-private-canary" not in str(error.value)
    assert not database.path.exists()


def test_registry_import_requires_an_explicit_path(tmp_path):
    database = Database(tmp_path / "not-created.sqlite3")
    with pytest.raises(ValueError, match="At least one"):
        import_registry(database)
    assert not database.path.exists()
