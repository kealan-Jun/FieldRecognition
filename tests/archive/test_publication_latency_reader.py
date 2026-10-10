"""Focused NAS polling must reduce traversal without caching snapshot evidence."""

import hashlib
import json
import os
from pathlib import Path

from scripts.measure_publication_latency import initialize, observe, parse_visible


def event(tmp_path, name="Event"):
    root = tmp_path / "replay"
    if not root.exists():
        initialize(root)
    directory = root / "FieldRecognitionArchive" / "Day" / name
    directory.mkdir(parents=True)
    group = {
        "group_id": name,
        "camera_id": "TestCamera",
        "expected_tasks": 1,
        "source_capture_ids": ["source-" + name],
        "t0_monotonic": 0,
        "evidence_first_parse_ms": None,
        "evidence_terminal_parse_ms": None,
        "result_first_parse_ms": None,
        "result_all_parse_ms": None,
        "terminal_snapshot_readable_ms": None,
        "manifest_verified_ms": None,
        "outcome": "pending",
        "metadata_status": "not_terminal",
        "task_outcomes": [],
        "task_timings": [],
        "result_files": [],
    }
    evidence = {
        "group_id": name,
        "camera_id": "TestCamera",
        "processing_terminal": True,
        "processing_outcome": "succeeded",
        "publication": {"generation": "generation-a"},
        "sources": [
            {"capture_id": "local-" + name, "external_photo": {"capture_id": "source-" + name}}
        ],
        "task_outcomes": [
            {
                "task_id": "job-" + name,
                "capture_id": "local-" + name,
                "terminal": True,
                "outcome": "succeeded",
            }
        ],
        "measurement_files": [],
    }
    return root, directory, group, evidence


def result(directory, value):
    raw = json.dumps({"values": [{"value": value}]}).encode()
    (directory / "Result.json").write_bytes(raw)
    return {
        "result": directory.relative_to(directory.parents[1]).as_posix() + "/Result.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def write(directory, evidence):
    (directory / "Evidence.json").write_text(json.dumps(evidence))


def test_system_tree_is_pruned_before_directory_scan(tmp_path, monkeypatch):
    root, directory, _, evidence = event(tmp_path)
    write(directory, evidence)
    system = root / "FieldRecognitionArchive" / ".System" / "ManyReceipts"
    system.mkdir(parents=True)
    (system / "Evidence.json").write_text(json.dumps(evidence))
    original = os.scandir

    def scan(path):
        assert ".System" not in Path(path).parts
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    visible = parse_visible(root, {}, clock=lambda: 1)
    assert [entry[0] for entry in visible[0]] == [directory / "Evidence.json"]


def test_early_local_capture_identity_does_not_hide_later_source_receipt(tmp_path):
    root, directory, group, evidence = event(tmp_path)
    evidence["group_id"] = "local-" + group["group_id"]
    sources = evidence.pop("sources")
    write(directory, evidence)
    reads, reader = {}, {}
    observe(
        group,
        parse_visible(root, reads, clock=lambda: 1, groups=[group], reader_state=reader),
        clock=lambda: 1,
    )
    assert group["outcome"] == "pending"
    assert str(directory) not in reader["event_identities"]
    evidence["sources"] = sources
    evidence["measurement_files"] = [result(directory, 63)]
    write(directory, evidence)
    observe(
        group,
        parse_visible(root, reads, clock=lambda: 2, groups=[group], reader_state=reader),
        clock=lambda: 2,
    )
    assert group["outcome"] == "succeeded"
    assert group["terminal_snapshot_readable_ms"] == 2000


def test_completed_event_is_neither_traversed_nor_read_again(tmp_path, monkeypatch):
    root, old, old_group, old_evidence = event(tmp_path, "Completed")
    old_evidence["measurement_files"] = [result(old, 1)]
    write(old, old_evidence)
    reader = {}
    parse_visible(root, {}, clock=lambda: 1, groups=[old_group], reader_state=reader)
    _, active, active_group, active_evidence = event(tmp_path, "Active")
    active_evidence["measurement_files"] = [result(active, 2)]
    write(active, active_evidence)
    original_scan, original_read = os.scandir, Path.read_bytes

    def scan(path):
        assert old not in [Path(path), *Path(path).parents]
        return original_scan(path)

    def read(path):
        assert old not in path.parents
        return original_read(path)

    monkeypatch.setattr(os, "scandir", scan)
    monkeypatch.setattr(Path, "read_bytes", read)
    visible = parse_visible(root, {}, clock=lambda: 2, groups=[active_group], reader_state=reader)
    assert [entry[0] for entry in visible[0]] == [active / "Evidence.json"]
    assert set(visible[1]) == {"Day/Active/Result.json"}


def test_focused_reader_reads_generation_manifest_and_readability_echo_from_bytes(tmp_path):
    root, directory, group, evidence = event(tmp_path)
    manifest_a = result(directory, 1)
    evidence["measurement_files"] = [manifest_a]
    write(directory, evidence)
    reads, reader = {}, {}
    visible = parse_visible(root, reads, clock=lambda: 1, groups=[group], reader_state=reader)
    observe(group, visible, clock=lambda: 2)
    assert group["metadata_status"] == "waiting_archive_timing"
    original_snapshot = group["terminal_snapshot_readable_ms"]
    original_evidence = group["terminal_evidence_sha256"]
    previous_mtime = (directory / "Evidence.json").stat().st_mtime_ns
    evidence["publication"]["generation"] = "generation-b"
    evidence["measurement_files"] = [result(directory, 2)]
    write(directory, evidence)
    # Metadata caches would miss an overwrite with an unchanged timestamp.
    os.utime(directory / "Evidence.json", ns=(previous_mtime, previous_mtime))
    visible = parse_visible(root, reads, clock=lambda: 3, groups=[group], reader_state=reader)
    assert visible[0][0][1]["publication"]["generation"] == "generation-b"
    assert visible[1]["Day/Event/Result.json"][2] == evidence["measurement_files"][0]["sha256"]
    observe(group, visible, clock=lambda: 3.1)
    assert group["metadata_status"] == "waiting_archive_timing"
    evidence["publication"]["generation"] = "generation-a"
    evidence["measurement_files"] = [result(directory, 1)]
    evidence["archive"] = {"readable_at": "2026-10-08T00:00:00+00:00"}
    evidence["observations"] = [
        {
            "job_id": "job-Event",
            "capture_id": "local-Event",
            "timing": {"archive_readable_at": "2026-10-08T00:00:00+00:00"},
        }
    ]
    write(directory, evidence)
    visible = parse_visible(root, reads, clock=lambda: 4, groups=[group], reader_state=reader)
    observe(group, visible, clock=lambda: 5)
    assert group["metadata_status"] == "complete"
    assert group["terminal_evidence_sha256"] == original_evidence
    assert group["metadata_evidence_sha256"] != original_evidence
    assert group["terminal_snapshot_readable_ms"] == original_snapshot


def test_result_deletion_and_returning_digest_need_a_current_read(tmp_path):
    root, directory, group, evidence = event(tmp_path)
    manifest = result(directory, 1)
    reads, reader = {}, {}
    parse_visible(root, reads, clock=lambda: 1, groups=[group], reader_state=reader)
    evidence["measurement_files"] = [manifest]
    write(directory, evidence)
    (directory / "Result.json").unlink()
    visible = parse_visible(root, reads, clock=lambda: 2, groups=[group], reader_state=reader)
    assert visible[1] == {}
    observe(group, visible, clock=lambda: 2.1)
    assert group["outcome"] == "pending"
    assert result(directory, 1) == manifest
    visible = parse_visible(root, reads, clock=lambda: 3, groups=[group], reader_state=reader)
    assert visible[1]["Day/Event/Result.json"][3] == 1
    observe(group, visible, clock=lambda: 4)
    assert group["result_first_parse_ms"] == 1000
    assert group["terminal_snapshot_readable_ms"] == 4000


def test_no_active_groups_does_not_scan_files(tmp_path, monkeypatch):
    root = initialize(tmp_path / "replay")
    monkeypatch.setattr(
        os, "walk", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError())
    )
    assert parse_visible(root, {}, groups=[], reader_state={}) == ([], {})
