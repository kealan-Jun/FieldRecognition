"""Factories dispose started, partially started and cold resources safely."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Timer

import pytest

from field_recognition.recognition.remote_ocr import RemoteOcr, RemoteOcrConfig
from field_recognition.runtime.archive_worker import ArchiveWorker
from field_recognition.runtime.context import RuntimeContext
from field_recognition.runtime.database import Database, apply_migrations
from field_recognition.runtime.factory import create_runtime
from field_recognition.runtime.ocr_worker import OCRWorker
from field_recognition.runtime.production_config import ProductionConfig


def assert_disposed(runtime):
    assert runtime["stopping"].is_set()
    for name in ("ocr_pool", "readout_pool"):
        with pytest.raises(RuntimeError, match="shutdown"):
            runtime[name].submit(lambda: None)


def test_api_context_exception_disposes_its_cold_runtime(tmp_path):
    runtime = create_runtime(
        ProductionConfig(database_path=str(tmp_path / "api.sqlite3"), service_role="api")
    )

    async def fail_request_context():
        async with runtime["lifespan"](runtime["app"]):
            raise ValueError("stub context failure")

    with pytest.raises(ValueError, match="stub context failure"):
        asyncio.run(fail_request_context())
    assert_disposed(runtime)
    assert runtime["ocr_model"] is None
    runtime.close(wait=True)


@pytest.mark.parametrize("failure", ["camera", "watcher"])
def test_failed_startup_closes_resources_already_started(tmp_path, monkeypatch, failure):
    runtime = create_runtime(ProductionConfig(database_path=str(tmp_path / "startup.sqlite3")))
    observed = []

    class Camera:
        target = "stub-camera"
        closed = False

        def start(self):
            observed.append("camera_started")
            if failure == "camera":
                raise ValueError("stub startup failure")

        def close(self):
            self.closed = True

    camera = Camera()
    runtime["receiver_camera"] = camera
    monkeypatch.setattr(
        runtime["live_scanner"],
        "observe_service",
        lambda event: observed.append(event["failure_reason"]),
    )
    monkeypatch.setattr(
        runtime["saved_photo_watcher"],
        "start",
        lambda: (_ for _ in ()).throw(ValueError("stub startup failure")),
    )
    runtime["queue_ocr_warmup"] = lambda: pytest.fail("Cold startup must not load models")

    async def start():
        async with runtime["lifespan"](runtime["app"]):
            pytest.fail("Startup must fail before yielding")

    with pytest.raises(ValueError, match="stub startup failure"):
        asyncio.run(start())
    assert camera.closed
    assert runtime["saved_photo_watcher"].stop.is_set()
    assert_disposed(runtime)
    assert runtime["ocr_model"] is None
    assert ("capture_service_stopped" in observed) == (failure == "watcher")


def test_waiting_close_finishes_active_task_before_closing_loaded_client(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    spool.mkdir()
    model = RemoteOcr(
        RemoteOcrConfig(
            service_url="https://ocr.invalid",
            token_file=str(tmp_path / "not-read"),
            scope="stub",
            storage="stub",
            revision="stub",
            spool=str(spool),
        )
    )
    runtime = RuntimeContext(ProductionConfig(), Path(tmp_path))
    runtime.update(
        stopping=Event(),
        ocr_pool=ThreadPoolExecutor(1),
        readout_pool=ThreadPoolExecutor(1),
        ocr_model=model,
    )
    events = []
    release = Event()
    started = Event()
    original = model.client.close

    def close_client():
        events.append("client_closed")
        original()

    monkeypatch.setattr(model.client, "close", close_client)

    def active_task():
        started.set()
        assert release.wait(timeout=2)
        assert not model.client.is_closed
        events.append("task_finished")

    task = runtime["readout_pool"].submit(active_task)
    assert started.wait(timeout=1)
    timer = Timer(0.05, release.set)
    timer.start()
    try:
        runtime.close(wait=True)
        task.result()
    finally:
        release.set()
        timer.join()
        runtime.close(wait=True)
    assert model.client.is_closed
    assert events == ["task_finished", "client_closed"]
    assert not (tmp_path / "not-read").exists()


def test_close_continues_after_component_error_and_does_not_repeat_disposal(tmp_path):
    runtime = RuntimeContext(ProductionConfig(), tmp_path)
    disposed = []

    class Broken:
        def close(self):
            disposed.append("broken")
            raise ValueError("stub close failure")

    class Remaining:
        def close(self):
            disposed.append("remaining")

    runtime.update(
        stopping=Event(),
        video_ocr=Broken(),
        automatic_runner=Remaining(),
        ocr_pool=ThreadPoolExecutor(1),
        readout_pool=ThreadPoolExecutor(1),
        ocr_model=None,
    )
    with pytest.raises(ValueError, match="stub close failure"):
        runtime.close(wait=True)
    assert_disposed(runtime)
    runtime.close(wait=True)
    assert disposed == ["broken", "remaining"]


@pytest.mark.parametrize("worker_type", [OCRWorker, ArchiveWorker])
def test_worker_startup_exception_closes_runtime_and_ipc(tmp_path, monkeypatch, worker_type):
    from field_recognition.runtime import runtime_rpc

    database = Database(tmp_path / "worker.sqlite3")
    apply_migrations(database)
    worker = worker_type(
        "stub", ProductionConfig(database_path=str(database.path), production_enabled=True)
    )
    servers = []
    original = runtime_rpc.RpcServer

    def server_factory(*args, **kwargs):
        server = original(*args, **kwargs)
        servers.append(server)
        return server

    def fail():
        raise ValueError("stub worker startup failure")

    monkeypatch.setattr(runtime_rpc, "RpcServer", server_factory)
    if worker_type is OCRWorker:
        worker.core["queue_ocr_warmup"] = fail
    else:
        monkeypatch.setattr(worker.archive_store.integrity, "start", fail)
    with pytest.raises(ValueError, match="stub worker startup failure"):
        worker.run(persistent=False)
    assert_disposed(worker.core)
    assert all(server.closed and not server.path.exists() for server in servers)
    assert worker.core["ocr_model"] is None
    worker.close()
