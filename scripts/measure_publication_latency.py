#!/usr/bin/env python3
"""Isolated three-photo replay and independently parsed Evidence/Result latency.

This script never starts services, edits a database, calls OCR or writes outside
an explicitly initialized empty test root. Configure a test service separately.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import time
import uuid
from zoneinfo import ZoneInfo

MARKER = ".field-recognition-test-only"
ARCHIVE_DIRECTORY = "FieldRecognitionArchive"
TASK_STAGES = (
    "write_to_detect_ms",
    "file_stability_ms",
    "ingest_wait_ms",
    "file_read_ms",
    "import_ms",
    "queue_wait_ms",
    "ocr_wait_ms",
    "ocr_scheduler_wait_ms",
    "ocr_execution_ms",
    "panel_detection_ms",
    "ocr_ms",
    "ocr_model_lock_wait_ms",
    "ocr_model_prepare_ms",
    "ocr_model_call_ms",
    "vision_queue_wait_ms",
    "vision_ms",
    "processing_ms",
    "result_generation_ms",
    "result_finalize_ms",
    "archive_publication_ms",
    "archive_readability_ms",
    "write_to_result_ms",
    "write_to_readable_result_ms",
)
ARCHIVE_STAGES = (
    "archive_queue_wait_ms",
    "receipt_publication_ms",
    "archive_queue_ms",
    "archive_publication_ms",
    "archive_readability_ms",
)


def archive_root(root):
    """Use the runtime's required basename; read old isolated reports compatibly."""
    for name in (ARCHIVE_DIRECTORY, "archive"):
        candidate = root / name
        candidate.resolve().relative_to(root)
        if candidate.is_dir():
            return candidate
    raise ValueError("initialized test archive directory required")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def atomic_bytes(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(root):
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("init requires a new or empty directory")
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(root / MARKER, {"scope": "test_only", "created_at": utc_now()})
    for name in ("voice_photos", ARCHIVE_DIRECTORY, "reports"):
        (root / name).mkdir()
    return root


def test_root(root):
    root = Path(root).resolve()
    marker = json.loads((root / MARKER).read_text())
    if not isinstance(marker, dict) or marker.get("scope") != "test_only":
        raise ValueError("test_only root marker required")
    for name in ("voice_photos", "reports"):
        (root / name).resolve().relative_to(root)
        if not (root / name).is_dir():
            raise ValueError("initialized test directory required")
    archive_root(root)
    return root


def inject_group(root, camera_id, images, *, clock=time.monotonic, moment=None):
    root = test_root(root)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", camera_id) or len(images) != 3:
        raise ValueError("one camera and exactly three images required")
    moment = (moment or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Shanghai"))
    directory = root / "voice_photos" / camera_id / moment.strftime("%Y-%m-%d/%H-%M-%S")
    directory.resolve().relative_to(root)
    if directory.exists():
        raise ValueError("capture directory already exists; use interval >= 1 second")
    group_id = "latency-" + uuid.uuid4().hex
    payloads, photos = [], []
    for index, source in enumerate(images):
        source = Path(source)
        suffix = source.suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg"}:
            raise ValueError("images must be PNG or JPEG")
        raw = source.read_bytes()
        filename = moment.strftime("%Y%m%d_%H%M%S") + f"_{index:03d}" + suffix
        capture_id = str(uuid.uuid4())
        photos.append(
            {
                "filename": filename,
                "capture_id": capture_id,
                "captured_at": moment.isoformat(),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        payloads.append((filename, raw))
    receipt = {
        "schema_version": "field-photo-receipt/1",
        "camera_id": camera_id,
        "measurement": {
            "burst_id": group_id,
            "expected_photos": 3,
            "experiment_context_ref": "test-only://latency/" + group_id,
            "instrument_ids": [],
        },
        "photos": photos,
    }
    atomic_json(directory / "PhotoReceipt.json", receipt)
    writes = []
    for filename, raw in payloads:
        atomic_bytes(directory / filename, raw)
        writes.append(clock())
    # This is the writer client's completion, not NAS server time or physical capture.
    t0 = clock()
    return {
        "group_id": group_id,
        "camera_id": camera_id,
        "expected_tasks": 3,
        "source_capture_ids": [p["capture_id"] for p in photos],
        "t0_utc": utc_now(),
        "t0_monotonic": t0,
        "photo_write_completed_monotonic": writes,
        "capture_time_basis": "test_replay_injection_time_not_original_photo_time",
        "evidence_first_parse_ms": None,
        "result_first_parse_ms": None,
        "evidence_terminal_parse_ms": None,
        "result_all_parse_ms": None,
        "terminal_snapshot_readable_ms": None,
        "manifest_verified_ms": None,
        "outcome": "pending",
        "metadata_status": "not_terminal",
        "task_outcomes": [],
        "task_timings": [],
        "result_files": [],
        "clock_basis": "writer_and_reader_same_process_monotonic",
        "measurement_start": "writer_client_completed_all_three_photo_writes",
    }


def parse_visible(root, first_reads, *, clock=time.monotonic, groups=None, reader_state=None):
    """Read current physical bytes; optionally limit polling to active events.

    System receipts are never business Evidence/Result files. A focused reader
    retains only small event identity headers, so completed events can be pruned
    during discovery. Active Evidence and Results are read afresh even when file
    metadata is unchanged: cached bytes cannot prove a current manifest snapshot.
    Omit groups to preserve the full business-file observation mode.
    """
    evidence, results = [], {}
    base = archive_root(Path(root).resolve())
    focused = groups is not None
    groups = list(groups) if focused else None
    if focused and not groups:
        return evidence, results
    state = reader_state if reader_state is not None else {}
    identities = state.setdefault("event_identities", {}) if focused else {}

    def interested(directory):
        header = identities.get(str(directory))
        return header is None or any(group_match(group, header) for group in groups)

    candidates = []
    for directory, children, filenames in os.walk(base, followlinks=False):
        directory = Path(directory)
        children[:] = [
            name
            for name in children
            if name != ".System" and (not focused or interested(directory / name))
        ]
        if focused and not interested(directory):
            continue
        candidates.extend(
            directory / name
            for name in filenames
            if name == "Evidence.json" or re.fullmatch(r"Result(?:\d+)?\.json", name)
        )
    for path in sorted(candidates):
        try:
            path.resolve().relative_to(base.resolve())
            raw = path.read_bytes()
            value = json.loads(raw)
            if not isinstance(value, dict):
                continue
        except (OSError, ValueError):
            continue  # A non-atomic backend remains unobserved until parsing succeeds.
        digest = hashlib.sha256(raw).hexdigest()
        stamp = first_reads.setdefault((str(path), digest), clock())
        entry = (path, value, digest, stamp)
        if path.name == "Evidence.json":
            if focused:
                decision = value.get("decision")
                context = decision.get("context") if isinstance(decision, dict) else None
                context = context if isinstance(context, dict) else {}
                mapping = source_mapping(value)
                # Do not freeze an early incomplete event with no tracking IDs.
                # A later publication may supply its first source relationship.
                # Standalone group_id can be an internal capture ID before its
                # source receipt arrives; it cannot prove a foreign event yet.
                if context.get("burst_id") or mapping:
                    identities[str(path.parent)] = {
                        "camera_id": value.get("camera_id"),
                        "group_id": value.get("group_id"),
                        "decision": {"context": {"burst_id": context.get("burst_id")}},
                        "sources": [
                            {"capture_id": local, "external_photo": {"capture_id": source}}
                            for local, source in mapping.items()
                        ],
                    }
            evidence.append(entry)
        else:
            results[path.relative_to(base).as_posix()] = entry
    return evidence, results


def source_mapping(value):
    sources = value.get("sources")
    if not isinstance(sources, list):
        return {}
    return {
        source.get("capture_id"): (source.get("external_photo") or {}).get("capture_id")
        for source in sources
        if isinstance(source, dict)
        and isinstance(source.get("external_photo") or {}, dict)
        and isinstance(source.get("capture_id"), str)
        and isinstance((source.get("external_photo") or {}).get("capture_id"), str)
    }


def group_match(group, value):
    if value.get("camera_id") and value["camera_id"] != group["camera_id"]:
        return None
    decision = value.get("decision") or {}
    context = decision.get("context") if isinstance(decision, dict) else None
    context = context if isinstance(context, dict) else {}
    if value.get("group_id") == group["group_id"] or context.get("burst_id") == group["group_id"]:
        return "declared_group_id"
    if set(source_mapping(value).values()).intersection(group["source_capture_ids"]):
        return "source_capture_id_overlap"
    return None


def terminal_completeness(group, value):
    """One visible photograph or a top-level claim does not finish a burst."""
    tasks = value.get("task_outcomes")
    if not isinstance(tasks, list) or not all(isinstance(t, dict) for t in tasks):
        return "missing_or_invalid_task_outcomes"
    ids = {t.get("task_id") for t in tasks if isinstance(t.get("task_id"), str) and t["task_id"]}
    if len(ids) != len(tasks) or len(ids) < group["expected_tasks"]:
        return "incomplete_or_duplicate_task_ids"
    if not all(
        t.get("terminal") is True and t.get("outcome") in {"succeeded", "skipped", "failed"}
        for t in tasks
    ):
        return "nonterminal_task_outcomes"
    outcomes = {t["outcome"] for t in tasks}
    expected_outcome = (
        "failed" if "failed" in outcomes else "skipped" if outcomes == {"skipped"} else "succeeded"
    )
    if value.get("processing_outcome") != expected_outcome:
        return "inconsistent_processing_outcome"
    mapping = source_mapping(value)
    expected = set(group["source_capture_ids"])
    if any(not isinstance(t.get("capture_id"), str) for t in tasks):
        return "missing_task_capture_ids"
    covered = {mapping.get(t.get("capture_id"), t.get("capture_id")) for t in tasks}
    if not expected.issubset(covered):
        return "incomplete_source_capture_coverage"
    return "complete"


def durations(value):
    if not isinstance(value, dict):
        return {}
    return {
        name: amount
        for name, amount in value.items()
        if isinstance(amount, (float, int))
        and not isinstance(amount, bool)
        and math.isfinite(amount)
        and amount >= 0
    }


def extract_timings(value):
    task_outcomes = value.get("task_outcomes")
    current = (
        {
            t.get("task_id")
            for t in task_outcomes
            if isinstance(t, dict) and isinstance(t.get("task_id"), str)
        }
        if isinstance(task_outcomes, list)
        else set()
    )
    tasks = {}
    observations = value.get("observations")
    for observation in observations if isinstance(observations, list) else []:
        if (
            not isinstance(observation, dict)
            or not isinstance(observation.get("job_id"), str)
            or observation["job_id"] not in current
        ):
            continue
        timing = observation.get("timing") or {}
        archive = observation.get("archive") or {}
        if not isinstance(timing, dict) or not isinstance(archive, dict):
            continue
        tasks[observation["job_id"]] = {
            "task_id": observation["job_id"],
            "capture_id": observation.get("capture_id"),
            "timing": copy.deepcopy(timing),
            "archive": copy.deepcopy(archive),
        }
    return list(tasks.values())


def absence_classification(value):
    if value.get("processing_outcome") == "skipped":
        return "recognition_skipped"
    if value.get("processing_outcome") == "failed":
        return "processing_failed"
    unbound = value.get("unbound_readings")
    unbound = unbound if isinstance(unbound, dict) else {}
    if value.get("status") == "unassigned" or unbound.get("count", 0):
        return "unassigned_candidates"
    if value.get("status") == "unreadable":
        return "unreadable"
    return "unexplained_terminal_absence"


def metadata_identity(value):
    publication = value.get("publication")
    publication = publication if isinstance(publication, dict) else {}
    manifest = value.get("measurement_files")
    if not isinstance(manifest, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("result"), str)
        and isinstance(item.get("sha256"), str)
        for item in manifest
    ):
        return None
    tasks = value.get("task_outcomes")
    if not isinstance(tasks, list) or not all(
        isinstance(t, dict) and isinstance(t.get("task_id"), str) for t in tasks
    ):
        return None
    return {
        "generation": publication.get("generation"),
        "task_ids": sorted(t["task_id"] for t in tasks),
        "manifest": sorted((item["result"], item["sha256"]) for item in manifest),
    }


def aware_time(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def refresh_metadata(group, value, digest, now):
    """Archive confirmation is echoed later; keep the measured first snapshot."""
    group.update(
        task_timings=extract_timings(value),
        group_timing=copy.deepcopy(value.get("timing") or {}),
        group_archive=copy.deepcopy(value.get("archive") or {}),
        metadata_evidence_sha256=digest,
    )
    task_timings = {t["task_id"]: t for t in group["task_timings"]}
    missing = []
    for task in group["task_outcomes"]:
        row = task_timings.get(task["task_id"]) or {}
        timing, archive = row.get("timing") or {}, row.get("archive") or {}
        if not aware_time(timing.get("archive_readable_at") or archive.get("readable_at")):
            missing.append(f"task[{task['task_id']}].archive_readable_at")
    archive = group["group_archive"]
    if not isinstance(archive, dict) or not aware_time(archive.get("readable_at")):
        missing.append("group.archive.readable_at")
    group.update(
        metadata_status="waiting_archive_timing" if missing else "complete",
        metadata_missing_fields=missing,
        metadata_observed_ms=round((now - group["t0_monotonic"]) * 1000, 2),
        metadata_scope="archive_writer_confirmation_fields; stage measurements may remain missing",
    )


def observe(group, visible, *, clock=time.monotonic):
    evidences, results = visible
    matches = [(entry, group_match(group, entry[1])) for entry in evidences]
    matches = [(entry, basis) for entry, basis in matches if basis]
    declared = [row for row in matches if row[1] == "declared_group_id"]
    matches = declared or matches
    elapsed = lambda tick: round((tick - group["t0_monotonic"]) * 1000, 2)
    for (path, value, digest, stamp), basis in matches:
        if group["outcome"] != "pending":
            if (
                group.get("terminal_identity") == metadata_identity(value)
                and value.get("processing_terminal") is True
                and terminal_completeness(group, value) == "complete"
            ):
                refresh_metadata(group, value, digest, clock())
                break
            continue
        first = elapsed(stamp)
        previous = group["evidence_first_parse_ms"]
        group["evidence_first_parse_ms"] = min(previous, first) if previous is not None else first
        group.update(
            evidence_path=str(path),
            evidence_sha256=digest,
            match_basis=basis,
            processing_terminal=value.get("processing_terminal"),
            evidence_status=value.get("status"),
            task_outcomes=value.get("task_outcomes")
            if isinstance(value.get("task_outcomes"), list)
            else [],
            task_timings=extract_timings(value),
            group_timing=copy.deepcopy(value.get("timing") or {}),
            group_archive=copy.deepcopy(value.get("archive") or {}),
        )
        completeness = terminal_completeness(group, value)
        group["terminal_completeness"] = completeness
        if value.get("processing_terminal") is True and completeness == "complete":
            if group["evidence_terminal_parse_ms"] is None:
                group["evidence_terminal_parse_ms"] = elapsed(stamp)
        manifest = value.get("measurement_files")
        if not isinstance(manifest, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("result"), str)
            and re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
            for item in manifest
        ):
            group["result_status"] = "missing_or_invalid_manifest"
            continue
        if len({item["result"] for item in manifest}) != len(manifest):
            group["result_status"] = "duplicate_manifest_paths"
            continue
        parsed = []
        for item in manifest:
            result = results.get(item["result"])
            if not result or result[2] != item.get("sha256"):
                continue
            record = result[1]
            if not isinstance(record, dict) or not isinstance(record.get("values"), list):
                continue
            parsed.append(
                {"path": item["result"], "sha256": result[2], "first_parse_ms": elapsed(result[3])}
            )
        if parsed:
            first_result = min(row["first_parse_ms"] for row in parsed)
            previous = group["result_first_parse_ms"]
            group["result_first_parse_ms"] = (
                min(previous, first_result) if previous is not None else first_result
            )
        group["result_files"] = parsed
        group["expected_result_count"] = len(manifest)
        group["result_status"] = (
            "waiting_manifest_files"
            if len(parsed) != len(manifest)
            else "waiting_complete_terminal_evidence"
        )
        if value.get("processing_terminal") is True and completeness == "complete":
            if len(parsed) == len(manifest):
                verified_at = clock()
                group["manifest_verified_ms"] = elapsed(verified_at)
                group["outcome"] = value.get("processing_outcome", "unknown_terminal")
                group["result_status"] = "parsed" if manifest else "absent_terminal"
                group["skip_reasons"] = value.get("skip_reasons", [])
                group["result_absence_classification"] = (
                    None if manifest else absence_classification(value)
                )
                group["result_all_parse_ms"] = max(
                    (row["first_parse_ms"] for row in parsed), default=None
                )
                # Component bytes can disappear and later reappear (A -> B -> A).
                # Their first parse times do not prove they coexisted as a snapshot.
                group["terminal_snapshot_readable_ms"] = group["manifest_verified_ms"]
                group["terminal_snapshot_basis"] = "current_reader_manifest_validation_completed"
                group["result_parse_scope"] = (
                    "first_parse_of_each_current_manifest_file_digest; not simultaneous_snapshot_visibility"
                )
                group["terminal_evidence_sha256"] = digest
                group["terminal_identity"] = metadata_identity(value)
                group["metadata_wait_started_monotonic"] = verified_at
                refresh_metadata(group, value, digest, verified_at)
                break
    return group


def stage_statistics(rows, expected_count, stage_names=(), *, direct=False):
    values = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        data = durations(row if direct else row.get("durations_ms"))
        if direct:
            data = {name: amount for name, amount in data.items() if name.endswith("_ms")}
        basis = row.get("duration_clock_basis")
        values.append((data, basis if isinstance(basis, dict) else {}))
    names = set(stage_names).union(name for data, _ in values for name in data)
    result = {}
    for name in sorted(names):
        observed = [
            (data[name], basis.get(name, "not_recorded")) for data, basis in values if name in data
        ]
        amounts = [amount for amount, _ in observed]
        by_clock = {}
        for amount, basis in observed:
            by_clock.setdefault(str(basis), []).append(amount)
        result[name] = {
            "recorded_count": len(amounts),
            "expected_count": expected_count,
            "missing_count": max(0, expected_count - len(amounts)),
            "min_ms": min(amounts) if amounts else None,
            "max_ms": max(amounts) if amounts else None,
            "mean_ms": round(statistics.mean(amounts), 2) if amounts else None,
            "by_clock_basis": {
                basis: {
                    "sample_count": len(data),
                    "min_ms": min(data),
                    "max_ms": max(data),
                    "mean_ms": round(statistics.mean(data), 2),
                }
                for basis, data in by_clock.items()
            },
        }
    return result


def summarize(groups, *, evidence_target_ms=None, result_target_ms=None):
    def metric(name, target):
        values = [row[name] for row in groups if row.get(name) is not None]
        result = {
            "sample_count": len(values),
            "denominator": "groups_with_observed_" + name,
            "min_ms": min(values) if values else None,
            "max_ms": max(values) if values else None,
            "mean_ms": round(statistics.mean(values), 2) if values else None,
            "target_ms": target,
        }
        if target is not None:
            result["above_target_count"] = sum(value > target for value in values)
        return result

    tasks = {
        item["task_id"]: item
        for group in groups
        for item in group["task_outcomes"]
        if isinstance(item, dict) and isinstance(item.get("task_id"), str)
    }
    expected_tasks = sum(g["expected_tasks"] for g in groups)
    task_timings = {
        item["task_id"]: item for group in groups for item in group.get("task_timings", [])
    }
    return {
        "group_count": len(groups),
        "group_outcomes": dict(Counter(g["outcome"] for g in groups)),
        "expected_task_count": expected_tasks,
        "observed_task_count": len(tasks),
        "task_outcomes": dict(Counter(t.get("outcome", "unknown") for t in tasks.values())),
        "result_absence_classifications": dict(
            Counter(
                g["result_absence_classification"]
                for g in groups
                if g.get("result_absence_classification")
            )
        ),
        "metadata_outcomes": dict(
            Counter(g.get("metadata_status", "not_recorded") for g in groups)
        ),
        "evidence": metric("evidence_first_parse_ms", evidence_target_ms),
        "result": metric("result_first_parse_ms", result_target_ms),
        "terminal_evidence": metric("evidence_terminal_parse_ms", evidence_target_ms),
        "all_results": metric("result_all_parse_ms", result_target_ms),
        "terminal_snapshot": metric("terminal_snapshot_readable_ms", None),
        "terminal_manifest": metric("manifest_verified_ms", None),
        "stages": {
            "tasks": stage_statistics(
                [t["timing"] for t in task_timings.values()], expected_tasks, TASK_STAGES
            ),
            "task_archive": stage_statistics(
                [t["archive"] for t in task_timings.values()],
                expected_tasks,
                ARCHIVE_STAGES,
                direct=True,
            ),
            "groups": stage_statistics([g.get("group_timing") for g in groups], len(groups)),
            "group_archive": stage_statistics(
                [g.get("group_archive") for g in groups], len(groups), ARCHIVE_STAGES, direct=True
            ),
        },
        "stage_scope": "recorded task/group timing and archive fields; overlapping stages must not be summed",
        "clock_basis": "writer_and_reader_same_process_monotonic; UTC timestamps are labels only",
        "measurement_scope": "all_three_photo_write_completion_to_JSON_parse; terminal_snapshot requires all tasks terminal and manifest hashes verified",
        "threshold_policy": "product_acceptance_pending"
        if evidence_target_ms is None and result_target_ms is None
        else "caller_supplied_not_product_approval",
        "limitations": "poll interval and client cache affect visibility; small samples do not establish long-term stability",
    }


def expire(group, now, timeout_seconds):
    if group["outcome"] != "pending" or now - group["t0_monotonic"] < timeout_seconds:
        return group
    if group.get("evidence_first_parse_ms") is None:
        stage = "waiting_evidence"
    elif group.get("terminal_completeness") != "complete":
        stage = "waiting_complete_terminal_evidence"
    elif group.get("processing_terminal") is not True:
        stage = "waiting_top_level_terminal_evidence"
    else:
        stage = group.get("result_status", "waiting_manifest_files")
    group.update(
        outcome="timeout",
        timeout_stage=stage,
        wait_ms=round((now - group["t0_monotonic"]) * 1000, 2),
    )
    return group


def expire_metadata(group, now, timeout_seconds):
    if (
        group.get("metadata_status") == "waiting_archive_timing"
        and now - group["metadata_wait_started_monotonic"] >= timeout_seconds
    ):
        group.update(
            metadata_status="timed_out_missing_archive_timing",
            metadata_wait_ms=round((now - group["metadata_wait_started_monotonic"]) * 1000, 2),
        )
    return group


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--root", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--camera-id", required=True)
    run.add_argument("--images", nargs=3, type=Path, required=True)
    run.add_argument("--groups", type=int, default=10)
    run.add_argument("--interval", type=float, default=60)
    run.add_argument("--timeout", type=float, default=120)
    run.add_argument("--poll", type=float, default=0.2)
    run.add_argument(
        "--metadata-timeout",
        type=float,
        default=5,
        help="additional bounded wait for the archive writer timing echo; does not change first readable times",
    )
    run.add_argument("--evidence-target-ms", type=float)
    run.add_argument("--result-target-ms", type=float)
    args = parser.parse_args()
    if args.command == "init":
        print(initialize(args.root))
        return
    if (
        args.groups < 1
        or not math.isfinite(args.interval)
        or args.interval < 1
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
        or not 0.01 <= args.poll <= 60
    ):
        parser.error("groups >= 1, interval >= 1, timeout > 0, 0.01 <= poll <= 60 required")
    if any(
        target is not None and (not math.isfinite(target) or target <= 0)
        for target in (args.evidence_target_ms, args.result_target_ms)
    ):
        parser.error(
            "optional targets must be finite and > 0; omit until acceptance thresholds are agreed"
        )
    if not math.isfinite(args.metadata_timeout) or args.metadata_timeout < 0:
        parser.error("metadata-timeout must be finite and >= 0")
    root = test_root(args.root)
    report = root / "reports" / ("latency-" + uuid.uuid4().hex + ".json")
    groups, first_reads = [], {}
    next_injection = time.monotonic()

    def waiting(group):
        return (
            group["outcome"] == "pending"
            or group.get("metadata_status") == "waiting_archive_timing"
        )

    while len(groups) < args.groups or any(waiting(g) for g in groups):
        now = time.monotonic()
        if len(groups) < args.groups and now >= next_injection:
            groups.append(inject_group(root, args.camera_id, args.images))
            next_injection = now + args.interval
        visible = parse_visible(root, first_reads)
        for group in groups:
            if not waiting(group):
                continue
            observe(group, visible)
            expire(group, time.monotonic(), args.timeout)
            expire_metadata(group, time.monotonic(), args.metadata_timeout)
        result = {
            "schema": "field-publication-latency/2",
            "scope": "test_only",
            "poll_seconds": args.poll,
            "timeout_seconds": args.timeout,
            "archive_root": str(archive_root(root)),
            "metadata_timeout_seconds": args.metadata_timeout,
            "groups": groups,
            "summary": summarize(
                groups,
                evidence_target_ms=args.evidence_target_ms,
                result_target_ms=args.result_target_ms,
            ),
        }
        atomic_json(report, result)
        if any(waiting(g) for g in groups) or len(groups) < args.groups:
            time.sleep(args.poll)
    print(
        json.dumps(
            {"report": str(report), "summary": result["summary"]}, ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
