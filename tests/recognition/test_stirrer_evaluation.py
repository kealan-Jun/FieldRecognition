import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.evaluate_stirrer import evaluate, normalize_job


def field(state="numeric", value="63", unit="°C", **kwargs):
    return {"state": state, "value": value if state == "numeric" else None, "unit": unit, **kwargs}


def inputs():
    references = {
        "schema": "stirrer-reference/1",
        "reference_kind": "assistant_visual_review",
        "samples": [
            {
                "sample_id": "01",
                "image_sha256": "a" * 64,
                "target_instrument_id": "stirrer",
                "scope": "target",
                "fields": {"temperature": field(), "speed": field(value="1140", unit="rpm")},
            }
        ],
    }
    predictions = {
        "schema": "stirrer-predictions/1",
        "run_id": "offline-fixture",
        "samples": [
            {
                "image_sha256": "a" * 64,
                "job_id": "job-one",
                "fields": {
                    "temperature": field(value="63.0"),
                    "speed": field(value=1140, unit="rpm"),
                },
            }
        ],
    }
    return references, predictions


def test_exact_decimal_agreement_does_not_upgrade_assistant_reference():
    references, predictions = inputs()
    before = copy.deepcopy((references, predictions))
    report = evaluate(references, predictions)
    assert report["reference_agreement"] == {"correct": 2, "eligible": 2, "ratio": 1.0}
    assert report["both_eligible_complete"]["correct"] == 1
    assert report["reference_kind"] == "assistant_visual_review"
    assert report["evidence_level"] == "PARTIAL_EVIDENCE"
    assert report["independent_test_set"] == "NOT_PROVEN"
    assert (references, predictions) == before


def test_off_is_not_zero_and_missing_prediction_stays_in_denominator():
    references, predictions = inputs()
    references["samples"][0]["fields"]["temperature"] = field("off")
    predictions["samples"][0]["fields"]["temperature"] = field(value="0")
    report = evaluate(references, predictions)
    assert report["fields"]["temperature"]["off"]["misread"] == 1
    assert report["fields"]["temperature"]["coverage"]["ratio"] == 1
    predictions["samples"] = []
    report = evaluate(references, predictions)
    assert report["reference_agreement"] == {"correct": 0, "eligible": 2, "ratio": 0.0}
    assert report["fields"]["temperature"]["off"]["unrecognized"] == 1
    assert report["fields"]["speed"]["numeric"]["unrecognized"] == 1
    assert report["both_eligible_complete"]["eligible"] == 1
    assert report["both_eligible_complete"]["coverage"] == 0
    assert report["missing_prediction_count"] == 1


def test_attested_human_reference_can_score_numeric_zero_and_off_correctly():
    references, predictions = inputs()
    references.update(
        reference_kind="human_ground_truth", human_verified=True, reviewer="human-reviewer"
    )
    for document in (references, predictions):
        document["samples"][0]["fields"] = {
            "temperature": field(value="0"),
            "speed": field("off", unit="rpm"),
        }
    report = evaluate(references, predictions)
    assert report["reference_kind"] == "human_ground_truth"
    assert report["fields"]["temperature"]["numeric"]["correct"] == 1
    assert report["fields"]["speed"]["off"]["correct"] == 1
    assert report["both_eligible_complete"]["correct"] == 1
    assert report["fields"]["speed"]["output_unit_agreement"]["eligible"] == 0
    assert report["independent_test_set"] == "NOT_PROVEN"


def test_no_readable_references_reports_null_rates_instead_of_success():
    references, predictions = inputs()
    references["samples"][0]["fields"] = {
        "temperature": field("unreadable"),
        "speed": field("not_visible"),
    }
    report = evaluate(references, predictions)
    assert report["reference_agreement"] == {"correct": 0, "eligible": 0, "ratio": None}
    assert report["both_eligible_complete"]["reference_agreement"] is None
    assert report["fields"]["speed"]["excluded"]["not_visible"] == 1


def test_unreadable_excludes_only_that_field_and_context_excludes_both():
    references, predictions = inputs()
    references["samples"][0]["fields"]["temperature"] = field("unreadable")
    report = evaluate(references, predictions)
    assert report["reference_agreement"]["eligible"] == 1
    assert report["fields"]["temperature"]["excluded"]["unreadable"] == 1
    assert report["both_eligible_complete"]["eligible"] == 0
    second = copy.deepcopy(references["samples"][0])
    second.update(sample_id="02", image_sha256="b" * 64, scope="context_only")
    second["fields"]["temperature"] = field("not_visible")
    references["samples"].append(second)
    report = evaluate(references, predictions)
    assert report["reference_agreement"]["eligible"] == 1
    assert all(
        report["fields"][f]["excluded"]["context_only"] == 1 for f in ("temperature", "speed")
    )


def test_swap_is_possible_misassignment_but_remains_two_misreads():
    references, predictions = inputs()
    predictions["samples"][0]["fields"]["temperature"]["value"] = "1140"
    predictions["samples"][0]["fields"]["speed"]["value"] = "63"
    report = evaluate(references, predictions)
    assert report["possible_field_misassignment_count"] == 1
    assert all(report["fields"][f]["numeric"]["misread"] == 1 for f in ("temperature", "speed"))
    predictions["samples"][0]["fields"]["speed"]["value"] = "1140"
    assert evaluate(references, predictions)["possible_field_misassignment_count"] == 0


def test_registered_unit_agreement_is_separate_from_numeric_agreement():
    references, predictions = inputs()
    predictions["samples"][0]["fields"]["temperature"].update(
        unit="rpm", unit_basis="recognition_text"
    )
    predictions["samples"][0]["fields"]["speed"]["unit_basis"] = "instrument_registry"
    report = evaluate(references, predictions)
    assert report["reference_agreement"]["ratio"] == 1
    assert report["fields"]["temperature"]["output_unit_agreement"]["correct"] == 0
    assert report["fields"]["speed"]["output_unit_agreement"]["basis_counts"] == {
        "instrument_registry": 1
    }


@pytest.mark.parametrize("input_name", ["references", "predictions"])
def test_even_identical_duplicate_hashes_are_rejected(input_name):
    references, predictions = inputs()
    document = references if input_name == "references" else predictions
    document["samples"].append(copy.deepcopy(document["samples"][0]))
    with pytest.raises(ValueError, match="duplicate image_sha256"):
        evaluate(references, predictions)


def test_extra_prediction_hash_and_different_target_are_rejected():
    references, predictions = inputs()
    predictions["samples"][0]["image_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="absent from references"):
        evaluate(references, predictions)
    predictions["samples"][0].update(image_sha256="a" * 64, target_instrument_id="other")
    with pytest.raises(ValueError, match="differs"):
        evaluate(references, predictions)


@pytest.mark.parametrize(
    "verified,reviewer", [(False, "reviewer"), (True, ""), (1, "reviewer"), (True, None)]
)
def test_human_reference_requires_explicit_attestation(verified, reviewer):
    references, predictions = inputs()
    references.update(
        reference_kind="human_ground_truth", human_verified=verified, reviewer=reviewer
    )
    with pytest.raises(ValueError, match="human_ground_truth requires"):
        evaluate(references, predictions)


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "OFF", None])
def test_invalid_numeric_reference_is_rejected(value):
    references, predictions = inputs()
    references["samples"][0]["fields"]["temperature"]["value"] = value
    with pytest.raises(ValueError, match="numeric"):
        evaluate(references, predictions)


def test_normalizer_uses_target_formal_fields_and_never_context_or_raw_candidates():
    job = {
        "job_id": "target-job",
        "image_sha256": "a" * 64,
        "status": "completed",
        "allowed_instrument_ids": ["stirrer"],
        "readings": [{"instrument": {"id": "stirrer"}, "measurement_name": "温度", "value": "63"}],
        "display_fields": [
            {"instrument_id": "other", "name": "温度", "value": 63},
            {"instrument_id": "stirrer", "name": "转速", "value": None, "display_state": "OFF"},
        ],
    }
    result = normalize_job(job, "stirrer")
    assert result["fields"]["temperature"]["state"] == "unrecognized"
    assert result["fields"]["speed"]["state"] == "off"
    job["display_fields"].append({"instrument_id": "stirrer", "name": "temperature", "value": 0})
    assert normalize_job(job, "stirrer")["fields"]["temperature"]["value"] == "0"
    job["display_fields"].append({"instrument_id": "stirrer", "name": "温度", "value": 0})
    assert (
        normalize_job(job, "stirrer")["fields"]["temperature"]["reason"]
        == "ambiguous_field_outputs"
    )
    job["status"] = "failed"
    assert all(
        f["state"] == "unrecognized" for f in normalize_job(job, "stirrer")["fields"].values()
    )


def test_cli_reads_only_explicit_json_and_emits_reproducible_report(tmp_path):
    references, predictions = inputs()
    paths = [tmp_path / "references.json", tmp_path / "predictions.json"]
    for path, document in zip(paths, (references, predictions)):
        path.write_text(json.dumps(document))
    script = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_stirrer.py"
    result = subprocess.run(
        [sys.executable, str(script), *(str(p) for p in paths)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == evaluate(references, predictions)
