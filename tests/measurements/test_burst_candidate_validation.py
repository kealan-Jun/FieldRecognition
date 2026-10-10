import copy
import json

import pytest

from tests.support.demo import app_client  # noqa: F401
from tests.support.photo_measurements import setup, enqueue, complete, get_draft  # noqa: F401
from field_recognition.recognition.panel_readout import save
from field_recognition.measurements.photo_measurements import summarize_candidates, RULE_VERSION


def finish(setup, index, text="51", *, invalid=False, fallback=False):
    app = setup[0]
    job = complete(setup, index, text, publish=False)
    if invalid or fallback:
        bad = copy.deepcopy(job["readings"][0])
        bad.update(value="951", text="951", quality_issue="digit_sequence_conflict")
        job["readings"] = [bad] + (job["readings"] if fallback else [])
        job["lines"] = [
            {"text": "951", "value": "951", "quality_issue": "digit_sequence_conflict"}
        ] + (job["lines"] if fallback else [])
    save(vars(app), job)


@pytest.mark.parametrize(
    "invalid", [(False, True, True), (True, False, True), (True, True, False), (False, False, True)]
)
def test_one_or_two_bad_photos_do_not_veto_usable_reading(setup, invalid):
    _, client, _, _ = setup
    mid = enqueue(setup)[0]["measurement_id"]
    for i, bad in enumerate(invalid):
        finish(setup, i, invalid=bad)
    draft = get_draft(client, mid)
    field = draft["fields"][0]
    assert draft["status"] == "draft" and draft["blockers"] == []
    assert field["value"] == 51 and not field["corrected"]
    assert field["agreement"]["matching_photos"] == 3 - sum(invalid)
    assert len(field["original_candidates"]) == 3
    assert len(field["excluded_candidates"]) == sum(invalid)
    assert field["recognition_warnings"] and field["validation_rules"] == RULE_VERSION
    assert client.get("/api/experiment-records").json()["items"] == []


def test_rejected_vision_alternatives_retained_without_blocking_three_clean_candidates(setup):
    _, client, _, _ = setup
    mid = enqueue(setup)[0]["measurement_id"]
    for i in range(3):
        finish(setup, i, fallback=i != 1)
    draft = get_draft(client, mid)
    field = draft["fields"][0]
    assert draft["blockers"] == [] and field["value"] == 51
    assert field["agreement"]["matching_photos"] == 3
    assert len(field["original_candidates"]) == 5
    assert len(field["excluded_candidates"]) == 2
    assert draft["sources"][0]["raw_lines"][0]["text"] == "951"


@pytest.mark.parametrize(
    "values,invalid", [(["51", "52", "51"], [False, False, True]), (["51"] * 3, [True] * 3)]
)
def test_conflicting_usable_or_all_bad_results_still_block(setup, values, invalid):
    _, client, _, _ = setup
    mid = enqueue(setup)[0]["measurement_id"]
    for i, (text, bad) in enumerate(zip(values, invalid)):
        finish(setup, i, text, invalid=bad)
    draft = get_draft(client, mid)
    assert draft["blockers"] and draft["fields"][0]["value"] is None
    assert (
        client.post(
            f"/api/photo-measurements/{mid}/confirm",
            json={"actor": "Reader", "revision": draft["revision"]},
        ).status_code
        == 409
    )


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"qr_hash": None}, "二维码"),
        ({"qr_conflict": True}, "不一致"),
        ({"binding_id": "other-session"}, "使用时段"),
    ],
)
def test_bad_candidate_never_masks_identity_or_session_conflict(setup, change, expected):
    _, client, _, _ = setup
    mid = enqueue(setup)[0]["measurement_id"]
    for i in range(3):
        finish(setup, i, invalid=i != 0)
    field = get_draft(client, mid)["fields"][0]
    field["original_candidates"][1].update(change)
    original = copy.deepcopy(field["original_candidates"])
    summarize_candidates(field)
    assert any(expected in issue for issue in field["recognition_issues"])
    assert field["original_candidates"] == original


def test_revalidation_is_audited_revision_checked_and_never_confirms(setup):
    app, client, _, _ = setup
    mid = enqueue(setup)[0]["measurement_id"]
    for i in range(3):
        finish(setup, i, fallback=i != 1)
    prior = get_draft(client, mid)
    prior["fields"][0]["recognition_issues"] = ["存在不可读或格式异常的原始结果"]
    prior["fields"][0].pop("validation_rules")
    prior["blockers"] = ["温度：存在不可读或格式异常的原始结果"]
    with app.db() as conn:
        conn.execute(
            "UPDATE photo_measurements SET document=? WHERE id=?", (json.dumps(prior), mid)
        )
    decision = {"actor": "Rule validation", "revision": prior["revision"]}
    response = client.post(f"/api/photo-measurements/{mid}/revalidate", json=decision)
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["revision"] == prior["revision"] + 1 and updated["blockers"] == []
    assert updated["status"] == "draft" and not updated["fields"][0]["corrected"]
    assert updated["sources"] == prior["sources"] and updated["corrections"] == prior["corrections"]
    assert updated["fields"][0]["original_candidates"] == prior["fields"][0]["original_candidates"]
    assert updated["validation_history"][-1]["before_fields"] == prior["fields"]
    assert updated["validation_history"][-1]["rules"] == RULE_VERSION
    assert (
        client.post(f"/api/photo-measurements/{mid}/revalidate", json=decision).status_code == 409
    )
    assert client.get("/api/experiment-records").json()["items"] == []
    assert (
        client.post(
            f"/api/photo-measurements/{mid}/confirm",
            json={"actor": "Reader", "revision": updated["revision"]},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/photo-measurements/{mid}/revalidate",
            json={"actor": "Rule validation", "revision": get_draft(client, mid)["revision"]},
        ).status_code
        == 409
    )


def test_revalidation_preserves_audited_manual_correction(setup):
    _, client, binding, _ = setup
    mid = enqueue(setup)[0]["measurement_id"]
    for i in range(3):
        finish(setup, i)
    draft = get_draft(client, mid)
    fixed = client.post(
        f"/api/photo-measurements/{mid}/revisions",
        json={
            "actor": "Reader",
            "revision": draft["revision"],
            "reason": "Reviewed actual value",
            "fields": [
                {
                    "field_id": draft["fields"][0]["field_id"],
                    "instrument_id": binding["instrument"]["id"],
                    "name": "温度",
                    "value": 50,
                    "unit": "°C",
                }
            ],
        },
    ).json()
    response = client.post(
        f"/api/photo-measurements/{mid}/revalidate",
        json={"actor": "Rule validation", "revision": fixed["revision"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["fields"] == fixed["fields"]
    assert response.json()["corrections"] == fixed["corrections"]


@pytest.mark.parametrize("role,device", [("operator", False), ("admin", True)])
def test_revalidation_denies_unprivileged_and_device_callers(setup, monkeypatch, role, device):
    import field_recognition.api.security as security

    _, client, _, _ = setup
    mid = enqueue(setup)[0]["measurement_id"]
    for i in range(3):
        finish(setup, i)
    draft = get_draft(client, mid)
    monkeypatch.setattr(
        security,
        "current",
        lambda: security.Principal("test-user", "User", role, frozenset(["TestCamera"]), device),
    )
    response = client.post(
        f"/api/photo-measurements/{mid}/revalidate",
        json={"actor": "spoofed admin", "revision": draft["revision"]},
    )
    assert response.status_code == 403
    assert get_draft(client, mid)["revision"] == draft["revision"]
