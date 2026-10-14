from tests.support.configuration import refresh_runtime_config
import json

import numpy as np
import pytest

import field_recognition.recognition.aliyun_vision as vision
import field_recognition.recognition.panel_readout as panel_readout
from tests.support.aliyun_vision import response
from tests.support.demo import app_client  # noqa: F401
from tests.support.panel_readout import Clock, cloud_result, refresh_recognition_config
from tests.support.unbound_readout import photo


@pytest.fixture
def configured(monkeypatch, request):
    monkeypatch.setenv("FIELD_VISION_PROVIDER", "nianfeng")
    monkeypatch.setenv("FIELD_VISION_ENABLED", "1")
    monkeypatch.setenv("FIELD_VISION_API_KEY", "test-nianfeng-secret")
    monkeypatch.setenv("FIELD_VISION_MODEL", "claude-opus-5")
    monkeypatch.setenv("FIELD_VISION_PHOTO_MODE", "primary")
    if "app_client" in request.fixturenames:
        app, _ = request.getfixturevalue("app_client")
        refresh_recognition_config(app)


@pytest.mark.parametrize("aliyun_thinking", ["0", "1"])
def test_nianfeng_uses_exact_authorized_endpoint_and_common_payload(
    configured, monkeypatch, aliyun_thinking
):
    monkeypatch.setenv("FIELD_ALIYUN_ENABLE_THINKING", aliyun_thinking)
    sent = []

    def request(payload, key, *, config=None):
        assert key == "test-nianfeng-secret"
        assert vision.base_url() == "https://api.nianfeng.tech/v1"
        sent.append(payload)
        return response(
            {
                "status": "readable",
                "readings": [{"text": "14.4062", "value": "14.4062", "unit": "g"}],
            }
        )

    monkeypatch.setattr(vision, "_request", request)
    result = vision.read_panel(np.zeros((40, 50, 3), np.uint8))
    assert result["provider"] == "nianfeng"
    assert result["answer"]["readings"][0]["value"] == "14.4062"
    assert sent[0]["model"] == "claude-opus-5"
    assert sent[0]["max_tokens"] == 4096
    assert "enable_thinking" not in sent[0] and "thinking_budget" not in sent[0]
    assert "test-nianfeng-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.nianfeng.tech/v1",
        "https://api.nianfeng.tech.evil.invalid/v1",
        "https://api.nianfeng.tech/wallet",
        "https://api.nianfeng.tech/v1?secret=x",
        "https://user@api.nianfeng.tech/v1",
    ],
)
def test_nianfeng_rejects_wrong_destinations_without_sending(configured, monkeypatch, endpoint):
    monkeypatch.setenv("FIELD_VISION_BASE_URL", endpoint)
    monkeypatch.setattr(vision, "_request", lambda *a, **kwargs: pytest.fail("Unexpected network"))
    assert (
        vision.read_panel(np.zeros((10, 10, 3), np.uint8))["error"] == "invalid_nianfeng_endpoint"
    )


def test_nianfeng_does_not_borrow_dashscope_secret_or_invent_model(configured, monkeypatch):
    monkeypatch.delenv("FIELD_VISION_API_KEY")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "unrelated-key")
    assert not vision.public_config()["available"]
    monkeypatch.setenv("FIELD_VISION_API_KEY", "test-nianfeng-secret")
    monkeypatch.delenv("FIELD_VISION_MODEL")
    assert not vision.public_config()["available"]


def test_invalid_model_answer_preserves_raw_evidence(configured, monkeypatch):
    monkeypatch.setattr(
        vision,
        "_request",
        lambda *a, **kwargs: response(
            {"status": "readable", "readings": [{"text": "144062", "value": "14.4062"}]}
        ),
    )
    result = vision.read_panel(np.zeros((20, 20, 3), np.uint8))
    assert result["error"] == "invalid_answer"
    assert "144062" in result["raw_answer"]


def unbound_job(app, client, monkeypatch, ident="primary-photo"):
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "camera_id")
    monkeypatch.setattr(app.readout_pool, "submit", lambda *a, **kwargs: None)
    result = client.post("/api/ocr/photo-result", json={"photo": photo(app, ident=ident)})
    assert result.status_code == 202
    return result.json()


def test_primary_unbound_skips_ocr_and_does_not_invent_binding(app_client, configured, monkeypatch):
    app, client = app_client
    job = unbound_job(app, client, monkeypatch)
    monkeypatch.setattr(
        app.ocr_pool,
        "submit",
        lambda *a, **kwargs: pytest.fail("Primary photo must not wait for OCR"),
    )
    monkeypatch.setattr(
        app, "reserve_fallback", lambda *a, **kwargs: pytest.fail("Primary photo has no cooldown")
    )
    calls = []

    def read(panel, *, config):
        calls.append(panel.shape)
        return cloud_result()

    monkeypatch.setattr(vision, "read_panel", read)
    clock = Clock()
    panel_readout.run(vars(app), job, clock=clock, pause=clock.pause)
    result = app.get_job(job["job_id"])
    assert result["status"] == "completed" and result["reading_mode"] == "vision_primary_photo"
    assert result["lines"][0]["value"] == "12.34"
    assert result["instrument"] is result["binding_id"] is None
    assert result["readings"][0]["human_verified"] is False
    assert result["readings"][0]["instrument"] is None
    assert result["local_ocr"]["status"] == "skipped"
    assert result["fallback"]["answer"]["readings"][0]["value"] == "12.34"
    assert len(calls) == 1 and clock.value == 0


def test_primary_each_burst_photo_is_processed_despite_existing_cooldown(
    app_client, configured, monkeypatch
):
    app, client = app_client
    monkeypatch.setattr(app, "reserve_fallback", lambda *a, **kwargs: False)
    calls = []
    monkeypatch.setattr(
        vision, "read_panel", lambda panel, **kwargs: calls.append(1) or cloud_result()
    )
    for index in range(3):
        job = unbound_job(app, client, monkeypatch, ident=f"primary-burst-{index}")
        app.run_ocr(job)
        assert app.get_job(job["job_id"])["status"] == "completed"
    assert len(calls) == 3


def test_primary_unreadable_is_empty_without_running_ocr(app_client, configured, monkeypatch):
    app, client = app_client
    job = unbound_job(app, client, monkeypatch)
    monkeypatch.setattr(
        app.ocr_pool,
        "submit",
        lambda *a, **kwargs: pytest.fail("Unreadable must not become guessed local digits"),
    )
    monkeypatch.setattr(
        vision,
        "read_panel",
        lambda panel, **kwargs: dict(
            cloud_result(), answer={"status": "unreadable", "readings": []}
        ),
    )
    app.run_ocr(job)
    result = app.get_job(job["job_id"])
    assert result["status"] == "completed" and result["lines"] == []
    assert result["outcome"] == "no_numeric_readout"


def test_primary_failure_keeps_receipt_and_falls_back_to_local_once(
    app_client, configured, monkeypatch
):
    app, client = app_client
    job = unbound_job(app, client, monkeypatch)
    calls = []
    monkeypatch.setattr(
        vision,
        "read_panel",
        lambda panel, **kwargs: calls.append(1)
        or {"status": "failed", "error": "timeout", "raw_answer": "partial"},
    )
    app.run_ocr(job)
    result = app.get_job(job["job_id"])
    assert result["status"] == "completed"
    assert (
        result["fallback"]["error"] == "timeout" and result["fallback"]["raw_answer"] == "partial"
    )
    assert len(calls) == 1 and result["local_ocr"]["actual_model_invocation"]


def display_rule():
    return {
        "instrument_type": "balance",
        "measurement_name": "质量",
        "unit": "g",
        "decimal_places": 4,
        "fixed_decimal_display": True,
        "confirmed_by_user": True,
    }


@pytest.mark.parametrize(
    "observed,expected,issue",
    [
        ("144062", "14.4062", None),
        ("14.4062", "14.4062", None),
        ("144.062", "144.062", "display_precision_mismatch"),
    ],
)
def test_user_confirmed_type_rule_preserves_raw_and_precision_conflicts(
    app_client, configured, monkeypatch, observed, expected, issue
):
    app, client = app_client
    job = unbound_job(app, client, monkeypatch)
    monkeypatch.setenv("FIELD_VISION_DISPLAY_RULES", json.dumps([display_rule()]))
    refresh_recognition_config(app)

    def read(panel, *, display_rules, config):
        assert display_rules == [display_rule()]
        answer = {
            "status": "readable",
            "readings": [
                {"text": observed + " g", "value": observed, "label": "质量", "unit": "g"}
            ],
        }
        return dict(cloud_result(), answer=answer, raw_answer=json.dumps(answer))

    monkeypatch.setattr(vision, "read_panel", read)
    app.run_ocr(job)
    result = app.get_job(job["job_id"])
    reading = result["readings"][0]
    assert reading["value"] == expected and reading["raw_value"] == observed
    assert reading["text"] == observed + " g" and reading["quality_issue"] == issue
    assert reading["instrument"] is reading["binding_id"] is None
    assert reading["human_verified"] is False and reading["status"] == "needs_review"
    assert result["fallback"]["answer"]["readings"][0]["value"] == observed
    if issue:
        assert reading["normalized_value"]["value"] is None and reading["normalization"] is None
    elif observed != expected:
        assert reading["normalization"]["basis"] == "user_confirmed_display_type"
        assert reading["normalization"]["instrument_id"] is None
        assert reading["normalized_value"]["value"] == 14.4062


def test_template_requires_explicit_confirmation_and_matching_model_type(monkeypatch):
    from field_recognition.recognition.vision_display_rules import configured_rules, apply_rule

    rule = display_rule()
    monkeypatch.setenv(
        "FIELD_VISION_DISPLAY_RULES", json.dumps([dict(rule, confirmed_by_user=False)])
    )
    assert configured_rules() == []
    for label, unit in [(None, "g"), ("质量", None), ("温度", "g"), ("质量", "kg")]:
        line = {"text": "144062", "value": "144062", "label": label, "unit": unit}
        assert apply_rule(line, [rule]) == line
    monkeypatch.setenv(
        "FIELD_VISION_DISPLAY_RULES", json.dumps([rule, dict(rule, decimal_places=3)])
    )
    assert configured_rules() == []


def test_display_rule_context_is_sent_without_binding_identity(configured, monkeypatch):
    sent = []
    monkeypatch.setattr(
        vision,
        "_request",
        lambda payload, key, **kwargs: sent.append(payload)
        or response({"status": "unreadable", "readings": []}),
    )
    result = vision.read_panel(np.zeros((20, 20, 3), np.uint8), display_rules=[display_rule()])
    prompt = sent[0]["messages"][1]["content"][1]["text"]
    assert "decimal_places" in prompt and "不要补数字" in prompt
    assert "instrument_id" not in prompt and result["display_rules"] == [display_rule()]


def test_bound_primary_reviews_even_numeric_ocr_and_uses_registered_precision(
    app_client, configured, monkeypatch
):
    from tests.support.panel_regions import configure, BOXES, B
    from tests.support.multi_readout import two_bindings

    app, client = app_client
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "camera_id")
    bindings = two_bindings(app, client)
    configure(app, monkeypatch, BOXES[1:])
    monkeypatch.setattr(app.readout_pool, "submit", lambda *a, **kwargs: None)
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app)), trigger="voice_photo_directory"
    )
    job["field_rules"][B] = {
        "质量": {"unit": "g", "decimal_places": 4, "fixed_decimal_display": True}
    }
    local = app.predict_readout(np.zeros((300, 400, 3), np.uint8), binding_snapshots=bindings)
    job["precomputed_local"] = local
    monkeypatch.setattr(
        app, "reserve_fallback", lambda *a, **kwargs: pytest.fail("Primary must not use cooldown")
    )
    calls = []

    def read(crop, *, display_rules, config):
        calls.append(display_rules)
        assert display_rules[0]["decimal_places"] == 4
        return dict(
            cloud_result(),
            answer={
                "status": "readable",
                "readings": [{"text": "144062 g", "value": "144062", "label": "质量", "unit": "g"}],
            },
        )

    monkeypatch.setattr(vision, "read_panel", read)
    clock = Clock()
    panel_readout.run(vars(app), job, clock=clock, pause=clock.pause)
    result = app.get_job(job["job_id"])
    assert result["status"] == "completed" and clock.value == 0 and len(calls) == 1
    assert result["readings"][0]["value"] == "14.4062"
    assert result["readings"][0]["instrument"]["id"] == B
    assert result["readings"][0]["normalization"]["raw_text"] == "144062 g"
    assert result["fallback"]["answer"]["readings"][0]["value"] == "144062"


@pytest.mark.parametrize("missing_detection", [True, False])
def test_primary_failed_localization_recovers_full_photo_without_identity(
    app_client, configured, monkeypatch, missing_detection
):
    from tests.support.panel_regions import configure, BOXES
    from tests.support.multi_readout import two_bindings

    app, client = app_client
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "camera_id")
    bindings = two_bindings(app, client)
    configure(app, monkeypatch, [] if missing_detection else BOXES[1:])
    monkeypatch.setattr(app.readout_pool, "submit", lambda *a, **kwargs: None)
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app)), trigger="voice_photo_directory"
    )
    local = app.predict_readout(np.zeros((300, 400, 3), np.uint8), binding_snapshots=bindings)
    job["precomputed_local"] = local
    monkeypatch.setenv("FIELD_VISION_DISPLAY_RULES", json.dumps([display_rule()]))
    refresh_recognition_config(app)
    calls = []

    def read(crop, **kwargs):
        calls.append(crop.shape[:2])
        if crop.shape[:2] != (300, 400):
            return dict(cloud_result(), answer={"status": "unreadable", "readings": []})
        assert kwargs["display_rules"] == [display_rule()]
        return dict(
            cloud_result(),
            answer={
                "status": "readable",
                "readings": [{"text": "144062 g", "value": "144062", "label": "质量", "unit": "g"}],
            },
        )

    monkeypatch.setattr(vision, "read_panel", read)
    clock = Clock()
    panel_readout.run(vars(app), job, clock=clock, pause=clock.pause)
    result = app.get_job(job["job_id"])
    assert result["status"] == "completed" and calls[-1] == (300, 400)
    assert len(calls) == (1 if missing_detection else 2)
    assert result["whole_photo_recovery"]["status"] == "completed"
    assert result["localized_attempt"]["panel_detection"]
    if not missing_detection:
        assert result["localized_attempt"]["fallback"]["answer"]["status"] == "unreadable"
    assert len(result["binding_snapshots"]) == 2
    assert result["instrument"] is result["binding_id"] is None
    assert result["readings"][0]["value"] == "14.4062"
    assert result["readings"][0]["instrument"] is None
    assert not result["readings"][0]["human_verified"]
    assert "panel_detection" not in result and not result.get("recognition_skipped")


def test_failed_whole_photo_recovery_retains_original_panel_evidence(
    app_client, configured, monkeypatch
):
    from tests.support.panel_regions import configure
    from tests.support.multi_readout import two_bindings

    app, client = app_client
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "camera_id")
    bindings = two_bindings(app, client)
    configure(app, monkeypatch, [])
    monkeypatch.setattr(app.readout_pool, "submit", lambda *a, **kwargs: None)
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app)), trigger="voice_photo_directory"
    )
    job["precomputed_local"] = app.predict_readout(
        np.zeros((300, 400, 3), np.uint8), binding_snapshots=bindings
    )
    monkeypatch.setattr(
        vision,
        "read_panel",
        lambda *a, **kw: {"status": "failed", "error": "timeout", "raw_answer": "partial"},
    )
    app.run_ocr(job)
    result = app.get_job(job["job_id"])
    assert result["recognition_skipped"] and result["panel_detection"]
    assert result["whole_photo_recovery"]["fallback"]["raw_answer"] == "partial"
    assert result["readings"] == []


@pytest.mark.parametrize("text", ["1440 18", "1440\u200918", "1440\t18 g"])
def test_spaced_lcd_digits_preserve_original_transcription(configured, monkeypatch, text):
    monkeypatch.setattr(
        vision,
        "_request",
        lambda *a, **kwargs: response(
            {
                "status": "readable",
                "readings": [{"text": text, "value": "144018", "label": None, "unit": None}],
            }
        ),
    )
    result = vision.read_panel(np.zeros((20, 20, 3), np.uint8))
    assert result["status"] == "completed"
    assert result["answer"]["readings"][0]["text"] == text
    assert result["answer"]["readings"][0]["value"] == "144018"
    assert result["raw_answer"]


@pytest.mark.parametrize(
    "text,value",
    [
        ("14.4018", "144018"),
        ("-144018", "144018"),
        ("+144018", "144018"),
        ("144018", "14401"),
        ("1440\n18", "144018"),
        ("1440 g 18", "144018"),
        ("1440a18", "144018"),
        ("14 .4018", "14.4018"),
        ("- 144018", "-144018"),
        ("- 144018", "144018"),
        ("+ 144018", "144018"),
    ],
)
def test_spaced_readout_cannot_delete_sign_decimal_unit_or_text(
    configured, monkeypatch, text, value
):
    monkeypatch.setattr(
        vision,
        "_request",
        lambda *a, **kwargs: response(
            {"status": "readable", "readings": [{"text": text, "value": value}]}
        ),
    )
    result = vision.read_panel(np.zeros((20, 20, 3), np.uint8))
    assert result["status"] == "failed" and result["error"] == "invalid_answer"
    assert result["raw_answer"]


def test_spaced_vision_readout_keeps_unknown_unit_through_reading_generation(
    app_client, configured, monkeypatch
):
    app, client = app_client
    job = unbound_job(app, client, monkeypatch)
    monkeypatch.setattr(
        vision,
        "_request",
        lambda *a, **kwargs: response(
            {
                "status": "readable",
                "readings": [{"text": "1440 18", "value": "144018", "label": None, "unit": None}],
            }
        ),
    )
    app.run_ocr(job)
    result = app.get_job(job["job_id"])
    reading = result["readings"][0]
    assert result["fallback"]["status"] == "completed"
    assert reading["text"] == "1440 18" and reading["value"] == "144018"
    assert reading["unit"] is None and reading["quality_issue"] is None
    assert reading["instrument"] is None


def test_bound_spaced_vision_readout_uses_registered_unit_without_suffix_invention(
    app_client, configured, monkeypatch
):
    from tests.support.panel_regions import configure, BOXES, B
    from tests.support.multi_readout import two_bindings

    app, client = app_client
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "camera_id")
    bindings = two_bindings(app, client)
    configure(app, monkeypatch, BOXES[1:])
    monkeypatch.setattr(app.readout_pool, "submit", lambda *a, **kwargs: None)
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app)), trigger="voice_photo_directory"
    )
    job["field_rules"][B] = {
        "质量": {"unit": "g", "decimal_places": 4, "fixed_decimal_display": True}
    }
    job["precomputed_local"] = app.predict_readout(
        np.zeros((300, 400, 3), np.uint8), binding_snapshots=bindings
    )
    monkeypatch.setattr(
        vision,
        "_request",
        lambda *a, **kwargs: response(
            {
                "status": "readable",
                "readings": [{"text": "1440 18", "value": "144018", "label": None, "unit": None}],
            }
        ),
    )
    app.run_ocr(job)
    result = app.get_job(job["job_id"])
    reading = result["readings"][0]
    assert reading["text"] == "1440 18" and reading["value"] == "14.4018"
    assert reading["unit"] is None and reading["quality_issue"] is None
    assert reading["normalized_value"]["unit"] == "g"
    assert reading["normalized_value"]["value"] == 14.4018
    assert reading["normalization"]["raw_text"] == "1440 18"
    assert reading["instrument"]["id"] == B
