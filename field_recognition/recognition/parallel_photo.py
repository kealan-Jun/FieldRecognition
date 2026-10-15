"""Concurrent photo readers; only the task owner publishes the current projection.

Bound photos share one detector result before both readers start. Cloud reads
the same authorized source crops, independently of local OCR/refinement. A
cloud-first completion may finish the task; a late local result is audit only.
"""

import copy
import time
from dataclasses import replace
from decimal import Decimal

from field_recognition.recognition.config import RecognitionConfig
from field_recognition.recognition.panel_regions import finish as finish_regions
from field_recognition.measurements.reading_results import build_readings
from field_recognition.measurements.readout_rules import numeric_review_conflict
from field_recognition.measurements.measurement_records import build_records, display_fields


def _project(core, document, local, image, pipeline, valid, clock, started):
    """Use the existing association/validation transaction without another model call."""
    if local.get("panel_regions"):
        finish_regions(
            core,
            document,
            local,
            image,
            pipeline=pipeline,
            valid=valid,
            clock=clock,
            started=started,
        )
    else:
        document.update(copy.deepcopy(local))
        document["local_ocr"] = copy.deepcopy(local)


def _cloud(core, original, template, image, panel, pipeline, valid, started):
    from field_recognition.recognition.panel_readout import primary_unbound_photo

    document = copy.deepcopy(original)
    try:
        if template.get("panel_regions"):
            finish_regions(
                core,
                document,
                template,
                image,
                pipeline=pipeline,
                valid=valid,
                started=started,
            )
        else:
            primary_unbound_photo(
                core,
                document,
                panel,
                pipeline=pipeline,
                valid=valid,
                clock=time.monotonic,
                pause=time.sleep,
                started=started,
            )
    except Exception as exc:
        document["fallback"] = {"status": "failed", "error": type(exc).__name__}
    return document


def _merge(document, reviewed, local_ready):
    """Replace eligible fields within this photograph; failures keep the initial view."""
    fallback = reviewed.get("fallback", {})

    def values(doc):
        return {
            (r.get("panel_id"), Decimal(r["value"]), r.get("unit"))
            for r in build_readings(doc)
            if not r.get("quality_issue") and r.get("value") is not None
        }

    before = values(document)
    cloud_values = values(reviewed)

    def conflict_for(panel_id):
        initial_values = [(value, unit) for pid, value, unit in before if pid == panel_id]
        reviewed_values = [(value, unit) for pid, value, unit in cloud_values if pid == panel_id]
        if len(initial_values) != 1 or len(reviewed_values) != 1:
            return None
        (initial_value, initial_unit), (reviewed_value, reviewed_unit) = (
            initial_values[0],
            reviewed_values[0],
        )
        if initial_unit and reviewed_unit and initial_unit != reviewed_unit:
            return None  # Existing field/unit validation handles that conflict.
        reason = numeric_review_conflict(initial_value, reviewed_value)
        if reason:
            conflict = {
                "panel_id": panel_id,
                "reason": reason,
                "initial_value": str(initial_value),
                "reviewed_value": str(reviewed_value),
            }
            document["parallel_review"].setdefault("conflicts", []).append(conflict)
            return conflict
        return None

    document["fallback"] = copy.deepcopy(fallback)
    cloud_timing = reviewed.get("timing", {}).get("monotonic_durations_ms", {})
    document.setdefault("timing", {}).setdefault("monotonic_durations_ms", {}).update(cloud_timing)
    states = []
    if reviewed.get("panel_regions"):
        current = {r["panel_id"]: r for r in document.get("panel_regions", [])}
        merged = []
        for cloud_region in reviewed["panel_regions"]:
            initial = current.get(cloud_region["panel_id"])
            region = copy.deepcopy(initial or cloud_region)
            if initial is None:
                region.update(lines=[], display_state=None)
            attempt = cloud_region.get("fallback", {})
            region["fallback"] = copy.deepcopy(attempt)
            if attempt.get("status") != "completed":
                states.append("failed")
            elif initial and (
                initial.get("measurement_name") != cloud_region.get("measurement_name")
                or initial.get("display_state_rejection")
                or (initial.get("local_ocr") or {}).get("display_state_rejection")
            ):
                states.append("conflict")
            elif any(
                line.get("quality_issue")
                or line.get("quality_issues")
                or line.get("label") not in (None, region.get("measurement_name"))
                for line in cloud_region.get("lines", [])
            ):
                states.append("conflict")
            elif conflict := conflict_for(cloud_region["panel_id"]):
                region["review_conflict"] = conflict
                states.append("conflict")
            else:
                region["lines"] = copy.deepcopy(cloud_region.get("lines", []))
                region["display_state"] = copy.deepcopy(cloud_region.get("display_state"))
                region.pop("display_state_rejection", None)
                states.append(
                    "completed" if region["lines"] or region["display_state"] else "unreadable"
                )
            merged.append(region)
        document["panel_regions"] = merged
        document["lines"] = [line for region in merged for line in region.get("lines", [])]
        if not current:
            # These facts come exclusively from shared localization and the task's
            # capture-time bindings, never from the cloud's transcribed identity.
            for key in (
                "instrument",
                "binding_id",
                "binding_ids",
                "instrument_candidates",
                "instrument_association",
                "instrument_identity_basis",
                "association_status",
                "workbench",
                "workbenches",
                "panel_detection",
                "panel_selection",
            ):
                if key in reviewed:
                    document[key] = copy.deepcopy(reviewed[key])
    elif fallback.get("status") == "completed":
        if conflict_for(None) or any(
            line.get("quality_issue") or line.get("quality_issues")
            for line in reviewed.get("lines", [])
        ):
            states.append("conflict")
        else:
            for key in (
                "lines",
                "model",
                "device",
                "actual_model_invocation",
                "instrument",
                "binding_id",
                "binding_ids",
                "instrument_candidates",
                "panel_regions",
                "instrument_association",
                "instrument_identity_basis",
                "association_status",
                "outcome",
                "vision_display_rules",
            ):
                if key in reviewed:
                    document[key] = copy.deepcopy(reviewed[key])
            states.append("completed" if document.get("lines") else "unreadable")
    else:
        states.append("failed")
    if not local_ready and not document.get("local_ocr"):
        document["local_ocr"] = {"status": "running", "lines": []}
    status = next((s for s in ("conflict", "failed", "unreadable") if s in states), "completed")
    document["parallel_review"].update(
        status=status, finished_at=reviewed.get("fallback", {}).get("finished_at")
    )
    accepted = any(s in {"completed", "unreadable"} for s in states)
    if accepted:
        document["parallel_review"]["result"] = (
            "consistent" if before == values(document) else "updated"
        )
        for key in ("recognition_skipped", "skip_reason", "failure_reason", "error"):
            document.pop(key, None)
        document["outcome"] = (
            "numeric_readout"
            if build_readings(document)
            else "display_state"
            if any(r.get("display_state") for r in document.get("panel_regions", []))
            else "no_numeric_readout"
        )
    document["actual_model_invocation"] = bool(
        document.get("actual_model_invocation")
        or fallback.get("actual_model_invocation")
        or any(r.get("actual_model_invocation") for r in fallback.get("regions", []))
    )
    return accepted


def run(core, document, image, panel, x, y, *, pipeline, valid, clock, pause, started):
    from field_recognition.recognition.panel_readout import submit_local

    config = pipeline.recognition or RecognitionConfig.from_environment()
    # A single immutable config snapshot is used by both threads. The background
    # reader never saves documents or touches the owner's task lease.
    local_pipeline = replace(pipeline, recognition=replace(config, enabled=False))
    cloud_pipeline = replace(
        pipeline, save_document=lambda _: None, recognition=replace(config, photo_mode="primary")
    )
    snapshots = document.get("all_binding_snapshots", document.get("binding_snapshots", []))
    template = (
        core["predict_readout"](
            panel,
            x,
            y,
            binding_snapshots=snapshots,
            field_rules=document.get("field_rules"),
            localization_only=True,
        )
        if snapshots
        else {"panel_regions": []}
    )
    document.update(
        reading_mode="parallel_photo",
        phase="parallel_reading",
        result_available=document.get("result_available", False),
        result_revision=document.get("result_revision", 0),
        parallel_review={"status": "running", "started_at": core["now"]()},
    )
    pipeline.save_document(document)
    local_future = submit_local(
        core, document, panel, x, y, clock=clock, detection=template.get("panel_detection")
    )
    cloud_future = core["vision_pool"].submit(
        _cloud,
        core,
        copy.deepcopy(document),
        template,
        image,
        panel,
        cloud_pipeline,
        valid,
        time.monotonic(),
    )
    local_ready = False
    reviewed = None
    while valid():
        if local_future.done() and not local_ready:
            local_ready = True
            try:
                local = local_future.result()
            except Exception as exc:
                local = {"status": "failed", "lines": [], "error": type(exc).__name__}
            _project(core, document, local, image, local_pipeline, valid, clock, started)
            document.update(
                status="running",
                phase="review_pending",
                result_available=bool(
                    document.get("lines")
                    or any(r.get("display_state") for r in document.get("panel_regions", []))
                ),
                result_stage="preliminary",
                result_updated_at=core["now"](),
            )
            if document["result_available"]:
                document.setdefault("first_result_at", document["result_updated_at"])
                document["result_revision"] += 1
            document["readings"] = build_readings(document)
            document["measurement_records"] = build_records(document)
            document["display_fields"] = display_fields(document)
            pipeline.save_document(document)
        if cloud_future.done() and reviewed is None:
            reviewed = cloud_future.result()
            accepted = _merge(document, reviewed, local_ready)
            if accepted or local_ready:
                document.update(
                    status="failed"
                    if not accepted and document.get("local_ocr", {}).get("status") == "failed"
                    else "completed",
                    phase="completed",
                    result_available=bool(
                        document.get("lines")
                        or any(r.get("display_state") for r in document.get("panel_regions", []))
                    ),
                    result_stage="reviewed" if accepted else "preliminary",
                    result_updated_at=core["now"](),
                )
                document.setdefault("first_result_at", document["result_updated_at"])
                document["result_revision"] += 1
                if document["status"] == "completed":
                    document.pop("error", None)
                return local_future
            pipeline.save_document(document)
        if reviewed is not None and local_ready:
            # Cloud failed before local OCR finished. Preserve the late local
            # answer as the current initial reading, with the failure still visible.
            document.update(
                status="failed"
                if document.get("local_ocr", {}).get("status") == "failed"
                else "completed",
                phase="completed",
                result_stage="preliminary",
            )
            return local_future
        if reviewed is not None and clock() - started >= 90:
            document.update(status="failed", phase="completed", error="local_ocr_timeout")
            return local_future
        pause(0.02)
    document.update(
        status="cancelled",
        phase="completed",
        error="binding_ended_or_changed",
        lines=[],
        result_available=False,
    )
    if not cloud_future.cancel():
        # An in-flight paid request cannot be cancelled safely. Keep its raw
        # receipt before the owner publishes cancellation/interruption; do not
        # let that answer restore a released binding or a cancelled reading.
        reviewed = cloud_future.result()
        document["fallback"] = copy.deepcopy(reviewed.get("fallback", {}))
    document["parallel_review"].update(status="cancelled", finished_at=core["now"]())
    return local_future
