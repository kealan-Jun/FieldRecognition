from tests.support.archive_incremental import STAMP
from tests.support.archive_incremental import snapshots, job, scan, group  # noqa: F401

"""Event closures retain archive semantics without decoding unrelated history."""
import json
import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import field_recognition.archive.archive_events as archive_events
import field_recognition.archive.archive_migration as archive_migration
from field_recognition.archive.archive_paths import ArchivePaths


@pytest.mark.parametrize("echo_failure", [False, True])
def test_changed_event_and_echo_do_not_decode_or_validate_unrelated_large_history(
    snapshots, monkeypatch, echo_failure
):
    archive, add, root = snapshots
    add("scans", "photo", scan("photo"))
    old = add("jobs", "reading", job("reading", "photo", status="queued"))
    final = add("jobs", "reading", job("reading", "photo"), pending=True)
    # About 100 MiB, comparable to the production archive's document volume.
    # Unrelated receipts deliberately do not exist: publication must not inspect them.
    unrelated = job("unrelated", "other-photo", large_unrelated_marker="x" * (512 * 1024))
    for _ in range(200):
        add("jobs", "unrelated", unrelated, receipt=False)
    real_loads = json.loads

    def guarded_loads(value, *args, **kwargs):
        marker = b"large_unrelated_marker" if isinstance(value, bytes) else "large_unrelated_marker"
        assert marker not in value, "Unrelated archive JSON was decoded on the event path"
        return real_loads(value, *args, **kwargs)

    monkeypatch.setattr(archive_events.json, "loads", guarded_loads)
    real_retire = ArchivePaths.retire_views
    calls = []

    def retire(paths, views, **kwargs):
        calls.append(True)
        if echo_failure and len(calls) == 2:
            raise OSError("Interrupted timing echo")
        return real_retire(paths, views, **kwargs)

    monkeypatch.setattr(ArchivePaths, "retire_views", retire)
    if echo_failure:
        with pytest.raises(OSError):
            archive_migration.publish_events(archive)
        with archive.core["db"]() as conn:
            confirmation = real_loads(
                conn.execute(
                    "SELECT value FROM archive_meta WHERE key='event_confirmation_pending'"
                ).fetchone()[0]
            )
        assert final in confirmation
    archive_migration.publish_events(archive)
    evidence = real_loads(next(root.glob("*/VoicePhotoReadings/*/Evidence.json")).read_text())
    assert [v["sequence"] for v in evidence["receipt_versions"]] == [1, old, final]
    assert evidence["observations"][0]["status"] == "completed"
    assert evidence["observations"][0]["archive"]["readable_at"] == STAMP
    assert evidence["processing_terminal"]
    with archive.core["db"]() as conn:
        assert (
            real_loads(
                conn.execute(
                    "SELECT value FROM archive_meta WHERE key='event_confirmation_pending'"
                ).fetchone()[0]
            )
            == []
        )


@pytest.mark.parametrize(
    "changed",
    [
        ("jobs", "retry"),
        ("scans", "shared"),
        ("photo_measurements", "burst"),
        ("experiment_records", "confirmed-2"),
        ("bindings", "binding"),
    ],
)
def test_incremental_closure_matches_full_preparation_for_groups_retries_and_confirmations(
    snapshots, changed
):
    archive, add, root = snapshots
    sequences = {}
    documents = [
        ("scans", "shared", scan("shared")),
        ("jobs", "original", job("original", "shared")),
        (
            "jobs",
            "retry",
            job(
                "retry",
                "shared",
                recognition_root_job_id="original",
                supersedes_job_id="original",
                recognition_revision=2,
            ),
        ),
        (
            "bindings",
            "binding",
            {
                "binding_id": "binding",
                "camera_id": "Camera",
                "scan_id": "shared",
                "started_at": STAMP,
            },
        ),
        ("scans", "first", scan("first")),
        ("scans", "second", scan("second")),
        ("jobs", "superseded", job("superseded", "first", measurement_id="burst")),
        (
            "jobs",
            "member-1",
            job(
                "member-1",
                "first",
                measurement_id="burst",
                recognition_root_job_id="superseded",
                supersedes_job_id="superseded",
                recognition_revision=2,
            ),
        ),
        ("jobs", "member-2", job("member-2", "second", measurement_id="burst")),
        (
            "photo_measurements",
            "burst",
            group(
                "burst",
                ["member-1", "member-2"],
                status="confirmed",
                superseded_job_ids=["superseded"],
                record_revision=2,
            ),
        ),
        (
            "experiment_records",
            "confirmed-1",
            group("burst", ["superseded", "member-2"], status="confirmed", record_revision=1),
        ),
        (
            "experiment_records",
            "confirmed-2",
            group(
                "burst",
                ["member-1", "member-2"],
                status="confirmed",
                superseded_job_ids=["superseded"],
                record_revision=2,
            ),
        ),
        ("scans", "unrelated", scan("unrelated")),
        ("jobs", "other", job("other", "unrelated")),
    ]
    for entity, ident, document in documents:
        sequences[(entity, ident)] = add(entity, ident, document)
    # Current state can run ahead of archival; ID routing must not substitute its values.
    with archive.core["db"]() as conn:
        conn.execute(
            "UPDATE jobs SET document=? WHERE id=?",
            (json.dumps(job("retry", "shared", status="running")), "retry"),
        )
        all_rows = conn.execute("SELECT * FROM archive_outbox ORDER BY seq").fetchall()
        selected_rows = archive_events.event_rows(conn, {sequences[changed]}, ArchivePaths(root))
    whole, _ = archive_events.prepare(all_rows, ArchivePaths(root))
    incremental, _ = archive_events.prepare(selected_rows, ArchivePaths(root))
    select = lambda bundles: [
        bundle
        for bundle in bundles
        if any(v["sequence"] == sequences[changed] for v in bundle["history"])
    ]
    assert select(incremental) == select(whole)
    if changed == ("jobs", "retry"):
        assert (
            next(b for b in incremental if b["key"] == "capture:shared")["jobs"][-1]["doc"][
                "status"
            ]
            == "completed"
        )


def test_published_aliases_recover_event_members_without_current_business_rows(snapshots):
    archive, add, root = snapshots
    add("scans", "photo", scan("photo"))
    old = add("jobs", "old", job("old", "photo"))
    final = add("jobs", "new", job("new", "photo"), pending=True)
    paths = ArchivePaths(root)
    directory = paths.directory("capture:photo", "2026-10-08_Camera/VoicePhotoReadings/Published")
    paths.alias("Records/Jobs/old/Record.json", directory + "/Evidence.json")
    paths.alias("Records/Scans/photo/Record.json", directory + "/Evidence.json")
    paths.save()
    with archive.core["db"]() as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", ("old",))
        selected = archive_events.event_rows(conn, {final}, ArchivePaths(root))
    assert old in {row["seq"] for row in selected}
    assert {row["entity_id"] for row in selected} == {"photo", "old", "new"}


def test_latest_rows_decodes_only_the_final_version():
    rows = [
        {
            "seq": 1,
            "entity": "jobs",
            "entity_id": "same",
            "document": "broken old json",
            "receipt_path": "old",
            "recorded_at": STAMP,
        },
        {
            "seq": 2,
            "entity": "jobs",
            "entity_id": "same",
            "document": '{"status":"completed"}',
            "receipt_path": "new",
            "recorded_at": STAMP,
        },
    ]
    latest, versions = archive_events.latest_rows(rows)
    assert latest[("jobs", "same")]["doc"]["status"] == "completed"
    assert [v["sequence"] for v in versions[("jobs", "same")]] == [1, 2]


def test_latest_document_decode_loop_yields_between_entities(monkeypatch):
    import field_recognition.archive.archive_progress as archive_progress

    clock = [0.0]
    decoded = []
    real_loads = json.loads

    def decode(value):
        decoded.append(value)
        result = real_loads(value)
        clock[0] += 2
        return result

    def new_receipt():
        raise InterruptedError("A new receipt is due")

    monkeypatch.setattr(archive_progress.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(archive_events.json, "loads", decode)
    rows = [
        {
            "seq": n,
            "entity": "jobs",
            "entity_id": str(n),
            "document": "{}",
            "receipt_path": str(n),
            "recorded_at": STAMP,
        }
        for n in (1, 2)
    ]
    with archive_progress.checkpoint(new_receipt), pytest.raises(InterruptedError):
        archive_events.latest_rows(rows)
    assert len(decoded) == 1


def test_prepare_yields_between_source_events_without_a_filesystem_operation(monkeypatch):
    import field_recognition.archive.archive_progress as archive_progress

    clock = [0.0]
    directories = {}

    def directory(key, proposed):
        directories[key] = proposed
        clock[0] += 2
        return proposed

    def new_receipt():
        raise InterruptedError("A new receipt is due")

    monkeypatch.setattr(archive_progress.time, "monotonic", lambda: clock[0])
    paths = SimpleNamespace(directory=directory)
    rows = [
        {
            "seq": n,
            "entity": "scans",
            "entity_id": str(n),
            "document": json.dumps(scan(str(n))),
            "receipt_path": str(n),
            "recorded_at": STAMP,
        }
        for n in (1, 2)
    ]
    with archive_progress.checkpoint(new_receipt), pytest.raises(InterruptedError):
        archive_events.prepare(rows, paths)
    assert len(directories) == 1
