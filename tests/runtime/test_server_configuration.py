"""The documented launcher honors explicit profiles without touching hardware."""

import os
import subprocess
import sys

import pytest

from field_recognition.runtime import server
from field_recognition.runtime.environment import load_environment_file, read_environment_file
from field_recognition.runtime.paths import PROJECT_ROOT
from scripts.control import configured_url


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for key in os.environ:
        if key.startswith("FIELD_"):
            monkeypatch.delenv(key)


def test_launcher_uses_profile_and_cli_overrides(tmp_path, monkeypatch):
    profile = tmp_path / "deployment.env"
    profile.write_text("FIELD_HOST=0.0.0.0\nFIELD_PORT=18188\n")
    calls = []
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setenv("FIELD_PORT", "18189")
    server.main(["--env-file", str(profile), "--port", "18190"])
    assert calls == [
        (
            ("field_recognition.runtime.server:app_factory",),
            {"factory": True, "host": "0.0.0.0", "port": 18190},
        )
    ]
    assert not (tmp_path / "Data").exists()


def test_profile_is_literal_and_does_not_search_or_override(tmp_path, monkeypatch):
    profile = tmp_path / "selected.env"
    profile.write_text('FIELD_PORT=18188\nFIELD_CAMERA_ID="${HOSTNAME}$(printf secret)"\n')
    monkeypatch.setenv("FIELD_PORT", "18189")
    values = read_environment_file(profile)
    assert values["FIELD_CAMERA_ID"] == "${HOSTNAME}$(printf secret)"
    assert "FIELD_CAMERA_ID" not in os.environ
    load_environment_file(profile)
    assert os.environ["FIELD_PORT"] == "18189"
    with pytest.raises(ValueError, match="does not exist"):
        load_environment_file(tmp_path / "missing.env")


@pytest.mark.parametrize(
    "host, expected", [("0.0.0.0", "127.0.0.1"), ("::", "[::1]"), ("::1", "[::1]")]
)
def test_local_control_uses_configured_port_and_connectable_bind_host(monkeypatch, host, expected):
    monkeypatch.setenv("FIELD_HOST", host)
    monkeypatch.setenv("FIELD_PORT", "18189")
    assert configured_url() == f"http://{expected}:18189"


def test_module_help_does_not_create_runtime(tmp_path):
    environment = dict(os.environ, FIELD_DEMO_DATA=str(tmp_path / "unused"))
    result = subprocess.run(
        [sys.executable, "-m", "field_recognition", "--help"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "--env-file" in result.stdout
    assert not (tmp_path / "unused").exists()
