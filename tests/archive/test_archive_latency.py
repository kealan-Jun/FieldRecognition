import json
import time
from types import SimpleNamespace

import pytest

from field_recognition.archive.archive_store import publication_timing, receipt_status
from field_recognition.runtime.archive_worker import ArchiveWorker
from tests.support.archive_store import archive  # noqa: F401
from tests.support.demo import app_client, scan  # noqa: F401


def test_archive_poll_drains_due_batches_but_respects_retry_backoff(archive):
    app, client, _ = archive
    store = app.archive_store
    assert store.next_poll_seconds() == 0.5
    scan(client)
    assert store.next_poll_seconds() == 0
    with app.db() as conn:
        conn.execute("UPDATE archive_outbox SET retry_after=?", (time.time() + 60,))
    assert store.next_poll_seconds() == 0.5
    with app.db() as conn:
        conn.execute("UPDATE archive_outbox SET retry_after=0")
    store.step()
    assert store.next_poll_seconds() == 0.5


def test_historical_navigation_does_not_start_while_another_receipt_is_due(archive, monkeypatch):
    import field_recognition.archive.archive_migration as archive_migration

    app, client, root = archive
    scan(client)
    scan(client, camera="SecondCamera")
    calls = []
    rebuild = archive_migration.rebuild
    monkeypatch.setattr(
        archive_migration, "rebuild", lambda *args: calls.append(True) or rebuild(*args)
    )
    app.archive_store.step(batch_size=1)
    assert calls == []
    assert app.archive_store.next_poll_seconds() == 0
    assert list(root.glob("*_TestCamera/Bindings/*/Binding.json"))
    app.archive_store.step(batch_size=1)
    assert calls and not app.archive_store.snapshot()["navigation_pending"]


def test_dedicated_worker_uses_backlog_idle_and_error_delays(tmp_path, monkeypatch):
    import field_recognition.runtime.archive_worker as archive_worker

    waits = []

    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped or len(waits) == 3

        def wait(self, seconds):
            waits.append(seconds)

        def set(self):
            self.stopped = True

    class Store:
        integrity = SimpleNamespace(start=lambda: None)
        calls = 0
        closed = False

        def snapshot(self):
            return {"pending_receipts": 1, "status": "ready"}

        def step(self):
            self.calls += 1
            if self.calls == 3:
                raise OSError("target unavailable")

        def next_poll_seconds(self):
            return 0 if self.calls == 1 else 0.5

        def close(self):
            self.closed = True

    store = Store()
    worker = ArchiveWorker(
        "test", None, {"database": None, "DATA": tmp_path, "archive_store": store}
    )
    worker.stop = Stop()
    monkeypatch.setattr(archive_worker, "heartbeat", lambda *args: None)
    worker.run()
    assert waits == [0, 0.5, 3]
    assert worker.stop.stopped
    assert store.closed


def test_archive_receipt_stages_are_per_version_and_legacy_stages_stay_null(archive):
    app, client, root = archive
    capture = scan(client)
    app.archive_store.step(100)
    with app.db() as conn:
        row = conn.execute(
            "SELECT * FROM archive_outbox WHERE entity_id=? ORDER BY seq DESC LIMIT 1",
            (capture["scan_id"],),
        ).fetchone()
        status = receipt_status(conn, "scans", capture["scan_id"])
    assert status["publication_started_at"]
    assert status["archive_queue_wait_ms"] >= 0
    assert status["receipt_publication_ms"] >= 0
    assert status["duration_clock_basis"]["receipt_publication_ms"] == "process_monotonic"
    index = json.loads((root / ".System/Index.json").read_text())
    item = next(i for i in index["items"] if i["sequence"] == row["seq"])
    assert item["receipt_publication_ms"] == status["receipt_publication_ms"]
    # Historical queued/published timestamps cannot reveal when the writer began.
    legacy = publication_timing({"recorded_at": row["recorded_at"]})
    assert legacy["publication_started_at"] is None
    assert legacy["archive_queue_wait_ms"] is None
    assert legacy["receipt_publication_ms"] is None


def test_concurrent_archive_initialization_preserves_legacy_versions(archive):
    from concurrent.futures import ThreadPoolExecutor
    from field_recognition.archive.archive_store import ArchiveStore

    app, client, _ = archive
    capture = scan(client)
    with app.db() as conn:
        before = conn.execute("SELECT document FROM archive_outbox ORDER BY seq").fetchall()
        conn.execute("ALTER TABLE archive_outbox DROP COLUMN publication_started_at")
        conn.execute("ALTER TABLE archive_outbox DROP COLUMN receipt_publication_ms")
    with ThreadPoolExecutor(max_workers=4) as pool:
        stores = list(pool.map(lambda _: ArchiveStore(vars(app)), range(4)))
    assert all(store.instance == app.archive_store.instance for store in stores)
    with app.db() as conn:
        assert [r[0] for r in conn.execute("SELECT document FROM archive_outbox ORDER BY seq")] == [
            r[0] for r in before
        ]
        status = receipt_status(conn, "scans", capture["scan_id"])
    assert status["publication_started_at"] is None and status["receipt_publication_ms"] is None


def test_event_timing_confirmation_survives_failed_historical_navigation(archive, monkeypatch):
    from concurrent.futures import Future
    import field_recognition.archive.archive_migration as archive_migration
    from tests.support.panel_readout import Clock, complete, execute, local_result

    app, client, root = archive
    capture = scan(client)
    monkeypatch.setattr(app.readout_pool, "submit", lambda *args: Future())
    job = client.post("/api/ocr", json={"capture_id": capture["capture_id"]}).json()
    execute(app, job, complete(local_result("12.34 g")), Clock())

    def fail(*args):
        raise OSError("historical navigation unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(archive_migration, "rebuild", fail)
        with pytest.raises(OSError):
            app.archive_store.step(100)
    evidence = next(
        json.loads(p.read_text())
        for p in root.glob("*/PhotoReadings/*/Evidence.json")
        if any(o["job_id"] == job["job_id"] for o in json.loads(p.read_text())["observations"])
    )
    observation = next(o for o in evidence["observations"] if o["job_id"] == job["job_id"])
    stored = app.get_job(job["job_id"])
    assert observation["archive"]["readable_at"] == stored["archive"]["readable_at"]
    assert observation["archive"]["readable_at"] is not None
    assert observation["timing"]["archive_readable_at"] == stored["archive"]["readable_at"]
    assert (
        observation["archive"]["receipt_publication_ms"]
        == stored["archive"]["receipt_publication_ms"]
    )
    assert app.archive_store.snapshot()["navigation_pending"]


def test_failed_timing_echo_retries_without_requiring_historical_navigation(archive, monkeypatch):
    import field_recognition.archive.archive_migration as archive_migration
    from field_recognition.archive.archive_paths import ArchivePaths

    app, client, root = archive
    capture = scan(client)
    real = ArchivePaths.retire_views
    calls = []

    def fail_echo(paths, views, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise OSError("transient echo failure")
        return real(paths, views, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ArchivePaths, "retire_views", fail_echo)
        app.archive_store.step(100)
        state = app.archive_store.snapshot()
        assert state["status"] == "retrying"
        assert state["pending_event_view_retries"] == 1
    with app.db() as conn:
        status = receipt_status(conn, "scans", capture["scan_id"])
        pending = json.loads(
            conn.execute(
                "SELECT value FROM archive_meta WHERE key='event_confirmation_pending'"
            ).fetchone()[0]
        )
        for retry in conn.execute(
            "SELECT key,value FROM archive_meta WHERE key GLOB 'view_retry:*'"
        ).fetchall():
            doc = json.loads(retry["value"])
            doc["retry_after"] = 0  # Deterministically advance the isolated retry.
            conn.execute(
                "UPDATE archive_meta SET value=? WHERE key=?", (json.dumps(doc), retry["key"])
            )
    assert status["readable_at"] and status["sequence"] in pending

    def fail_rebuild(*args):
        raise OSError("historical navigation still unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(archive_migration, "rebuild", fail_rebuild)
        with pytest.raises(OSError):
            app.archive_store.step(100)
    event = json.loads(next(root.glob("*/Bindings/*/Binding.json")).read_text())
    assert event["archive"]["readable_at"] == status["readable_at"]
    with app.db() as conn:
        assert (
            json.loads(
                conn.execute(
                    "SELECT value FROM archive_meta WHERE key='event_confirmation_pending'"
                ).fetchone()[0]
            )
            == []
        )


@pytest.mark.parametrize("interrupt_business", [False, True])
def test_configuration_receipt_does_not_block_navigation_or_acknowledge_unpublished_events(
    archive, monkeypatch, interrupt_business
):
    app, client, root = archive
    capture = scan(client)
    with app.db() as conn:
        auxiliary = conn.execute(
            "INSERT INTO archive_outbox(entity,entity_id,document,recorded_at) VALUES(?,?,?,?)",
            (
                "automation_settings",
                "TestCamera",
                json.dumps({"camera_id": "TestCamera", "status": "paused"}),
                app.now(),
            ),
        ).lastrowid
    if interrupt_business:
        monkeypatch.setattr(app.archive_store, "_publish_ready_events", lambda: None)
    app.archive_store.step(100)
    with app.db() as conn:
        row = conn.execute("SELECT * FROM archive_outbox WHERE seq=?", (auxiliary,)).fetchone()
        business = receipt_status(conn, "scans", capture["scan_id"])
    assert row["archived_at"] and row["published_at"]
    if interrupt_business:
        assert row["readable_at"] is None and business["readable_at"] is None
        assert not (root / ".System/Index.json").exists()
    else:
        assert row["readable_at"] and business["readable_at"]
        assert row["readability_basis"] == "archive_writer_derived_views_published"
        index = json.loads((root / ".System/Index.json").read_text())
        indexed = next(item for item in index["items"] if item["sequence"] == auxiliary)
        assert indexed["archive_readable_at"] == row["readable_at"]
        assert not app.archive_store.snapshot()["navigation_pending"]
