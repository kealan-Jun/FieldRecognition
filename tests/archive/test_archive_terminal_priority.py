"""Current event scheduling uses temporary SQLite and a deterministic writer."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import time
import hashlib
import uuid

import pytest

from field_recognition.archive.archive_store import ArchiveStore
from field_recognition.archive.archive_migration import publish_events as real_publish_events


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    database = tmp_path / "queue.sqlite3"

    @contextmanager
    def db():
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    now = lambda: datetime.now(timezone.utc).isoformat()
    store = ArchiveStore({"db": db, "DATA": tmp_path, "now": now})
    # Routing tables provide IDs only, as in the real scheduler. Insert snapshots
    # directly so each version in these tests has an intentional sequence.
    with db() as conn:
        for table in ("scans", "jobs", "photo_measurements", "experiment_records"):
            conn.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY,document TEXT NOT NULL)")
    root = tmp_path / "FieldRecognitionArchive"
    root.mkdir()
    monkeypatch.setenv("FIELD_ARCHIVE_ENABLED", "1")
    monkeypatch.setattr(store, "_root", lambda **kwargs: root)
    publications, events, navigation = [], [], []
    on_publish = [lambda row: None]

    def add(entity, ident, doc, *, archived=False, retry_after=0):
        raw = json.dumps(doc, sort_keys=True)
        with db() as conn:
            seq = conn.execute(
                "INSERT INTO archive_outbox(entity,entity_id,document,recorded_at,"
                "archived_at,published_at,receipt_path,retry_after) VALUES(?,?,?,?,?,?,?,?)",
                (
                    entity,
                    ident,
                    raw,
                    now(),
                    now() if archived else None,
                    now() if archived else None,
                    f"{ident}.json" if archived else None,
                    retry_after,
                ),
            ).lastrowid
            if entity in {"scans", "jobs", "photo_measurements", "experiment_records"}:
                conn.execute(f"INSERT OR REPLACE INTO {entity} VALUES(?,?)", (ident, raw))
        return seq

    def publish(row):
        publications.append(row["seq"])
        on_publish[0](row)
        return f"receipt-{row['seq']}.json"

    def publish_events(archive, rows=None, *, sequences=None):
        assert archive.integrity.lock.locked()
        seeds = set(sequences or ())
        events.append((len(publications), seeds))
        with db() as conn:
            for sequence in seeds:
                conn.execute(
                    "UPDATE archive_outbox SET readable_at=? WHERE seq=?", (now(), sequence)
                )

    monkeypatch.setattr(store, "publish", publish)
    monkeypatch.setattr(
        "field_recognition.archive.archive_migration.publish_events", publish_events
    )
    monkeypatch.setattr(store, "write_index", lambda: navigation.append(True))
    monkeypatch.setattr(store.integrity, "publish_pending", lambda: None)
    return store, db, add, publications, events, navigation, on_publish


def test_terminal_arriving_inside_batch_preempts_next_selection(scheduler):
    store, db, add, published, events, navigation, on_publish = scheduler
    history = [add("automation_settings", "camera", {"version": i}) for i in range(8)]
    terminal = []

    def arrive(row):
        if row["seq"] == history[0]:
            terminal.append(add("jobs", "new-job", {"job_id": "new-job", "status": "completed"}))

    on_publish[0] = arrive
    store.step(batch_size=3)
    assert published == [history[0], terminal[0], history[1]]
    assert any(count == 2 and terminal[0] in seeds for count, seeds in events)
    assert navigation == []
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM archive_outbox").fetchone()[0] == 9
        assert conn.execute(
            "SELECT readable_at FROM archive_outbox WHERE seq=?", (terminal[0],)
        ).fetchone()[0]


def test_three_priority_selections_then_oldest_history_across_batches(scheduler):
    store, db, add, published, _, _, on_publish = scheduler
    history = [add("automation_settings", f"old-{i}", {"version": i}) for i in range(4)]
    originals = {}
    with db() as conn:
        originals = {
            row["seq"]: row["document"]
            for row in conn.execute("SELECT seq,document FROM archive_outbox")
        }
    serial = [0]

    def add_terminal():
        serial[0] += 1
        ident = f"terminal-{serial[0]}"
        add("jobs", ident, {"job_id": ident, "status": "completed"})

    add_terminal()
    on_publish[0] = lambda row: add_terminal()
    for _ in range(16):
        store.step(batch_size=1)
    assert published[3::4] == history
    with db() as conn:
        for sequence, document in originals.items():
            row = conn.execute("SELECT * FROM archive_outbox WHERE seq=?", (sequence,)).fetchone()
            assert row["archived_at"] and row["document"] == document
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM archive_outbox WHERE archived_at IS NULL"
            ).fetchone()[0]
            > 0
        )


def test_archived_terminal_prioritizes_latest_scans_members_and_draft_group(scheduler):
    store, db, add, published, events, navigation, _ = scheduler
    history = [add("automation_settings", "old-camera", {"version": i}) for i in range(8)]
    scans = [
        add("scans", f"photo-{i}", {"capture_id": f"photo-{i}", "status": "no_qr"})
        for i in range(3)
    ]
    jobs = [
        add(
            "jobs",
            f"job-{i}",
            {
                "job_id": f"job-{i}",
                "capture_id": f"photo-{i}",
                "measurement_id": "burst",
                "status": "completed",
            },
            archived=i == 0,
        )
        for i in range(3)
    ]
    draft = add(
        "photo_measurements",
        "burst",
        {
            "measurement_id": "burst",
            "status": "draft",
            "job_ids": [f"job-{i}" for i in range(3)],
            "sources": [{"capture_id": f"photo-{i}"} for i in range(3)],
        },
    )
    store.step(batch_size=7)
    assert published == [*scans, history[0], jobs[1], jobs[2], draft]
    assert all(jobs[0] not in seeds for count, seeds in events if count < 7)
    assert any(count == 7 and jobs[0] in seeds for count, seeds in events)
    assert navigation == []
    with db() as conn:
        assert conn.execute(
            "SELECT readable_at FROM archive_outbox WHERE seq=?", (jobs[0],)
        ).fetchone()[0]
        assert (
            conn.execute(
                "SELECT archived_at FROM archive_outbox WHERE seq=?", (history[1],)
            ).fetchone()[0]
            is None
        )


def test_failed_dependency_backoff_does_not_block_fifo_or_acknowledge_event(scheduler):
    store, db, add, published, events, navigation, on_publish = scheduler
    old = add("automation_settings", "old", {"version": 1})
    source = add("scans", "photo", {"capture_id": "photo"})
    terminal = add("jobs", "job", {"job_id": "job", "capture_id": "photo", "status": "completed"})

    def fail_source(row):
        if row["seq"] == source:
            raise OSError("source copy unavailable")

    on_publish[0] = fail_source
    store.step(batch_size=3)
    assert published == [source, old, terminal]
    assert all(terminal not in seeds for _, seeds in events)
    assert navigation == []
    with db() as conn:
        row = conn.execute("SELECT * FROM archive_outbox WHERE seq=?", (source,)).fetchone()
        assert row["attempts"] == 1 and row["retry_after"] > time.time()
        assert row["archived_at"] is None
        assert (
            conn.execute(
                "SELECT readable_at FROM archive_outbox WHERE seq=?", (terminal,)
            ).fetchone()[0]
            is None
        )


def test_legacy_retry_columns_are_extended_before_pending_index(scheduler):
    store, db, add, _, _, _, _ = scheduler
    sequence = add("automation_settings", "legacy", {"retained": "original"})
    with db() as conn:
        original = conn.execute(
            "SELECT document FROM archive_outbox WHERE seq=?", (sequence,)
        ).fetchone()[0]
        conn.execute("DROP INDEX archive_pending_due")
        for name in ("retry_after", "attempts", "last_error"):
            conn.execute(f"ALTER TABLE archive_outbox DROP COLUMN {name}")
    restored = ArchiveStore(store.core)
    with db() as conn:
        row = conn.execute("SELECT * FROM archive_outbox WHERE seq=?", (sequence,)).fetchone()
        assert row["document"] == original
        assert row["retry_after"] == 0 and row["attempts"] == 0 and row["last_error"] is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='archive_pending_due'"
        ).fetchone()
    assert restored.instance == store.instance


def test_corrupt_old_event_retries_independently_and_new_evidence_is_physically_readable(
    scheduler, monkeypatch
):
    """Use actual immutable receipts, assets, manifests and event publication."""
    from PIL import Image

    store, db, add, _, _, navigation, _ = scheduler
    root = store._root()
    monkeypatch.setattr(store, "publish", ArchiveStore.publish.__get__(store))
    monkeypatch.setattr(
        "field_recognition.archive.archive_migration.publish_events", real_publish_events
    )
    images = store.core["DATA"] / "Images"
    images.mkdir()
    timestamp = store.core["now"]()

    def reading(*, archived):
        capture, job = str(uuid.uuid4()), str(uuid.uuid4())
        image = images / (capture + ".png")
        Image.new("RGB", (2, 2), color="white").save(image)
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        scan = {
            "capture_id": capture,
            "scan_id": capture,
            "camera_id": "Camera",
            "received_at": timestamp,
            "matches": [],
            "image_sha256": digest,
            "external_photo": {"captured_at": timestamp},
        }
        source = add("scans", capture, scan, archived=archived)
        task = add(
            "jobs",
            job,
            {
                "job_id": job,
                "capture_id": capture,
                "camera_id": "Camera",
                "submitted_at": timestamp,
                "finished_at": timestamp,
                "status": "completed",
                "lines": [],
                "readings": [],
                "request_trigger": "voice_photo_directory",
            },
            archived=archived,
        )
        return source, task, job

    source, broken, _ = reading(archived=True)
    originals = {}
    for sequence in (source, broken):
        with db() as conn:
            row = conn.execute("SELECT * FROM archive_outbox WHERE seq=?", (sequence,)).fetchone()
        relative = store.publish(row)
        originals[sequence] = (relative, (root / relative).read_bytes())
        with db() as conn:
            conn.execute(
                "UPDATE archive_outbox SET receipt_path=?,readable_at=? WHERE seq=?",
                (relative, timestamp if sequence == source else None, sequence),
            )
    broken_path = root / originals[broken][0]
    broken_path.write_bytes(b"corrupt historical receipt")
    new_source, new_task, new_job = reading(archived=False)

    store.step(batch_size=2)
    evidence = next(
        json.loads(path.read_text())
        for path in root.glob("*/VoicePhotoReadings/*/Evidence.json")
        if any(item["job_id"] == new_job for item in json.loads(path.read_text())["observations"])
    )
    assert evidence["processing_terminal"]
    assert evidence["observations"][0]["archive"]["readable_at"]
    assert evidence["sources"][0]["image"]["sha256"]
    with db() as conn:
        for sequence in (new_source, new_task):
            row = conn.execute("SELECT * FROM archive_outbox WHERE seq=?", (sequence,)).fetchone()
            assert row["archived_at"] and row["readable_at"]
        old = conn.execute("SELECT * FROM archive_outbox WHERE seq=?", (broken,)).fetchone()
        assert old["archived_at"] and old["readable_at"] is None
        assert old["receipt_path"] == originals[broken][0]
        retry_key, value = conn.execute(
            "SELECT key,value FROM archive_meta WHERE key GLOB 'view_retry:*'"
        ).fetchone()
        failure = json.loads(value)
        assert failure["error"] == "ValueError" and failure["attempts"] == 1
        assert failure["retry_after"] > time.time()
    assert broken_path.read_bytes() == b"corrupt historical receipt"
    assert store.status["status"] == "retrying" and store.status["pending_event_view_retries"] == 1
    assert navigation == []
    # An immediate repeat honors durable backoff and does not retry the bad
    # group. It cannot remove or alter the new event's evidence.
    store.step(batch_size=0)
    with db() as conn:
        assert (
            json.loads(
                conn.execute("SELECT value FROM archive_meta WHERE key=?", (retry_key,)).fetchone()[
                    0
                ]
            )["attempts"]
            == 1
        )
    broken_path.write_bytes(originals[broken][1])
    with db() as conn:
        failure["retry_after"] = 0
        conn.execute(
            "UPDATE archive_meta SET value=? WHERE key=?", (json.dumps(failure), retry_key)
        )
    store.step(batch_size=0)
    with db() as conn:
        assert conn.execute(
            "SELECT readable_at FROM archive_outbox WHERE seq=?", (broken,)
        ).fetchone()[0]
        assert not conn.execute(
            "SELECT 1 FROM archive_meta WHERE key GLOB 'view_retry:*'"
        ).fetchone()
    assert store.status["status"] == "ready" and store.status["pending_event_view_retries"] == 0
