"""IPC uses each explicit runtime's data directory, independent of process env."""

import asyncio

import numpy as np
import pytest

from field_recognition.runtime.database import Database, apply_migrations
from field_recognition.runtime.factory import create_runtime
from field_recognition.runtime.ocr_worker import OCRWorker
from field_recognition.runtime.production_config import ProductionConfig
from field_recognition.runtime.runtime_rpc import RpcServer, call, socket_path


def test_two_api_runtimes_keep_same_named_proxies_in_their_own_directory(tmp_path, monkeypatch):
    runtimes = []
    servers = []
    ambient = tmp_path / "ambient"
    monkeypatch.setenv("FIELD_DEMO_DATA", str(ambient))
    try:
        for name in ("a", "b"):
            data = tmp_path / name
            runtime = create_runtime(
                ProductionConfig(
                    database_path=str(data / "State.sqlite3"),
                    camera_id="same-camera",
                    service_role="api",
                )
            )
            runtimes.append(runtime)
            camera = RpcServer(
                "camera:same-camera",
                {
                    "receiver.snapshot": lambda name=name: {"runtime": name},
                    "receiver.frame": lambda name=name: [name, {}],
                    "receiver.preview": lambda *_args, name=name: name,
                    "scanner.snapshot": lambda name=name: {"runtime": name},
                    "automation.settings": lambda name=name: {"runtime": name},
                    "watcher.snapshot": lambda name=name: {"runtime": name},
                    "video.preview": lambda name=name: name,
                },
                data_dir=runtime.data_dir,
            )
            ocr = RpcServer(
                "ocr",
                {
                    "predict_panel": lambda _panel, *_args, name=name: {"runtime": name},
                    "predict_readout": lambda _panel, *_args, name=name, **kwargs: {
                        "runtime": name,
                        "options": kwargs,
                    },
                    "detector.snapshot": lambda name=name: {"runtime": name},
                    "warmup": lambda: True,
                    "status": lambda name=name: {"runtime": name},
                },
                data_dir=runtime.data_dir,
            )
            for server in (camera, ocr):
                server.start()
                servers.append(server)
        # A changed environment must not reroute an already-created proxy.
        monkeypatch.setenv("FIELD_DEMO_DATA", str(tmp_path / "later"))
        panel = np.zeros((4, 4, 3), dtype=np.uint8)
        for runtime, name in zip(runtimes, ("a", "b")):
            assert runtime["receiver_camera"].snapshot() == {"runtime": name}
            assert runtime["receiver_camera"].frame()[0] == name
            assert runtime["receiver_camera"].preview_part(False, None) == name
            assert runtime["live_scanner"].session == {"runtime": name}
            assert runtime["live_scanner"].camera().snapshot() == {"runtime": name}
            assert runtime["automatic_runner"].settings() == {"runtime": name}
            assert runtime["saved_photo_watcher"].snapshot() == {"runtime": name}
            assert runtime["video_ocr"].preview() == name
            assert runtime["panel_detector"].snapshot() == {"runtime": name}
            assert runtime["panel_detector"].warmup() is True
            assert runtime["predict_panel"](panel) == {"runtime": name}
            assert runtime["predict_readout"](panel, video=True) == {
                "runtime": name,
                "options": {"video": True},
            }
            runtime["remote_warmup"]()
            assert runtime["ocr_state"]["runtime"] == name
        assert not ambient.exists()
        assert not (tmp_path / "later").exists()
        assert len({server.path for server in servers}) == 4
    finally:
        for server in servers:
            server.close()
        for runtime in runtimes:
            runtime.close(wait=True)


def test_ocr_worker_binds_server_to_configured_directory(tmp_path, monkeypatch):
    from field_recognition.runtime import runtime_rpc

    data = tmp_path / "worker"
    database = Database(data / "State.sqlite3")
    apply_migrations(database)
    worker = OCRWorker(
        "stub-worker", ProductionConfig(database_path=str(database.path), production_enabled=True)
    )
    worker.core["queue_ocr_warmup"] = lambda: None
    monkeypatch.setenv("FIELD_DEMO_DATA", str(tmp_path / "ambient"))
    observed = []
    original = runtime_rpc.RpcServer

    def server_factory(name, handlers, *, data_dir):
        server = original(name, handlers, data_dir=data_dir)
        start = server.start

        def start_and_inspect():
            start()
            observed.append((server.path, call("ocr", "status", data_dir=worker.core.data_dir)))

        server.start = start_and_inspect
        return server

    monkeypatch.setattr(runtime_rpc, "RpcServer", server_factory)
    worker.run(persistent=False)
    assert observed[0][0].parent == data / "Runtime"
    assert observed[0][1]["status"] == "not_loaded"
    assert not observed[0][0].exists()
    assert not (tmp_path / "ambient").exists()
    assert worker.core["ocr_pool"]._shutdown and worker.core["readout_pool"]._shutdown


def test_capture_worker_failed_server_start_cleans_the_explicit_instance(tmp_path, monkeypatch):
    from field_recognition.runtime import capture_worker

    database = Database(tmp_path / "capture" / "State.sqlite3")
    apply_migrations(database)
    runtime = create_runtime(
        ProductionConfig(
            database_path=str(database.path),
            production_enabled=True,
            service_role="capture",
            camera_id="same-camera",
        )
    )

    class Camera:
        closed = False

        def snapshot(self):
            return {}

        def frame(self):
            return None

        def close(self):
            self.closed = True

    camera = Camera()
    runtime["receiver_camera"] = camera
    observed = []

    def server_factory(name, handlers, *, data_dir):
        server = RpcServer(name, handlers, data_dir=data_dir)
        observed.append(server)
        monkeypatch.setattr(
            server.thread,
            "start",
            lambda: (_ for _ in ()).throw(RuntimeError("stub start failure")),
        )
        monkeypatch.setattr(
            server.server,
            "shutdown",
            lambda: pytest.fail("Never-started server cannot be shutdown"),
        )
        return server

    monkeypatch.setattr(capture_worker, "RpcServer", server_factory)
    monkeypatch.setenv("FIELD_DEMO_DATA", str(tmp_path / "ambient"))
    with pytest.raises(RuntimeError, match="stub start failure"):
        asyncio.run(capture_worker.run(core=runtime))
    assert observed[0].path.parent == runtime.data_dir / "Runtime"
    assert observed[0].closed and not observed[0].path.exists()
    assert camera.closed
    assert runtime["ocr_pool"]._shutdown and runtime["readout_pool"]._shutdown
    assert not (tmp_path / "ambient").exists()


def test_unstarted_server_close_is_idempotent_and_legacy_env_default_remains(tmp_path, monkeypatch):
    monkeypatch.setenv("FIELD_DEMO_DATA", str(tmp_path))
    server = RpcServer("legacy", {"identity": lambda value: value})
    assert server.path == socket_path("legacy")
    monkeypatch.setattr(
        server.server, "shutdown", lambda: pytest.fail("No serve_forever has started")
    )
    server.close()
    server.close()
    assert not server.path.exists()
    running = RpcServer("legacy", {"identity": lambda value: value})
    running.start()
    try:
        assert call("legacy", "identity", "offline fixture") == "offline fixture"
    finally:
        running.close()
