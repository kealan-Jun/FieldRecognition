"""Install user units only while every existing FieldRecognition service is offline."""

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from field_recognition.runtime.database import Database, get_migration_status
from field_recognition.runtime.prepare_runtime import resolve_runtime_paths

ROOT = Path(__file__).resolve().parents[1]
SERVICE_NAMES = tuple(
    "field-recognition-" + name + ".service"
    for name in ("demo", "api", "ocr", "cameras", "archive", "prepare")
) + ("field-recognition.target",)


def inspect_services(run):
    """Fail closed before touching installed units or private configuration."""
    states = {}
    for unit in SERVICE_NAMES:
        response = run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "MainPID",
                "-p",
                "ControlPID",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        state = dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
        if (
            state.get("LoadState") == "loaded"
            and unit.endswith(".service")
            and not {
                "MainPID",
                "ControlPID",
            }
            <= state.keys()
        ):
            raise RuntimeError("Cannot establish service identity: " + unit)
        try:
            busy = any(int(state.get(key, "0")) for key in ("MainPID", "ControlPID"))
        except ValueError:
            raise RuntimeError("Cannot establish service identity: " + unit) from None
        if busy or state.get("ActiveState") not in {"inactive", "failed"}:
            raise RuntimeError("Installation requires existing services to be offline: " + unit)
        if state.get("LoadState") == "not-found":
            states[unit] = False
        elif response.returncode or state.get("LoadState") != "loaded":
            raise RuntimeError("Cannot inspect installed service: " + unit)
        else:
            states[unit] = True
    return states


def systemd_path(value):
    """Directory/file assignments consume the whole value without token unquoting."""
    if any(character in str(value) for character in ("\n", "\r", "\x00")):
        raise ValueError("Service paths cannot contain a line break or NUL")
    return str(value).replace("%", "%%")


def render_unit(content, root, environment, configuration=None):
    prefix = "%h/Projects/FieldRecognition"
    lines = []
    for line in content.splitlines():
        key, separator, value = line.partition("=")
        if separator and key == "WorkingDirectory" and value == "@PROJECT_ROOT@":
            # WorkingDirectory consumes the whole assignment and does not unquote it.
            line = key + "=" + systemd_path(root)
        elif (
            separator
            and key == "EnvironmentFile"
            and value.lstrip("-")
            in {
                "@SERVICE_ENV_FILE@",
                "@ENV_FILE@",
            }
        ):
            path = (
                environment
                if value.lstrip("-") == "@SERVICE_ENV_FILE@"
                else configuration or root / ".env"
            )
            line = key + "=" + ("-" if value.startswith("-") else "") + systemd_path(path)
        elif separator and key == "ExecStart" and value.startswith("@PROJECT_ROOT@"):
            executable, _, arguments = value.partition(" ")
            if executable != "@PROJECT_ROOT@/.venv/bin/python":
                raise ValueError("Unsupported template executable")
            line = key + "=/usr/bin/env ./.venv/bin/python" + (" " + arguments if arguments else "")
        elif separator and key in {"WorkingDirectory", "EnvironmentFile"} and prefix in value:
            optional = value.startswith("-")
            value = value.removeprefix("-")
            path = root / value.removeprefix(prefix).lstrip("/")
            if path == root / "Data/Runtime/ServiceEnvironment.env":
                path = environment
            elif path == root / ".env" and configuration is not None:
                path = configuration
            line = key + "=" + ("-" if optional else "") + systemd_path(path)
        elif separator and key == "ExecStart" and value.startswith(prefix):
            executable, _, arguments = value.partition(" ")
            if executable != prefix + "/.venv/bin/python":
                raise ValueError("Unsupported legacy template executable")
            line = key + "=/usr/bin/env ./.venv/bin/python" + (" " + arguments if arguments else "")
        lines.append(line)
    if any(
        placeholder in line
        for line in lines
        for placeholder in ("@PROJECT_ROOT@", "@SERVICE_ENV_FILE@", "@ENV_FILE@")
    ):
        raise ValueError("Unresolved service template path")
    return "\n".join(lines) + "\n"


def write_private(path, text):
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
        handle.write(text)


def install_runtime(
    project_root=ROOT,
    *,
    no_start=False,
    init_env=False,
    env_file=None,
    run=None,
    unit_directory=None,
):
    run = subprocess.run if run is None else run
    root = Path(project_root).resolve()
    states = inspect_services(run)
    configuration = Path(env_file).expanduser() if env_file else root / ".env"
    if not configuration.is_absolute():
        configuration = root / configuration
    configuration = configuration.resolve()
    if not configuration.is_file():
        if not init_env:
            raise RuntimeError(
                "Missing .env; copy .env.example to .env or pass --init-env, then configure this deployment"
            )
        example = root / ".env.example"
        if not example.is_file():
            raise RuntimeError("Missing .env.example; provide a reviewed .env before installation")
        # Check every read-only precondition before creating either configuration file.
        configuration_text = example.read_text()
    else:
        configuration_text = None
    path_environment = dict(os.environ)
    from field_recognition.runtime.environment import read_environment_file

    # Match the paths the installed units will actually load. Only these two
    # settings affect preflight; do not load credentials into this process.
    for path in (root / "Data/Runtime/ServiceEnvironment.env", configuration):
        if path.is_file():
            values = read_environment_file(path)
            for key in ("FIELD_DEMO_DATA", "FIELD_DATABASE_PATH"):
                if key in values:
                    path_environment[key] = values[key]
    data, database = resolve_runtime_paths(project_root=root, environment=path_environment)
    if not database.is_file() or get_migration_status(Database(database))["pending"]:
        raise RuntimeError("Run prepare_runtime.py first for database " + str(database))
    runtime = data / "Runtime"
    environment = runtime / "ServiceEnvironment.env"
    legacy = states["field-recognition-demo.service"]
    environment_text = None
    if not environment.exists():
        lines = []
        if legacy:
            result = run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    "field-recognition-demo.service",
                    "--property=Environment",
                    "--value",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            for token in shlex.split(result.stdout):
                key, separator, value = token.partition("=")
                if separator and key not in {
                    "FIELD_PRODUCTION_ENABLED",
                    "FIELD_SERVICE_ROLE",
                    "FIELD_DEMO_DATA",
                    "FIELD_DATABASE_PATH",
                }:
                    lines.append(key + "=" + json.dumps(value, ensure_ascii=False))
        lines.extend(
            key + "=" + json.dumps(str(value), ensure_ascii=False)
            for key, value in (("FIELD_DEMO_DATA", data), ("FIELD_DATABASE_PATH", database))
        )
        environment_text = "\n".join(lines) + "\n"
    sources = [
        *sorted((root / "deployment").glob("field-recognition-*.service")),
        root / "deployment/field-recognition.target",
    ]
    sources = [path for path in sources if path.name != "field-recognition-demo.service"]
    rendered = {
        path.name: render_unit(path.read_text(), root, environment, configuration)
        for path in sources
    }
    # Recheck after preflight; installation never stops or restarts a service.
    inspect_services(run)
    if configuration_text is not None:
        write_private(configuration, configuration_text)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    if environment_text is not None:
        write_private(environment, environment_text)
    units = (
        Path(unit_directory) if unit_directory is not None else Path.home() / ".config/systemd/user"
    )
    units.mkdir(parents=True, exist_ok=True)
    for name, content in rendered.items():
        (units / name).write_text(content)
    if legacy:
        run(
            ["systemctl", "--user", "disable", "field-recognition-demo.service"],
            check=True,
            timeout=10,
        )
    run(["systemctl", "--user", "daemon-reload"], check=True, timeout=10)
    run(
        [
            "systemctl",
            "--user",
            "enable",
            *([] if no_start else ["--now"]),
            "field-recognition.target",
        ],
        check=True,
        timeout=30,
    )
    return {
        "status": "installed",
        "started": not no_start,
        "database": str(database),
        "data": str(data),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-start", action="store_true", help="Install and enable units without starting services"
    )
    parser.add_argument(
        "--init-env",
        action="store_true",
        help="Create a missing .env from .env.example with mode 0600",
    )
    parser.add_argument(
        "--env-file",
        help="Systemd configuration file; its database/data paths govern installation preflight",
    )
    args = parser.parse_args(argv)
    result = install_runtime(no_start=args.no_start, init_env=args.init_env, env_file=args.env_file)
    print(
        "Managed local units installed"
        + (" and started" if result["started"] else " without starting")
    )


if __name__ == "__main__":
    main()
