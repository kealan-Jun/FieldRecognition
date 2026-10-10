#!/usr/bin/env python3
"""Score explicit local references against normalized replay JSON; never run OCR.

References use stirrer-reference/1, reference_kind (assistant_visual_review or
human_ground_truth), and samples. Each sample has sample_id (01, 02, ...),
image_sha256, target_instrument_id, scope (target or context_only), and fields
temperature/speed. Reference states are numeric, off, unreadable, not_visible;
numeric requires value, and unit is optional. Human references additionally
require top-level human_verified=true and a nonempty reviewer.

Predictions use stirrer-predictions/1, run_id, and samples from normalize_job().
Their field states are numeric, off, unrecognized. Every hash is unique within
each input. Missing predictions remain failures in eligible denominators.
"""

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re


FIELDS = {"temperature": "温度", "speed": "转速"}
REFERENCE_STATES = {"numeric", "off", "unreadable", "not_visible"}
PREDICTION_STATES = {"numeric", "off", "unrecognized"}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("numeric value must be a finite number or numeric string")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid numeric value") from exc
    if not result.is_finite():
        raise ValueError("numeric value must be finite")
    return result


def image_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("image_sha256 must be a lowercase SHA-256 digest")
    return value


def validate_field(field, states):
    if not isinstance(field, dict) or field.get("state") not in states:
        raise ValueError("invalid field state")
    if field["state"] == "numeric":
        number(field.get("value"))
    elif field.get("value") is not None:
        raise ValueError("only numeric fields may have a value; OFF is not zero")
    for key in ("unit", "unit_basis"):
        if field.get(key) is not None and not isinstance(field[key], str):
            raise ValueError(f"{key} must be a string or null")


def normalize_job(job, instrument_id):
    """Use formal target display fields, never binding/context or raw candidates."""
    result = {
        "image_sha256": image_hash(job.get("image_sha256")),
        "job_id": job.get("job_id"),
        "pipeline_status": job.get("status"),
        "target_instrument_id": instrument_id,
        "fields": {},
    }
    for field, name in FIELDS.items():
        output = {"state": "unrecognized", "value": None, "unit": None, "unit_basis": None}
        matches = [
            item
            for item in job.get("display_fields", [])
            if isinstance(item, dict)
            and item.get("instrument_id") == instrument_id
            and item.get("name") in {name, field}
        ]
        if job.get("status") != "completed":
            output["reason"] = "pipeline_not_completed"
        elif len(matches) != 1:
            output["reason"] = "missing_field" if not matches else "ambiguous_field_outputs"
        else:
            item = matches[0]
            output.update(unit=item.get("unit"), unit_basis=item.get("unit_basis"))
            value, state = item.get("value"), item.get("display_state")
            if state == "OFF" and value is None:
                output["state"] = "off"
            elif state is None and value is not None:
                try:
                    number(value)
                except ValueError:
                    output["reason"] = "invalid_numeric_output"
                else:
                    output.update(state="numeric", value=str(value))
            else:
                output["reason"] = "unreadable_or_conflicting_output"
        result["fields"][field] = output
    return result


def index_samples(document, reference):
    samples = document.get("samples")
    if not isinstance(samples, list) or (reference and not samples):
        raise ValueError("samples must be a list; references must be nonempty")
    indexed = {}
    target = None
    for position, sample in enumerate(samples, 1):
        if not isinstance(sample, dict):
            raise ValueError("sample must be an object")
        digest = image_hash(sample.get("image_sha256"))
        if digest in indexed:
            raise ValueError(
                f"duplicate image_sha256 in {'references' if reference else 'predictions'}"
            )
        fields = sample.get("fields")
        if not isinstance(fields, dict) or set(fields) != set(FIELDS):
            raise ValueError("each sample needs exactly temperature and speed fields")
        for field in fields.values():
            validate_field(field, REFERENCE_STATES if reference else PREDICTION_STATES)
        if reference:
            if sample.get("sample_id") != f"{position:02d}":
                raise ValueError("reference sample_id must follow 01, 02, ... input order")
            if sample.get("scope") not in {"target", "context_only"}:
                raise ValueError("reference scope must be target or context_only")
            identity = sample.get("target_instrument_id")
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError("target_instrument_id is required")
            if target is not None and identity != target:
                raise ValueError("references must share one target_instrument_id")
            target = identity
        indexed[digest] = sample
    return indexed, target


def counts():
    return {"eligible": 0, "correct": 0, "misread": 0, "unrecognized": 0}


def ratio(correct, eligible):
    return correct / eligible if eligible else None


def evaluate(references, predictions):
    if not isinstance(references, dict) or not isinstance(predictions, dict):
        raise ValueError("reference and prediction documents must be objects")
    if references.get("schema") != "stirrer-reference/1":
        raise ValueError("expected stirrer-reference/1")
    kind = references.get("reference_kind")
    if kind not in {"assistant_visual_review", "human_ground_truth"}:
        raise ValueError("invalid reference_kind")
    if kind == "human_ground_truth" and (
        references.get("human_verified") is not True
        or not isinstance(references.get("reviewer"), str)
        or not references["reviewer"].strip()
    ):
        raise ValueError("human_ground_truth requires explicit human_verified=true and reviewer")
    if predictions.get("schema") != "stirrer-predictions/1":
        raise ValueError("expected stirrer-predictions/1")
    if not isinstance(predictions.get("run_id"), str) or not predictions["run_id"].strip():
        raise ValueError("run_id is required")
    refs, target = index_samples(references, True)
    preds, _ = index_samples(predictions, False)
    if set(preds) - set(refs):
        raise ValueError("prediction image_sha256 absent from references")
    if any(p.get("target_instrument_id", target) != target for p in preds.values()):
        raise ValueError("prediction target_instrument_id differs from reference target")

    metrics = {
        field: {
            "numeric": counts(),
            "off": counts(),
            "excluded": dict.fromkeys(("unreadable", "not_visible", "context_only"), 0),
            "coverage": {"recognized": 0, "eligible": 0},
            "output_unit_agreement": {"correct": 0, "eligible": 0, "basis_counts": Counter()},
        }
        for field in FIELDS
    }
    complete = {"eligible": 0, "correct": 0, "recognized": 0}
    observations = []
    missing = {"state": "unrecognized", "value": None, "unit": None, "unit_basis": None}
    for digest, reference in refs.items():
        prediction = preds.get(digest)
        outcomes = {}
        predicted = prediction["fields"] if prediction else dict.fromkeys(FIELDS, missing)
        for field in FIELDS:
            truth, actual, metric = reference["fields"][field], predicted[field], metrics[field]
            if reference["scope"] == "context_only" or truth["state"] not in {"numeric", "off"}:
                reason = "context_only" if reference["scope"] == "context_only" else truth["state"]
                metric["excluded"][reason] += 1
                outcomes[field] = f"excluded_{reason}"
                continue
            count = metric[truth["state"]]
            count["eligible"] += 1
            metric["coverage"]["eligible"] += 1
            if actual["state"] == "unrecognized":
                outcome = "unrecognized"
            else:
                metric["coverage"]["recognized"] += 1
                equal = actual["state"] == truth["state"] and (
                    truth["state"] == "off" or number(actual["value"]) == number(truth["value"])
                )
                outcome = "correct" if equal else "misread"
            count[outcome] += 1
            outcomes[field] = outcome
            if truth["state"] == "numeric" and truth.get("unit"):
                units = metric["output_unit_agreement"]
                units["eligible"] += 1
                units["correct"] += actual.get("unit") == truth["unit"]
                units["basis_counts"][actual.get("unit_basis") or "unknown"] += 1
        both = all(outcomes[f] in {"correct", "misread", "unrecognized"} for f in FIELDS)
        if both:
            complete["eligible"] += 1
            complete["correct"] += all(outcomes[f] == "correct" for f in FIELDS)
            complete["recognized"] += all(predicted[f]["state"] != "unrecognized" for f in FIELDS)
        truth_fields = reference["fields"]
        swap = (
            both
            and all(truth_fields[f]["state"] == predicted[f]["state"] == "numeric" for f in FIELDS)
            and number(truth_fields["temperature"]["value"])
            != number(truth_fields["speed"]["value"])
            and number(predicted["temperature"]["value"]) == number(truth_fields["speed"]["value"])
            and number(predicted["speed"]["value"]) == number(truth_fields["temperature"]["value"])
        )
        observations.append(
            {
                "sample_id": reference["sample_id"],
                "image_sha256": digest,
                "job_id": prediction.get("job_id") if prediction else None,
                "missing_prediction": prediction is None,
                "field_outcomes": outcomes,
                "possible_field_misassignment": swap,
            }
        )
    total_correct = total_eligible = 0
    for metric in metrics.values():
        for state in ("numeric", "off"):
            count = metric[state]
            count["reference_agreement"] = ratio(count["correct"], count["eligible"])
            total_correct += count["correct"]
            total_eligible += count["eligible"]
        coverage = metric["coverage"]
        coverage["ratio"] = ratio(coverage["recognized"], coverage["eligible"])
        units = metric["output_unit_agreement"]
        units["reference_agreement"] = ratio(units["correct"], units["eligible"])
        units["basis_counts"] = dict(units["basis_counts"])
    complete["reference_agreement"] = ratio(complete["correct"], complete["eligible"])
    complete["coverage"] = ratio(complete["recognized"], complete["eligible"])
    return {
        "schema": "stirrer-evaluation/1",
        "reference_kind": kind,
        "evidence_level": "PARTIAL_EVIDENCE",
        "independent_test_set": "NOT_PROVEN",
        "run_id": predictions["run_id"],
        "target_instrument_id": target,
        "image_count": len(refs),
        "missing_prediction_count": len(set(refs) - set(preds)),
        "reference_agreement": {
            "correct": total_correct,
            "eligible": total_eligible,
            "ratio": ratio(total_correct, total_eligible),
        },
        "fields": metrics,
        "both_eligible_complete": complete,
        "possible_field_misassignment_count": sum(
            o["possible_field_misassignment"] for o in observations
        ),
        "observations": observations,
        "notes": [
            "Assistant visual review is not human ground truth; agreement is not proven instrument accuracy.",
            "Unit agreement concerns output units; registry units do not prove OCR unit recognition.",
            "Crossed distinct numeric values flag possible field misassignment, not confirmed spatial misassignment.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("references", type=Path, help="Explicit local reference JSON")
    parser.add_argument("predictions", type=Path, help="Explicit local normalized prediction JSON")
    args = parser.parse_args()
    try:
        report = evaluate(
            json.loads(args.references.read_text()), json.loads(args.predictions.read_text())
        )
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"evaluation rejected: {exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
