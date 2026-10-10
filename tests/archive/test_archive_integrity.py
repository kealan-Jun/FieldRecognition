import json
import pytest
from datetime import datetime, timedelta, timezone

from field_recognition.archive.archive_integrity import ArchiveIntegrity
from tests.support.archive_store import archive  # noqa: F401
from tests.support.demo import app_client, scan  # noqa: F401
from tests.support.configuration import refresh_runtime_config


def test_daily_check_verifies_deduplicated_objects_and_persists_schedule(archive):
    app, client, root = archive
    capture = scan(client)
    with app.db() as conn:
        conn.execute(
            "UPDATE scans SET document=? WHERE id=?",
            (json.dumps(capture | {"operator": "实验员"}), capture["scan_id"]),
        )
    app.archive_store.step()
    checker = app.archive_store.integrity
    assert checker.due()
    report = checker.run_once()
    assert report["status"] == "completed" and report["issue_count"] == 0
    assert report["verified_receipts"] == report["expected_receipts"] == 2
    assert report["verified_objects"] == report["expected_objects"] == 2
    assert not report["repair_performed"]
    assert not checker.due() and not ArchiveIntegrity(app.archive_store).due()
    app.archive_store.step()
    status = client.get("/api/state").json()["archive"]["integrity"]
    assert status["report_path"] and not status["report_pending"]
    assert json.loads((root / status["report_path"]).read_text()) == report
    assert (
        json.loads((root / ".System/Integrity/Latest.json").read_text())["run_id"]
        == report["run_id"]
    )
    assert (
        json.loads((root / ".System/Index.json").read_text())["integrity"]["last_report"]["run_id"]
        == report["run_id"]
    )
    # Tomorrow is due even after constructing a new service instance.
    with app.db() as conn:
        old = report | {"finished_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}
        conn.execute("UPDATE archive_integrity_runs SET document=?", (json.dumps(old),))
    assert ArchiveIntegrity(app.archive_store).due()


def test_missing_and_corrupt_files_are_reported_and_never_repaired(archive):
    app, client, root = archive
    scan(client)
    scan(client)
    app.archive_store.step()
    records = sorted(root.glob(".System/Receipts/**/*.json"))
    artifact = next(root.glob("**/Photos/*.png"))
    artifact.write_bytes(b"corrupt-object")
    missing = records[-1]
    missing.unlink()
    report = app.archive_store.integrity.run_once()
    assert report["status"] == "findings" and report["issue_count"] == 2
    assert {i["kind"] for i in report["issues"]} == {"receipt", "object"}
    assert any(i["code"] == "missing" for i in report["issues"])
    assert artifact.read_bytes() == b"corrupt-object" and not missing.exists()
    assert (
        app.archive_store.snapshot()["pending_receipts"] == 0
    )  # Acknowledgement history stays intact.
    with pytest.raises(ValueError):  # Never rebuild a business view from corrupt evidence.
        app.archive_store.step()
    assert app.archive_store.snapshot()["integrity"]["last_report"]["issue_count"] == 2


def test_altered_receipt_cannot_redefine_the_expected_object_hash(archive):
    app, client, root = archive
    scan(client)
    app.archive_store.step()
    receipt = next(root.glob(".System/Receipts/**/*.json"))
    doc = json.loads(receipt.read_text())
    doc["document"]["operator"] = "changed"
    receipt.write_text(json.dumps(doc))
    report = app.archive_store.integrity.run_once()
    assert report["status"] == "findings" and report["verified_receipts"] == 0
    assert report["issues"][0]["kind"] == "receipt"


def test_lost_mount_keeps_local_report_and_retries_publication(archive, monkeypatch):
    app, client, root = archive
    scan(client)
    app.archive_store.step()
    monkeypatch.setenv("FIELD_ARCHIVE_MOUNT", "/no-mount")
    refresh_runtime_config(app, "archive_mount")
    checker = app.archive_store.integrity
    report = checker.run_once()
    assert report["status"] == "unavailable" and report["verified_receipts"] == 0
    assert checker.snapshot()["report_pending"]
    assert not checker.due()
    monkeypatch.delenv("FIELD_ARCHIVE_MOUNT")
    refresh_runtime_config(app, "archive_mount")
    app.archive_store.step()
    assert not checker.snapshot()["report_pending"]
    assert (root / checker.snapshot()["report_path"]).is_file()


def test_shutdown_is_incomplete_and_simultaneous_check_is_skipped(archive):
    app, client, root = archive
    scan(client)
    app.archive_store.step()
    checker = app.archive_store.integrity
    with checker.run_lock:
        assert checker.run_once() is None
    checker.stop.set()
    report = checker.run_once()
    assert report["status"] == "interrupted" and report["verified_receipts"] == 0


def test_historical_hashing_does_not_block_new_event_publication(archive, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    app, client, root = archive
    scan(client)
    app.archive_store.step(100)
    checker = app.archive_store.integrity
    paused, resume = threading.Event(), threading.Event()
    real_wait = checker.stop.wait

    def pause_once(seconds):
        if not paused.is_set():
            paused.set()
            assert resume.wait(5)
        return real_wait(seconds)

    monkeypatch.setattr(checker.stop, "wait", pause_once)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(checker.run_once)
        try:
            assert paused.wait(3)
            assert checker.lock.acquire(blocking=False)
            checker.lock.release()
            assert checker.run_once() is None
            scan(client, camera="DuringIntegrity")
            app.archive_store.step(100)
            assert list(root.glob("*_DuringIntegrity/Bindings/*/Binding.json"))
            assert checker.running
        finally:
            resume.set()
        report = future.result(timeout=5)
    assert report["status"] == "completed" and report["issue_count"] == 0
    assert report["expected_receipts"] == 1  # A frozen acknowledgement snapshot.


@pytest.mark.parametrize("change", ["rename", "rewrite"])
def test_open_integrity_descriptor_survives_moves_and_detects_in_place_changes(
    archive, monkeypatch, change
):
    from concurrent.futures import ThreadPoolExecutor
    import hashlib
    import os
    import threading
    from field_recognition.archive.archive_integrity import IntegrityError

    app, _, root = archive
    checker = app.archive_store.integrity
    path = root / "test-object.bin"
    root.mkdir(parents=True, exist_ok=True)
    raw = b"a" * (1024 * 1024 + 17)
    path.write_bytes(raw)
    paused, resume = threading.Event(), threading.Event()

    def pause_once(_):
        if not paused.is_set():
            paused.set()
            assert resume.wait(3)
        return False

    monkeypatch.setattr(checker.stop, "wait", pause_once)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(checker.digest, lambda: path)
        try:
            assert paused.wait(3)
            with checker.lock:
                if change == "rename":
                    path.rename(root / "moved-object.bin")
                else:
                    stamp = path.stat().st_mtime_ns
                    path.write_bytes(b"b" * len(raw))
                    os.utime(path, ns=(stamp + 1_000_000, stamp + 1_000_000))
        finally:
            resume.set()
        if change == "rewrite":
            with pytest.raises(IntegrityError, match="changed_during_check"):
                future.result(timeout=3)
        else:
            digest, size, _ = future.result(timeout=3)
            assert digest == hashlib.sha256(raw).hexdigest() and size == len(raw)
