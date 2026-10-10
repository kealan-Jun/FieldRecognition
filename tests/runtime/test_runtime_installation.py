"""First-install and offline-upgrade contracts with a simulated user systemd manager."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from field_recognition.runtime.database import Database, apply_migrations
from field_recognition.runtime.paths import PROJECT_ROOT
from field_recognition.runtime.prepare_runtime import main as prepare_main
from field_recognition.runtime.prepare_runtime import prepare, resolve_runtime_paths
from scripts.install_runtime import SERVICE_NAMES, install_runtime, render_unit, systemd_path


@pytest.fixture(autouse=True)
def isolated_paths(monkeypatch):
    for key in (
        "FIELD_DEMO_DATA",
        "FIELD_DATABASE_PATH",
        "FIELD_LOGIN_URL",
        "FIELD_HOST",
        "FIELD_PORT",
        "FIELD_AUTH_ENABLED",
        "FIELD_RECEIVER_URL",
        "FIELD_CAMERA_ID",
        "FIELD_SAVED_PHOTO_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'Field Recognition % "demo" $clone'
    root.mkdir()
    shutil.copytree(PROJECT_ROOT / "deployment", root / "deployment")
    shutil.copyfile(PROJECT_ROOT / ".env.example", root / ".env.example")
    apply_migrations(Database(root / "Data/Demo.sqlite3"))
    return root


class Manager:
    def __init__(self, *, existing=(), busy=None, fail_inspection=False, becomes_busy=False):
        self.existing = set(existing)
        self.busy = busy
        self.fail_inspection = fail_inspection
        self.becomes_busy = becomes_busy
        self.calls = []
        self.inspections = 0

    def run(self, args, **kwargs):
        self.calls.append(args)
        assert args[:2] == ["systemctl", "--user"]
        if args[2] == "show":
            unit = args[3]
            if "--property=Environment" in args:
                assert unit in self.existing
                return SimpleNamespace(
                    returncode=0,
                    stdout="FIELD_SERVICE_ROLE=combined FIELD_PRODUCTION_ENABLED=0 FIELD_OCR_DEVICE=cpu FIELD_CAMERA_ID=registered-camera\n",
                )
            self.inspections += 1
            if self.fail_inspection:
                return SimpleNamespace(returncode=1, stdout="")
            load = "loaded" if unit in self.existing else "not-found"
            busy = unit == self.busy or (
                self.becomes_busy and self.inspections > len(SERVICE_NAMES)
            )
            state = "active" if busy else "inactive"
            pid = 4242 if busy else 0
            return SimpleNamespace(
                returncode=0,
                stdout=f"LoadState={load}\nActiveState={state}\nMainPID={pid}\nControlPID=0\n",
            )
        assert args[2] in {"disable", "daemon-reload", "enable"}
        assert "stop" not in args and "restart" not in args
        if args[2] == "disable":
            assert args[3] in self.existing and "--now" not in args
        return SimpleNamespace(returncode=0, stdout="")


def install(project, tmp_path, manager, **settings):
    return install_runtime(project, run=manager.run, unit_directory=tmp_path / "units", **settings)


def test_first_install_without_legacy_unit_initializes_private_environment(project, tmp_path):
    manager = Manager()
    result = install(project, tmp_path, manager, init_env=True)
    assert result["started"] is True
    assert (project / ".env").stat().st_mode & 0o777 == 0o600
    environment = project / "Data/Runtime/ServiceEnvironment.env"
    assert environment.stat().st_mode & 0o777 == 0o600
    assert "FIELD_DATABASE_PATH=" in environment.read_text()
    assert not any(args[2] == "disable" for args in manager.calls)
    assert not any("--property=Environment" in args for args in manager.calls)
    assert manager.calls[-1] == [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "field-recognition.target",
    ]
    unit = (tmp_path / "units/field-recognition-api.service").read_text()
    assert "ExecStart=/usr/bin/env ./.venv/bin/python -m field_recognition\n" in unit
    assert "%h/Projects/FieldRecognition" not in unit
    assert "%%" in unit and '"demo"' in unit and "$clone" in unit


def test_missing_configuration_fails_before_any_installed_file_change(project, tmp_path):
    manager = Manager()
    with pytest.raises(RuntimeError, match="Missing .env"):
        install(project, tmp_path, manager, no_start=True)
    assert not (project / "Data/Runtime").exists()
    assert not (tmp_path / "units").exists()
    assert all(args[2] == "show" for args in manager.calls)


@pytest.mark.parametrize("unit", SERVICE_NAMES)
def test_any_active_existing_service_refuses_even_no_start_before_writes(project, tmp_path, unit):
    manager = Manager(existing=SERVICE_NAMES, busy=unit)
    with pytest.raises(RuntimeError, match="offline"):
        install(project, tmp_path, manager, init_env=True, no_start=True)
    assert not (project / ".env").exists()
    assert not (project / "Data/Runtime").exists()
    assert not (tmp_path / "units").exists()
    assert all(args[2] == "show" for args in manager.calls)


def test_service_becoming_active_during_preflight_still_prevents_writes(project, tmp_path):
    manager = Manager(becomes_busy=True)
    with pytest.raises(RuntimeError, match="offline"):
        install(project, tmp_path, manager, init_env=True)
    assert not (project / ".env").exists()
    assert not (tmp_path / "units").exists()


def test_unavailable_user_manager_fails_closed_before_writes(project, tmp_path):
    with pytest.raises(RuntimeError):
        install(project, tmp_path, Manager(fail_inspection=True), init_env=True)
    assert not (project / ".env").exists()
    assert not (tmp_path / "units").exists()


def test_loaded_service_with_missing_pid_identity_refuses_before_writes(project, tmp_path):
    def run(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="LoadState=loaded\nActiveState=inactive\n")

    with pytest.raises(RuntimeError, match="identity"):
        install_runtime(
            project, init_env=True, no_start=True, run=run, unit_directory=tmp_path / "units"
        )
    assert not (project / ".env").exists()
    assert not (tmp_path / "units").exists()


def test_offline_legacy_install_preserves_settings_without_stop_or_start(project, tmp_path):
    (project / ".env").write_text("FIELD_AUTH_ENABLED=0\n")
    manager = Manager(existing=["field-recognition-demo.service"])
    result = install(project, tmp_path, manager, no_start=True)
    assert result["started"] is False
    text = (project / "Data/Runtime/ServiceEnvironment.env").read_text()
    assert 'FIELD_OCR_DEVICE="cpu"' in text and 'FIELD_CAMERA_ID="registered-camera"' in text
    assert "FIELD_SERVICE_ROLE=" not in text and "FIELD_PRODUCTION_ENABLED=" not in text
    assert ["systemctl", "--user", "disable", "field-recognition-demo.service"] in manager.calls
    assert manager.calls[-1] == ["systemctl", "--user", "enable", "field-recognition.target"]


def test_explicit_environment_file_is_the_file_installed_units_use(project, tmp_path):
    data = tmp_path / "Evidence Store"
    database = tmp_path / "Private DB/state.sqlite3"
    apply_migrations(Database(database))
    configuration = tmp_path / "deployment config.env"
    configuration.write_text(f'FIELD_DEMO_DATA="{data}"\nFIELD_DATABASE_PATH="{database}"\n')
    result = install(project, tmp_path, Manager(), env_file=configuration, no_start=True)
    assert result["database"] == str(database)
    assert result["data"] == str(data)
    text = (tmp_path / "units/field-recognition-api.service").read_text()
    assert f"EnvironmentFile={systemd_path(configuration)}\n" in text
    assert f"EnvironmentFile=-{systemd_path(data / 'Runtime/ServiceEnvironment.env')}\n" in text
    assert not (project / ".env").exists()


def test_installer_checks_the_environment_file_database_before_writing(project, tmp_path):
    (project / ".env").write_text('FIELD_DATABASE_PATH="Missing/State.sqlite3"\n')
    with pytest.raises(RuntimeError, match="prepare_runtime.py first"):
        install(project, tmp_path, Manager(), no_start=True)
    assert not (project / "Missing").exists()
    assert not (tmp_path / "units").exists()


def test_render_quotes_optional_and_required_paths_without_changing_arguments():
    root = Path('/tmp/clone % "with spaces"')
    environment = Path("/tmp/private env/ServiceEnvironment.env")
    content = "WorkingDirectory=%h/Projects/FieldRecognition\nEnvironmentFile=-%h/Projects/FieldRecognition/Data/Runtime/ServiceEnvironment.env\nEnvironmentFile=%h/Projects/FieldRecognition/.env\nExecStart=%h/Projects/FieldRecognition/.venv/bin/python -m ocr_worker --persistent\n"
    result = render_unit(content, root, environment)
    assert 'WorkingDirectory=/tmp/clone %% "with spaces"\n' in result
    assert f"EnvironmentFile=-{systemd_path(environment)}\n" in result
    assert result.endswith(" -m ocr_worker --persistent\n")


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd parser unavailable")
def test_actual_systemd_parser_accepts_rendered_clone_paths_with_special_characters(
    project, tmp_path
):
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    configuration = project / ".env"
    configuration.write_text("FIELD_AUTH_ENABLED=0\n")
    environment = project / "Data/Runtime/ServiceEnvironment.env"
    environment.parent.mkdir()
    environment.write_text("FIELD_AUTH_ENABLED=0\n")
    units = tmp_path / "syntax-units"
    units.mkdir()
    for source in (project / "deployment").glob("field-recognition-*.service"):
        (units / source.name).write_text(
            render_unit(source.read_text(), project, environment, configuration)
        )
    shutil.copyfile(
        project / "deployment/field-recognition.target", units / "field-recognition.target"
    )
    for name in (
        "network-online",
        "basic",
        "sysinit",
        "sockets",
        "paths",
        "timers",
        "shutdown",
        "default",
    ):
        (units / (name + ".target")).write_text(
            "[Unit]\nDescription=Offline syntax fixture\nDefaultDependencies=no\n"
        )
    runtime = tmp_path / "user-runtime"
    runtime.mkdir(mode=0o700)
    # An isolated graph prevents the parser from loading installed user units.
    environment = dict(
        os.environ,
        SYSTEMD_UNIT_PATH=str(units),
        XDG_RUNTIME_DIR=str(runtime),
        SYSTEMD_LOG_LEVEL="warning",
    )
    result = subprocess.run(
        [
            "systemd-analyze",
            "--user",
            "verify",
            "--man=no",
            *map(str, sorted(units.glob("field-recognition-*"))),
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_prepare_environment_database_and_data_match_resolved_runtime_paths(tmp_path, monkeypatch):
    data = tmp_path / "evidence"
    database = tmp_path / "db/Custom.sqlite3"
    monkeypatch.setenv("FIELD_DEMO_DATA", str(data))
    monkeypatch.setenv("FIELD_DATABASE_PATH", str(database))
    result = prepare(None, "operator", "操作人", "portable-camera")
    assert result["database"] == str(database)
    assert not (data / "Demo.sqlite3").exists()
    with Database(database).connection() as conn:
        assert conn.execute("SELECT username FROM users").fetchone()[0] == "operator"


def test_relative_paths_resolve_from_clone_root_without_environment_mutation(tmp_path):
    environment = {"FIELD_DATABASE_PATH": "state/custom.sqlite3", "FIELD_DEMO_DATA": "photos"}
    data, database = resolve_runtime_paths(project_root=tmp_path, environment=environment)
    assert data == tmp_path / "photos" and database == tmp_path / "state/custom.sqlite3"
    assert environment == {
        "FIELD_DATABASE_PATH": "state/custom.sqlite3",
        "FIELD_DEMO_DATA": "photos",
    }
    assert resolve_runtime_paths(
        database_path="custom.sqlite3", project_root=tmp_path, environment={}
    ) == (tmp_path, tmp_path / "custom.sqlite3")


@pytest.mark.parametrize("explicit", [None, "https://field.example/workbench/login"])
def test_prepare_login_url_uses_configured_listener_or_explicit_url(
    tmp_path, monkeypatch, explicit
):
    monkeypatch.setenv("FIELD_HOST", "0.0.0.0")
    monkeypatch.setenv("FIELD_PORT", "18222")
    prepare(tmp_path, "admin", "管理员", None, auth_enabled=True, login_url=explicit)
    access = json.loads((tmp_path / "Access/InitialAccess.json").read_text())
    assert access["login_url"] == (explicit or "http://127.0.0.1:18222/login")
    assert (tmp_path / "Access/InitialAccess.json").stat().st_mode & 0o777 == 0o600


def test_invalid_login_url_fails_before_creating_registration_or_credentials(tmp_path):
    data = tmp_path / "not-created"
    with pytest.raises(ValueError, match="Login URL"):
        prepare(
            data,
            "admin",
            "管理员",
            None,
            auth_enabled=True,
            login_url="https://secret:password@example/login",
        )
    assert not data.exists()


def test_prepare_never_imports_site_sample_identities_without_explicit_request(tmp_path):
    result = prepare(tmp_path, "operator", "操作人", None)
    with Database(result["database"]).connection() as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "instruments" in tables:
            assert conn.execute("SELECT count(*) FROM instruments").fetchone()[0] == 0


def test_prepare_explicit_sample_import_is_idempotent_and_preserves_registration(tmp_path):
    result = prepare(tmp_path, "operator", "操作人", None, registry_policy="sample")
    registry = json.loads((PROJECT_ROOT / "InstrumentRegistry.json").read_text())
    sample_id = registry["instruments"][0]["instrument_id"]
    with Database(result["database"]).connection() as conn:
        before = conn.execute("SELECT count(*) FROM instruments").fetchone()[0]
        assert conn.execute("SELECT id FROM instruments WHERE id=?", (sample_id,)).fetchone()
        conn.execute("UPDATE instruments SET name='用户已登记名称' WHERE id=?", (sample_id,))
    again = prepare(tmp_path, "operator", "操作人", None, registry_policy="sample")
    with Database(again["database"]).connection() as conn:
        assert conn.execute("SELECT count(*) FROM instruments").fetchone()[0] == before
        assert (
            conn.execute("SELECT name FROM instruments WHERE id=?", (sample_id,)).fetchone()[0]
            == "用户已登记名称"
        )
        for table in ("scans", "bindings"):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_prepare_cli_explicit_environment_file_configures_the_registered_source(
    tmp_path, monkeypatch, capsys
):
    data = tmp_path / "ConfiguredData"
    database = tmp_path / "Database/state.sqlite3"
    configuration = tmp_path / "source.env"
    configuration.write_text(
        f'FIELD_DEMO_DATA="{data}"\nFIELD_DATABASE_PATH="{database}"\nFIELD_CAMERA_ID=configured-camera\nFIELD_RECEIVER_URL=http://receiver.invalid:8080\nFIELD_SAVED_PHOTO_ROOT=/offline/source-fixture\n'
    )
    # load_environment_file changes this process only; monkeypatch restores its additions.
    for key in (
        "FIELD_DEMO_DATA",
        "FIELD_DATABASE_PATH",
        "FIELD_CAMERA_ID",
        "FIELD_RECEIVER_URL",
        "FIELD_SAVED_PHOTO_ROOT",
    ):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    prepare_main(
        ["--env-file", str(configuration), "--username", "operator", "--display-name", "操作人"]
    )
    assert json.loads(capsys.readouterr().out)["database"] == str(database)
    with Database(database).connection() as conn:
        row = conn.execute(
            "SELECT * FROM camera_registry WHERE camera_id='configured-camera'"
        ).fetchone()
        assert row["receiver_url"] == "http://receiver.invalid:8080"
        assert row["nas_photo_root"] == "/offline/source-fixture"
    assert not (data / "Demo.sqlite3").exists()


@pytest.fixture
def custom_registries(tmp_path):
    directory = tmp_path / "Registries"
    directory.mkdir()
    instrument_id = "11111111-1111-4111-8111-111111111111"
    scene_id = "22222222-2222-4222-8222-222222222222"
    instruments = directory / "Instruments.json"
    scenes = directory / "Scenes.json"
    instruments.write_text(
        json.dumps(
            {
                "schema": "FieldRecognitionQrLabels/1",
                "instruments": [
                    {
                        "instrument_id": instrument_id,
                        "label": "自有仪器",
                        "type_id": "builtin-balance-v1",
                        "device_category": "instrument",
                        "measurement_ranges": {"质量": {"unit": "g", "range": [0, 200]}},
                        "scene": "自有实验台",
                        "model": "Custom-Model",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    scenes.write_text(
        "\ufeff"
        + json.dumps(
            {
                "schema": "FieldRecognitionSceneQrLabels/1",
                "scenes": [{"scene_id": scene_id, "scene_name": "自有实验台"}],
            },
            ensure_ascii=False,
        )
    )
    return instruments, scenes, instrument_id, scene_id


def test_prepare_explicit_custom_registries_import_only_user_identities_and_preserve_updates(
    tmp_path, custom_registries
):
    _, _, instrument_id, scene_id = custom_registries
    arguments = dict(
        instrument_registry="Registries/Instruments.json",
        scene_registry="Registries/Scenes.json",
        registry_policy="none",
        project_root=tmp_path,
    )
    result = prepare(tmp_path / "Data", "operator", "操作人", None, **arguments)
    with Database(result["database"]).connection() as conn:
        assert conn.execute("SELECT count(*) FROM instruments").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM scenes").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM instruments WHERE id=?", (instrument_id,)).fetchone()
        assert row["name"] == "自有仪器" and row["model"] == "Custom-Model"
        assert json.loads(row["measurement_ranges"])["质量"]["range"] == [0, 200]
        conn.execute(
            "UPDATE instruments SET name='已修订名称',device_no='现场编号001' WHERE id=?",
            (instrument_id,),
        )
        conn.execute("UPDATE scenes SET name='已修订场景' WHERE id=?", (scene_id,))
    again = prepare(tmp_path / "Data", "operator", "操作人", None, **arguments)
    with Database(again["database"]).connection() as conn:
        assert conn.execute("SELECT count(*) FROM instruments").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM scenes").fetchone()[0] == 1
        assert tuple(
            conn.execute(
                "SELECT name,device_no FROM instruments WHERE id=?", (instrument_id,)
            ).fetchone()
        ) == ("已修订名称", "现场编号001")
        assert (
            conn.execute("SELECT name FROM scenes WHERE id=?", (scene_id,)).fetchone()[0]
            == "已修订场景"
        )
        for table in ("scans", "bindings", "qr_device_registry"):
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                assert conn.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_prepare_cli_imports_custom_instrument_file_without_implicit_scenes(
    tmp_path, custom_registries, capsys
):
    instruments, _, instrument_id, _ = custom_registries
    data = tmp_path / "cli-data"
    prepare_main(
        [
            "--data",
            str(data),
            "--username",
            "operator",
            "--display-name",
            "操作人",
            "--registry-policy",
            "none",
            "--instrument-registry",
            str(instruments),
        ]
    )
    result = json.loads(capsys.readouterr().out)
    with Database(result["database"]).connection() as conn:
        assert conn.execute("SELECT id FROM instruments").fetchone()[0] == instrument_id
        assert conn.execute("SELECT count(*) FROM instruments").fetchone()[0] == 1
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='scenes'").fetchone():
            assert conn.execute("SELECT count(*) FROM scenes").fetchone()[0] == 0


def test_prepare_scene_only_registry_can_expand_user_path(tmp_path, custom_registries, monkeypatch):
    _, _, _, scene_id = custom_registries
    expanduser = os.path.expanduser
    monkeypatch.setattr(
        os.path,
        "expanduser",
        lambda value: str(tmp_path) if value == "~" else expanduser(value),
    )
    result = prepare(
        tmp_path / "scene-data",
        "operator",
        "操作人",
        None,
        scene_registry="~/Registries/Scenes.json",
        project_root=tmp_path / "unused-source-root",
    )
    with Database(result["database"]).connection() as conn:
        assert conn.execute("SELECT id FROM scenes").fetchone()[0] == scene_id
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='instruments'").fetchone():
            assert conn.execute("SELECT count(*) FROM instruments").fetchone()[0] == 0


def test_prepare_rejects_sample_and_custom_registry_before_any_data_mutation(
    tmp_path, custom_registries
):
    instruments, _, _, _ = custom_registries
    data = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="cannot be combined"):
        prepare(
            data,
            "operator",
            "操作人",
            None,
            registry_policy="sample",
            instrument_registry=instruments,
        )
    assert not data.exists()
