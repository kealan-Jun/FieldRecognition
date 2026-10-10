"""Replay reports use temporary files, injected clocks and no live services."""

import copy
import hashlib
import json

import pytest

from scripts import measure_publication_latency as replay
from scripts.measure_publication_latency import (
    ARCHIVE_DIRECTORY,
    archive_root,
    expire,
    expire_metadata,
    initialize,
    inject_group,
    observe,
    parse_visible,
    summarize,
    test_root as validate_test_root,
)


def setup_group(tmp_path):
    root = initialize(tmp_path / "replay")
    photo = tmp_path / "input.png"
    photo.write_bytes(b"isolated replay bytes")
    ticks = iter([1, 2, 3, 4])
    group = inject_group(root, "TestCamera", [photo] * 3, clock=lambda: next(ticks))
    directory = root / ARCHIVE_DIRECTORY / "Day" / "Event"
    directory.mkdir(parents=True)
    return root, group, directory


def terminal_evidence(group, *, outcome="succeeded"):
    # NAS receipts identify source capture IDs separately from local scan IDs.
    return {
        "group_id": group["group_id"],
        "camera_id": group["camera_id"],
        "processing_terminal": True,
        "processing_outcome": outcome,
        "sources": [
            {"capture_id": f"local-{i}", "external_photo": {"capture_id": source}}
            for i, source in enumerate(group["source_capture_ids"])
        ],
        "task_outcomes": [
            {
                "task_id": f"job-{i}",
                "capture_id": f"local-{i}",
                "terminal": True,
                "outcome": outcome,
            }
            for i in range(3)
        ],
        "measurement_files": [],
    }


def write_evidence(directory, value):
    (directory / "Evidence.json").write_text(json.dumps(value))


def result_manifest(directory, filename, value):
    raw = json.dumps({"values": [{"value": value}]}).encode()
    (directory / filename).write_bytes(raw)
    return {"result": f"Day/Event/{filename}", "sha256": hashlib.sha256(raw).hexdigest()}


def test_initialized_archive_has_runtime_basename_and_rejects_escaping_symlinks(tmp_path):
    root = initialize(tmp_path / "replay")
    assert archive_root(root).name == "FieldRecognitionArchive"
    assert validate_test_root(root) == root
    with pytest.raises(ValueError, match="new or empty"):
        initialize(root)
    (root / ARCHIVE_DIRECTORY).rmdir()
    (root / "archive").mkdir()
    assert archive_root(validate_test_root(root)).name == "archive"  # Old report compatibility.
    (root / "archive").rmdir()
    (root / "archive").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        validate_test_root(root)


def test_all_three_tasks_and_all_result_variants_are_required(tmp_path):
    root, group, directory = setup_group(tmp_path)
    reads = {}
    evidence = terminal_evidence(group)
    first_result = result_manifest(directory, "Result.json", 63)
    parse_visible(root, reads, clock=lambda: 6)
    second_raw = json.dumps({"values": [{"value": 42}]}).encode()
    second_result = {
        "result": "Day/Event/Result02.json",
        "sha256": hashlib.sha256(second_raw).hexdigest(),
    }
    evidence["measurement_files"] = [first_result, second_result]
    partial = copy.deepcopy(evidence)
    partial["task_outcomes"] = partial["task_outcomes"][:1]
    write_evidence(directory, partial)
    observe(group, parse_visible(root, reads, clock=lambda: 7), clock=lambda: 7.1)
    assert group["outcome"] == "pending"
    assert group["evidence_first_parse_ms"] == 3000
    assert group["evidence_terminal_parse_ms"] is None

    write_evidence(directory, evidence)
    observe(group, parse_visible(root, reads, clock=lambda: 8), clock=lambda: 8.1)
    assert group["outcome"] == "pending"
    assert group["evidence_terminal_parse_ms"] == 4000
    assert group["result_first_parse_ms"] == 2000
    assert group["result_status"] == "waiting_manifest_files"

    (directory / "Result02.json").write_bytes(second_raw)
    observe(group, parse_visible(root, reads, clock=lambda: 10), clock=lambda: 11)
    assert group["outcome"] == "succeeded"
    assert group["result_all_parse_ms"] == 6000
    assert group["terminal_snapshot_readable_ms"] == 7000
    assert group["manifest_verified_ms"] == 7000
    summary = summarize([group])
    assert summary["result"]["max_ms"] == 2000
    assert summary["all_results"]["max_ms"] == 6000
    assert summary["terminal_evidence"]["max_ms"] == 4000
    assert summary["threshold_policy"] == "product_acceptance_pending"


def test_one_source_overlap_does_not_claim_complete_group_or_different_camera(tmp_path):
    root, group, directory = setup_group(tmp_path)
    evidence = terminal_evidence(group, outcome="skipped")
    evidence.pop("group_id")
    evidence["sources"] = evidence["sources"][:1]
    write_evidence(directory, evidence)
    observe(group, parse_visible(root, {}, clock=lambda: 8), clock=lambda: 9)
    assert group["outcome"] == "pending"
    assert group["match_basis"] == "source_capture_id_overlap"
    assert group["terminal_completeness"] == "incomplete_source_capture_coverage"
    evidence["camera_id"] = "OtherCamera"
    write_evidence(directory, evidence)
    fresh = copy.deepcopy(group)
    fresh["evidence_first_parse_ms"] = None
    observe(fresh, parse_visible(root, {}, clock=lambda: 8), clock=lambda: 9)
    assert fresh["evidence_first_parse_ms"] is None


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing_tasks", "missing_or_invalid_task_outcomes"),
        ("duplicate_tasks", "incomplete_or_duplicate_task_ids"),
        ("pending_task", "nonterminal_task_outcomes"),
        ("wrong_outcome", "inconsistent_processing_outcome"),
        ("missing_capture", "missing_task_capture_ids"),
    ],
)
def test_terminal_claim_does_not_override_task_evidence(tmp_path, change, reason):
    root, group, directory = setup_group(tmp_path)
    evidence = terminal_evidence(group, outcome="skipped")
    if change == "missing_tasks":
        evidence.pop("task_outcomes")
    elif change == "duplicate_tasks":
        evidence["task_outcomes"][2] = copy.deepcopy(evidence["task_outcomes"][0])
    elif change == "pending_task":
        evidence["task_outcomes"][2]["terminal"] = False
    elif change == "wrong_outcome":
        evidence["processing_outcome"] = "succeeded"
    else:
        evidence["task_outcomes"][2].pop("capture_id")
    write_evidence(directory, evidence)
    observe(group, parse_visible(root, {}, clock=lambda: 8), clock=lambda: 9)
    assert group["outcome"] == "pending"
    assert group["terminal_completeness"] == reason


def test_invalid_json_and_symlinked_files_are_not_observed(tmp_path):
    root, group, directory = setup_group(tmp_path)
    (directory / "Evidence.json").write_text("[]")
    (directory / "Result.json").write_text('{"values":')
    outside = tmp_path / "Result.json"
    outside.write_text('{"values":[]}')
    (directory / "Result02.json").symlink_to(outside)
    assert parse_visible(root, {}, clock=lambda: 8) == ([], {})
    evidence = terminal_evidence(group, outcome="skipped")
    evidence["measurement_files"] = [None]
    write_evidence(directory, evidence)
    observe(group, parse_visible(root, {}, clock=lambda: 8), clock=lambda: 9)
    assert group["outcome"] == "pending"
    assert group["result_status"] == "missing_or_invalid_manifest"
    assert group["evidence_terminal_parse_ms"] == 4000
    expire(group, 124, 120)
    assert group["outcome"] == "timeout"
    assert group["timeout_stage"] == "missing_or_invalid_manifest"


def test_stale_result_generation_waits_and_preserves_first_parse(tmp_path):
    root, group, directory = setup_group(tmp_path)
    evidence = terminal_evidence(group)
    evidence["measurement_files"] = [result_manifest(directory, "Result.json", 63)]
    reads = {}
    first_visible = parse_visible(root, reads, clock=lambda: 6)
    assert parse_visible(root, reads, clock=lambda: 7)[1]["Day/Event/Result.json"][3] == 6
    result_manifest(directory, "Result.json", 64)
    write_evidence(directory, evidence)
    observe(group, parse_visible(root, reads, clock=lambda: 8), clock=lambda: 9)
    assert group["outcome"] == "pending" and group["result_first_parse_ms"] is None
    # An older successful JSON parse is recorded but cannot verify another generation.
    assert first_visible[1]["Day/Event/Result.json"][3] == 6
    expire(group, 124, 120)
    assert group["timeout_stage"] == "waiting_manifest_files"


@pytest.mark.parametrize(
    "outcome,status,classification",
    [
        ("skipped", "skipped", "recognition_skipped"),
        ("failed", "failed", "processing_failed"),
        ("succeeded", "unassigned", "unassigned_candidates"),
        ("succeeded", "unreadable", "unreadable"),
        ("succeeded", "completed", "unexplained_terminal_absence"),
    ],
)
def test_terminal_without_result_keeps_reason_and_is_not_a_reading(
    tmp_path, outcome, status, classification
):
    root, group, directory = setup_group(tmp_path)
    evidence = terminal_evidence(group, outcome=outcome)
    evidence.update(
        status=status, skip_reasons=["no_visible_bound_panel"] if outcome == "skipped" else []
    )
    write_evidence(directory, evidence)
    # Some old consumers alias Result to Evidence; this is not a six-field result.
    (directory / "Result.json").write_text(json.dumps(evidence))
    observe(group, parse_visible(root, {}, clock=lambda: 8), clock=lambda: 9)
    assert group["outcome"] == outcome
    assert group["result_first_parse_ms"] is None
    assert group["result_all_parse_ms"] is None
    assert group["terminal_snapshot_readable_ms"] == 5000
    assert group["result_absence_classification"] == classification
    report = summarize([group], evidence_target_ms=3000)
    assert report["result"]["sample_count"] == 0
    assert report["terminal_evidence"]["above_target_count"] == 1
    assert report["result_absence_classifications"] == {classification: 1}


def test_stage_report_preserves_recorded_clocks_and_missing_samples(tmp_path):
    root, group, directory = setup_group(tmp_path)
    evidence = terminal_evidence(group, outcome="skipped")
    timing = {
        "durations_ms": {
            "queue_wait_ms": 15,
            "ocr_execution_ms": 20,
            "ocr_model_call_ms": 11,
            "vision_ms": None,
            "ocr_ms": -1,
            "invalid": True,
        },
        "duration_clock_basis": {
            "queue_wait_ms": "wall_clock_timestamp_difference",
            "ocr_execution_ms": "process_monotonic",
            "ocr_model_call_ms": "process_monotonic",
        },
        "source_clock_basis": "NAS file mtime; clocks not independently synchronized",
    }
    archive = {
        "publication_started_at": "2026-10-08T00:00:00+00:00",
        "receipt_publication_ms": 3,
        "archive_queue_wait_ms": 9,
        "sequence": 42,
        "duration_clock_basis": {
            "receipt_publication_ms": "process_monotonic",
            "archive_queue_wait_ms": "wall_clock_timestamp_difference",
        },
    }
    evidence["observations"] = [
        {"job_id": "job-0", "capture_id": "local-0", "timing": timing, "archive": archive},
        {"job_id": "superseded-job", "timing": {"durations_ms": {"ocr_execution_ms": 999}}},
    ]
    evidence["timing"] = {
        "durations_ms": {"candidate_merge_ms": 2},
        "duration_clock_basis": {"candidate_merge_ms": "process_monotonic"},
    }
    write_evidence(directory, evidence)
    observe(group, parse_visible(root, {}, clock=lambda: 8), clock=lambda: 9)
    assert group["task_timings"] == [
        {"task_id": "job-0", "capture_id": "local-0", "timing": timing, "archive": archive}
    ]
    report = summarize([group])
    stages = report["stages"]
    assert stages["tasks"]["ocr_execution_ms"]["recorded_count"] == 1
    assert stages["tasks"]["ocr_execution_ms"]["missing_count"] == 2
    assert stages["tasks"]["ocr_execution_ms"]["mean_ms"] == 20
    assert stages["tasks"]["ocr_model_call_ms"]["mean_ms"] == 11
    assert (
        stages["tasks"]["queue_wait_ms"]["by_clock_basis"]["wall_clock_timestamp_difference"][
            "sample_count"
        ]
        == 1
    )
    assert stages["tasks"]["vision_ms"]["recorded_count"] == 0
    assert stages["tasks"]["ocr_ms"]["mean_ms"] is None
    assert "invalid" not in stages["tasks"]
    assert stages["groups"]["candidate_merge_ms"]["mean_ms"] == 2
    assert stages["task_archive"]["receipt_publication_ms"]["mean_ms"] == 3
    assert "sequence" not in stages["task_archive"]
    assert "must not be summed" in report["stage_scope"]
    assert timing["durations_ms"]["vision_ms"] is None  # No missing clock data is fabricated.


def test_timeout_without_any_evidence_has_separate_denominator(tmp_path):
    root, group, directory = setup_group(tmp_path)
    expire(group, 123, 120)
    assert group["outcome"] == "pending"
    expire(group, 124, 120)
    assert group["outcome"] == "timeout"
    assert group["timeout_stage"] == "waiting_evidence"
    report = summarize([group])
    assert report["expected_task_count"] == 3 and report["observed_task_count"] == 0
    assert report["terminal_snapshot"]["sample_count"] == 0
    assert report["stages"]["tasks"]["queue_wait_ms"]["missing_count"] == 3


def test_a_returning_digest_does_not_backdate_snapshot_readability(tmp_path):
    root, group, directory = setup_group(tmp_path)
    reads = {}
    evidence = terminal_evidence(group)
    result_a = result_manifest(directory, "Result.json", 63)
    evidence["measurement_files"] = [result_a]
    parse_visible(root, reads, clock=lambda: 6)  # A was parseable before Evidence existed.
    result_manifest(directory, "Result.json", 64)  # Generation B replaces it.
    write_evidence(directory, evidence)
    observe(group, parse_visible(root, reads, clock=lambda: 7), clock=lambda: 7.1)
    assert group["outcome"] == "pending"
    result_manifest(directory, "Result.json", 63)  # A returns; its old first stamp is 6.
    observe(group, parse_visible(root, reads, clock=lambda: 8), clock=lambda: 9)
    assert group["result_first_parse_ms"] == 2000
    assert group["evidence_terminal_parse_ms"] == 3000
    assert group["terminal_snapshot_readable_ms"] == 5000
    assert group["terminal_snapshot_basis"] == "current_reader_manifest_validation_completed"


def test_task_terminal_but_top_level_pending_is_a_distinct_timeout(tmp_path):
    root, group, directory = setup_group(tmp_path)
    evidence = terminal_evidence(group, outcome="skipped")
    evidence["processing_terminal"] = False
    write_evidence(directory, evidence)
    observe(group, parse_visible(root, {}, clock=lambda: 8), clock=lambda: 9)
    assert group["outcome"] == "pending" and group["terminal_completeness"] == "complete"
    expire(group, 124, 120)
    assert group["timeout_stage"] == "waiting_top_level_terminal_evidence"


def test_archive_timing_echo_refreshes_after_terminal_without_moving_latency(tmp_path):
    root, group, directory = setup_group(tmp_path)
    evidence = terminal_evidence(group, outcome="skipped")
    evidence["publication"] = {"generation": "generation-a"}
    write_evidence(directory, evidence)
    reads = {}
    observe(group, parse_visible(root, reads, clock=lambda: 8), clock=lambda: 9)
    original_digest = group["terminal_evidence_sha256"]
    assert group["metadata_status"] == "waiting_archive_timing"
    evidence["archive"] = {"readable_at": "2026-10-08T00:00:00+00:00"}
    evidence["observations"] = [
        {
            "job_id": f"job-{i}",
            "capture_id": f"local-{i}",
            "timing": {
                "archive_readable_at": "2026-10-08T00:00:00+00:00",
                "durations_ms": {"ocr_execution_ms": 20 + i},
                "duration_clock_basis": {"ocr_execution_ms": "process_monotonic"},
            },
            "archive": {
                "receipt_publication_ms": 3,
                "duration_clock_basis": {"receipt_publication_ms": "process_monotonic"},
            },
        }
        for i in range(3)
    ]
    write_evidence(directory, evidence)
    observe(group, parse_visible(root, reads, clock=lambda: 10), clock=lambda: 11)
    assert group["metadata_status"] == "complete" and group["metadata_missing_fields"] == []
    assert group["evidence_terminal_parse_ms"] == 4000
    assert group["terminal_snapshot_readable_ms"] == group["manifest_verified_ms"] == 5000
    assert group["terminal_evidence_sha256"] == original_digest
    assert group["metadata_evidence_sha256"] != original_digest
    assert summarize([group])["stages"]["tasks"]["ocr_execution_ms"]["recorded_count"] == 3


def test_metadata_wait_is_bounded_and_ignores_a_changed_generation(tmp_path):
    root, group, directory = setup_group(tmp_path)
    evidence = terminal_evidence(group, outcome="skipped")
    evidence["publication"] = {"generation": "generation-a"}
    write_evidence(directory, evidence)
    observe(group, parse_visible(root, {}, clock=lambda: 8), clock=lambda: 9)
    evidence["publication"]["generation"] = "generation-b"
    evidence["timing"] = {"durations_ms": {"candidate_merge_ms": 999}}
    write_evidence(directory, evidence)
    observe(group, parse_visible(root, {}, clock=lambda: 10), clock=lambda: 11)
    assert group["group_timing"] == {}
    expire_metadata(group, 13, 5)
    assert group["metadata_status"] == "waiting_archive_timing"
    expire_metadata(group, 14, 5)
    assert group["metadata_status"] == "timed_out_missing_archive_timing"
    assert group["metadata_wait_ms"] == 5000
    assert group["outcome"] == "skipped" and group["terminal_snapshot_readable_ms"] == 5000


def test_run_collects_final_groups_later_timing_echo(tmp_path, monkeypatch, capsys):
    root = initialize(tmp_path / "replay")
    photo = tmp_path / "input.png"
    photo.write_bytes(b"isolated replay")
    now = [0.0]
    calls = []
    injected = []
    original_inject, original_parse, original_observe = (
        replay.inject_group,
        replay.parse_visible,
        replay.observe,
    )
    monkeypatch.setattr(replay.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(replay.time, "sleep", lambda amount: now.__setitem__(0, now[0] + amount))

    def inject(*args, **kwargs):
        group = original_inject(*args, **kwargs, clock=lambda: now[0])
        injected.append(group)
        return group

    def parse(root, reads):
        calls.append(now[0])
        assert len(calls) <= 3  # This isolated run should finish on the confirmation echo.
        directory = root / ARCHIVE_DIRECTORY / "Day" / "Event"
        directory.mkdir(parents=True, exist_ok=True)
        evidence = terminal_evidence(injected[0], outcome="skipped")
        if len(calls) > 1:
            evidence["archive"] = {"readable_at": "2026-10-08T00:00:00+00:00"}
            evidence["observations"] = [
                {
                    "job_id": f"job-{i}",
                    "timing": {"archive_readable_at": "2026-10-08T00:00:00+00:00"},
                }
                for i in range(3)
            ]
        write_evidence(directory, evidence)
        return original_parse(root, reads, clock=lambda: now[0])

    monkeypatch.setattr(replay, "inject_group", inject)
    monkeypatch.setattr(replay, "parse_visible", parse)
    monkeypatch.setattr(
        replay,
        "observe",
        lambda group, visible: original_observe(group, visible, clock=lambda: now[0]),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "replay",
            "run",
            "--root",
            str(root),
            "--camera-id",
            "TestCamera",
            "--images",
            str(photo),
            str(photo),
            str(photo),
            "--groups",
            "1",
            "--metadata-timeout",
            "1",
        ],
    )
    replay.main()
    capsys.readouterr()
    report = json.loads(next((root / "reports").glob("*.json")).read_text())
    assert len(calls) == 2
    assert report["groups"][0]["metadata_status"] == "complete"
    assert report["groups"][0]["terminal_snapshot_readable_ms"] == 0
    assert report["groups"][0]["metadata_observed_ms"] == 200
