from tests.support.digit_regions import line  # noqa: F401
import json
import cv2
import numpy as np
import pytest

from field_recognition.recognition.digit_regions import refine_digits


def test_main_digits_exclude_menu_number_and_map_polygons_to_original():
    raw = {
        "lines": [
            line("1", [15, 12, 5, 6]),
            line("0.0000", [30, 35, 60, 20]),
            line("MENU", [15, 20, 25, 8]),
        ],
        "actual_model_invocation": True,
    }
    calls = []

    def predict(image, x, y):
        calls.append(image.shape)
        return {"lines": [line("0.0000", [0, 0, image.shape[1] - 1, image.shape[0] - 1])]}

    result = refine_digits(predict, np.zeros((100, 120, 3), np.uint8), raw)
    assert [l["text"] for l in result["lines"]] == ["0.0000"]
    assert result["panel_ocr"] == raw
    assert len(calls) == 1 and calls[0][0] >= 64
    assert np.allclose(result["lines"][0]["polygon"], result["digit_region"]["polygon"], atol=0.001)
    assert result["digit_region"]["initial_text"] == "0.0000"


def test_missing_decimal_is_never_guessed_and_original_candidate_survives_failed_refine():
    raw = {"lines": [line("00000", [40, 30, 60, 20])]}
    out = refine_digits(lambda *a: {"lines": []}, np.zeros((100, 120, 3), np.uint8), raw)
    assert out["lines"][0]["text"] == "00000"
    assert not out["digit_region"]["refined_readout_available"]


def test_status_text_and_low_confidence_icons_do_not_create_numeric_roi():
    raw = {"lines": [line("OFF", [1, 1, 30, 18]), line("1", [10, 30, 5, 6], 0.1)]}
    out = refine_digits(
        lambda *a: pytest.fail("No main digit row"), np.zeros((100, 120, 3), np.uint8), raw
    )
    assert out["lines"] == [] and out["digit_region"]["status"] == "not_located"


def test_digit_coordinates_include_parent_crop_offset():
    raw = {"lines": [line("240", [120, 235, 60, 20])]}
    out = refine_digits(lambda *a: {"lines": []}, np.zeros((100, 120, 3), np.uint8), raw, 100, 200)
    assert min(p[0] for p in out["digit_region"]["polygon"]) >= 100
    assert min(p[1] for p in out["digit_region"]["polygon"]) >= 200


def test_partial_digit_text_can_locate_roi_but_is_not_guessed_into_a_reading():
    raw = {"lines": [line("5:", [40, 30, 60, 20])]}
    image = np.zeros((100, 120, 3), np.uint8)
    out = refine_digits(lambda *a: {"lines": []}, image, raw)
    assert out["digit_region"]["status"] == "located" and not out["lines"]
    out = refine_digits(lambda *a: {"lines": [line("51", [0, 0, 60, 20])]}, image, raw)
    assert out["lines"][0]["text"] == "51"


def test_conflicting_warp_does_not_overwrite_source_without_a_crop_check():
    raw = {"lines": [line("390", [40, 30, 60, 20])]}
    image = np.zeros((100, 120, 3), np.uint8)
    answers = iter(
        [{"lines": [line("290", [0, 0, 60, 20])]}, {"lines": [line("390", [12, 12, 60, 20])]}]
    )
    out = refine_digits(lambda *a: next(answers), image, raw, source_image=image)
    assert out["lines"][0]["text"] == "390" and out["digit_consistency"]["status"] == "resolved"
    assert out["digit_region"]["method"] == "dominant_numeric_line_source_check_v1"
    json.dumps(out)
    out = refine_digits(lambda *a: {"lines": [line("290", [0, 0, 60, 20])]}, image, raw)
    assert out["lines"] == [] and out["digit_consistency"]["status"] == "disagreement"


def test_photo_red_numeric_conflict_preserves_receipts_without_same_source_third_vote():
    raw = {
        "status": "completed",
        "request_id": "original",
        "lines": [line("95", [40, 30, 60, 20])],
        "ocr_finished_at": "original-finished",
    }
    original = json.loads(json.dumps(raw))
    receipt = {
        "status": "completed",
        "request_id": "warped",
        "lines": [line("55", [0, 0, 60, 20])],
        "ocr_finished_at": "warp-finished",
        "ocr_model_call_count": 1,
        "actual_model_invocation": True,
    }
    calls = []
    image = np.zeros((100, 120, 3), np.uint8)
    out = refine_digits(
        lambda *args: calls.append(1) or receipt,
        image,
        raw,
        source_image=image,
        recover_red=True,
        require_native_conflict_confirmation=True,
    )
    assert calls == [1] and "digit_ocr_check" not in out
    assert out["lines"] == []
    assert out["digit_consistency"] == {
        "status": "disagreement",
        "candidates": ["95", "55"],
        "check_text": None,
        "basis": "independent_native_confirmation_required",
        "native_confirmation_required": True,
    }
    assert out["panel_ocr"] == raw == original
    assert out["digit_ocr"] == receipt
    assert out["ocr_finished_at"] == "warp-finished"
    assert not out["digit_region"]["refined_readout_available"]


def test_failed_warp_cannot_promote_a_conflicting_wider_crop():
    raw = {"lines": [line("055", [40, 30, 60, 20], 0.67)]}
    image = np.zeros((100, 120, 3), np.uint8)
    answers = iter(
        [
            {"lines": [line("06S", [0, 0, 60, 20], 0.37)]},
            {"lines": [line("065", [12, 12, 60, 20], 0.62)]},
        ]
    )
    out = refine_digits(lambda *a: next(answers), image, raw, source_image=image)
    assert out["lines"] == []
    assert out["digit_consistency"]["status"] == "disagreement"
    assert out["digit_consistency"]["candidates"] == ["055", "065"]
    assert out["panel_ocr"] == raw


def test_small_lcd_retry_keeps_original_empty_output_and_source_coordinates():
    raw = {"status": "completed", "lines": []}
    calls = []

    def predict(image, x, y):
        calls.append(image.shape[:2])
        return (
            {"status": "completed", "lines": [line("02181", [40, 30, 100, 40])]}
            if len(calls) == 1
            else {"lines": []}
        )

    out = refine_digits(
        predict, np.zeros((100, 160, 3), np.uint8), raw, 400, 600, recover_small=True
    )
    assert calls[0] == (200, 320) and len(calls) == 2
    assert out["panel_ocr"] == raw and out["small_panel_recovery"]["scale"] == 2
    assert out["lines"][0]["text"] == "02181"
    assert out["lines"][0]["polygon"][0] == [420, 615]


def test_small_panel_no_text_retry_is_bounded_to_one():
    calls = []
    raw = {"status": "completed", "lines": []}
    out = refine_digits(
        lambda *a: calls.append(1) or raw,
        np.zeros((100, 160, 3), np.uint8),
        raw,
        recover_small=True,
    )
    assert len(calls) == 1 and out["lines"] == []


def red_off_panel():
    """Native bars with independent O/F/F direction, rather than mock geometry."""
    image = np.full((44, 80, 3), (5, 5, 100), np.uint8)
    for x, character in ((5, "O"), (30, "F"), (55, "F")):
        w, h, t, y = 18, 32, 4, 5
        bars = [(x, y, w, t), (x, y + h // 2 - t // 2, w, t), (x, y, t, h)]
        if character == "O":
            bars += [(x + w - t, y, t, h), (x, y + h - t, w, t)]
        for left, top, width, height in bars:
            image[top : top + height, left : left + width] = (190, 190, 255)
        if character == "O":
            image[y + h // 2 - t // 2 : y + h // 2 + t // 2, x + t : x + w - t] = (5, 5, 100)
    return image


def test_red_pixel_geometry_recovers_actual_off_and_preserves_receipts_and_offsets():
    image = red_off_panel()
    raw = {"status": "completed", "lines": [line("出", [100, 200, 80, 44])]}
    receipts = []

    def predict(view, x, y):
        receipt = {
            "status": "completed",
            "request_id": str(len(receipts)),
            "lines": [line("OFF", [0, 0, view.shape[1] - 1, view.shape[0] - 1])],
            "ocr_model_call_count": 1,
            "actual_model_invocation": True,
        }
        receipts.append(receipt)
        return receipt

    out = refine_digits(predict, image, raw, 100, 200, recover_red=True)
    assert len(receipts) == 2
    assert out["display_state"]["text"] == "OFF" and out["lines"] == []
    assert out["panel_ocr"] == raw
    assert [attempt["ocr"] for attempt in out["red_display_ocr_attempts"]] == receipts
    assert out["red_display_orientation"]["status"] == "consistent"
    assert out["red_display_orientation"]["orientation_degrees"] == 0
    assert np.allclose(out["display_state"]["polygon"], out["digit_region"]["polygon"], atol=0.001)
    assert min(p[0] for p in out["display_state"]["polygon"]) >= 100
    assert min(p[1] for p in out["display_state"]["polygon"]) >= 200
    json.dumps(out)


def test_rotated_red_off_coordinates_return_to_source_and_all_four_receipts_survive():
    image = cv2.rotate(red_off_panel(), cv2.ROTATE_180)
    raw = {"status": "completed", "lines": []}
    calls = []

    def predict(view, x, y):
        calls.append(view.copy())
        text = "OFF" if len(calls) > 2 else "出"
        return {
            "lines": [line(text, [0, 0, view.shape[1] - 1, view.shape[0] - 1])],
            "request_id": f"adapter-{len(calls)}",
        }

    out = refine_digits(predict, image, raw, 7, 13, recover_red=True)
    assert len(calls) == 4
    assert out["red_display_orientation"]["orientation_degrees"] == 180
    assert out["red_display_orientation"]["status"] == "consistent"
    assert out["display_state"]["text"] == "OFF"
    assert np.allclose(
        out["display_state"]["polygon"],
        np.asarray(out["digit_region"]["polygon"])[[2, 3, 0, 1]],
        atol=0.001,
    )
    assert len(out["red_display_ocr_attempts"]) == 4


@pytest.mark.parametrize("rotated,calls", [(False, 2), (True, 4)])
def test_red_retries_never_replace_ambiguous_characters_or_infer_a_rotated_number(rotated, calls):
    image = red_off_panel()
    if rotated:
        image = cv2.rotate(image, cv2.ROTATE_180)
    raw = {"status": "completed", "lines": [line("1B2", [0, 0, 80, 44])]}
    answers = iter(("5G", "出", "182", "182"))
    out = refine_digits(
        lambda view, *a: {"lines": [line(next(answers), [0, 0, 50, 40])]},
        image,
        raw,
        recover_red=True,
    )
    assert not out["lines"] and not out.get("display_state")
    assert len(out["red_display_ocr_attempts"]) == calls
    assert out["panel_ocr"] == raw


def test_red_geometry_numeric_requires_matching_views_and_never_inserts_decimal_or_unit():
    raw = {"status": "completed", "lines": [line("1B2", [0, 0, 80, 44])]}
    image = red_off_panel()
    for x in (30, 55):
        image[5:37, x + 14 : x + 18] = (190, 190, 255)
        image[33:37, x : x + 18] = (190, 190, 255)
    calls = []

    def predict(view, *args):
        calls.append(1)
        return {"lines": [line("182", [0, 0, view.shape[1] - 1, view.shape[0] - 1])]}

    out = refine_digits(predict, image, raw, 20, 30, recover_red=True)
    assert len(calls) == 2 and [candidate["text"] for candidate in out["lines"]] == ["182"]
    assert "unit" not in out["lines"][0]
    assert out["red_display_orientation"]["status"] == "unconfirmed"


def test_non_red_pixels_and_disabled_recovery_do_not_spend_extra_adapter_calls():
    raw = {"status": "completed", "lines": [line("出", [0, 0, 80, 44])]}
    for image, enabled in ((red_off_panel(), False), (np.zeros((44, 80, 3), np.uint8), True)):
        out = refine_digits(
            lambda *a: pytest.fail("No authorized red geometry retry"),
            image,
            raw,
            recover_red=enabled,
        )
        assert not out["lines"] and out["panel_ocr"] == raw


@pytest.mark.parametrize(
    "polygon",
    [
        None,
        [[0, 0]],
        [["bad", 0]] * 4,
        [[float("nan"), 0]] * 4,
        [[10000, 0]] * 4,
        [[0, 0]] * 4,
        [[0, 0], [1, 0], [2, 0], [3, 0]],
    ],
)
def test_red_retry_malformed_polygon_is_preserved_raw_but_never_promoted(polygon):
    raw = {"status": "completed", "lines": []}
    receipt = {"lines": [{"text": "OFF", "confidence": 0.9, "polygon": polygon}]}
    out = refine_digits(lambda *a: receipt, red_off_panel(), raw, recover_red=True)
    assert not out["lines"] and not out.get("display_state")
    assert len(out["red_display_ocr_attempts"]) == 2
    assert out["red_display_ocr_attempts"][0]["ocr"] == receipt


def test_red_retry_infinite_confidence_cannot_become_a_reading():
    raw = {"status": "completed", "lines": []}
    out = refine_digits(
        lambda view, *a: {
            "lines": [line("OFF", [0, 0, view.shape[1] - 1, view.shape[0] - 1], float("inf"))]
        },
        red_off_panel(),
        raw,
        recover_red=True,
    )
    assert not out["lines"] and not out.get("display_state")
    assert len(out["red_display_ocr_attempts"]) == 2


def test_red_retry_does_not_replace_existing_numeric_with_conflicting_numeric():
    image = red_off_panel()
    raw = {"status": "completed", "lines": [line("182", [5, 5, 60, 30])]}
    calls = []

    def predict(view, *args):
        calls.append(1)
        # Original numeric refinement is stable; new red view contradicts it.
        text = "182" if len(calls) == 1 else "175"
        return {"lines": [line(text, [0, 0, view.shape[1] - 1, view.shape[0] - 1])]}

    out = refine_digits(predict, image, raw, recover_red=True)
    assert not out["lines"] and not out.get("display_state")
    assert out["digit_consistency"]["status"] == "disagreement"
    assert out["digit_consistency"]["candidates"] == ["182", "175"]


def test_conflicting_off_cannot_overwrite_numeric_without_native_off_bars():
    image = red_off_panel()
    # Close the F right and bottom strokes; the original pixels are not OFF.
    for x in (30, 55):
        image[5:37, x + 14 : x + 18] = (190, 190, 255)
        image[33:37, x : x + 18] = (190, 190, 255)
    raw = {"status": "completed", "lines": [line("182", [5, 5, 60, 30])]}
    calls = []

    def predict(view, *args):
        calls.append(1)
        text = "175" if len(calls) == 1 else "OFF"
        return {"lines": [line(text, [0, 0, view.shape[1] - 1, view.shape[0] - 1])]}

    out = refine_digits(predict, image, raw, recover_red=True)
    assert not out["lines"] and not out.get("display_state")
    assert out["digit_consistency"]["status"] == "disagreement"
    assert out["digit_consistency"]["candidates"] == ["182", "OFF"]


def test_native_180_direction_rejects_zero_degree_off_and_retries_correct_orientation():
    image = cv2.rotate(red_off_panel(), cv2.ROTATE_180)
    calls = []

    def predict(view, *args):
        calls.append(1)
        return {"lines": [line("OFF", [0, 0, view.shape[1] - 1, view.shape[0] - 1])]}

    out = refine_digits(predict, image, {"status": "completed", "lines": []}, recover_red=True)
    assert len(calls) == 4
    assert out["red_display_orientation"]["orientation_degrees"] == 180
    assert out["red_display_orientation"]["status"] == "consistent"
    assert out["display_state"]["text"] == "OFF"


@pytest.mark.parametrize("native_direction", [True, False])
def test_original_exact_off_fastpath_preserves_receipt_and_spends_no_adapter_retry(
    native_direction,
):
    image = red_off_panel() if native_direction else np.zeros((44, 80, 3), np.uint8)
    raw = {
        "status": "completed",
        "lines": [line("OFF", [100, 200, 79, 43])],
        "request_id": "original-off",
        "ocr_started_at": "original-started",
        "ocr_finished_at": "original-finished",
        "ocr_model_call_count": 1,
        "ocr_model_call_ms": 32.0,
        "actual_model_invocation": True,
    }
    original = json.loads(json.dumps(raw))
    out = refine_digits(
        lambda *args: pytest.fail("Actual first-pass OFF needs no resized OCR"),
        image,
        raw,
        100,
        200,
        recover_red=True,
        require_native_conflict_confirmation=True,
    )
    assert out["panel_ocr"] == raw == original
    assert out["lines"] == [] and out["display_state"]["text"] == "OFF"
    assert out["display_state"]["source"] == "local_ocr.panel_ocr"
    assert out["display_state"]["polygon"] == raw["lines"][0]["polygon"]
    assert out["display_state"]["verification"] == (
        "native_corroborated" if native_direction else "ocr_only"
    )
    assert out["ocr_finished_at"] == "original-finished"
    assert out["ocr_model_call_count"] == 1 and out["ocr_model_call_ms"] == 32.0
    assert not out.get("red_display_ocr_attempts") and "digit_ocr" not in out
    if native_direction:
        assert out["red_display_orientation"]["orientation_degrees"] == 0
    else:
        assert not out.get("red_display_orientation")


def test_original_off_native_reverse_direction_cannot_fastpath_or_fallback_to_raw_state():
    image = cv2.rotate(red_off_panel(), cv2.ROTATE_180)
    raw = {"status": "completed", "lines": [line("OFF", [0, 0, 79, 43])]}
    calls = []
    out = refine_digits(
        lambda *args: calls.append(1) or {"status": "completed", "lines": []},
        image,
        raw,
        recover_red=True,
    )
    assert len(calls) == 4 and not out.get("display_state")
    assert not out.get("red_display_fastpath")
    assert out["display_state_rejection"] == {
        "text": "OFF",
        "basis": "native_off_direction_conflict",
        "native_directions": [180],
    }
    assert out["panel_ocr"] == raw
    assert out["red_display_retry_gate"]["orientation_degrees"] == [0, 180]


@pytest.mark.parametrize("confidence", [0.59, float("inf"), float("nan"), "invalid"])
def test_uncertain_or_nonfinite_original_off_is_not_an_exact_off_fastpath(confidence):
    raw = {"status": "completed", "lines": [line("OFF", [0, 0, 79, 43], confidence)]}
    out = refine_digits(
        lambda *args: {"status": "completed", "lines": []},
        red_off_panel(),
        raw,
        recover_red=True,
    )
    assert not out.get("red_display_fastpath") and not out.get("display_state")
    assert len(out["red_display_ocr_attempts"]) == 2


def test_red_rotation_needs_independent_native_direction_not_empty_upright_ocr():
    image = red_off_panel()
    for x in (30, 55):
        image[5:37, x + 14 : x + 18] = (190, 190, 255)
        image[33:37, x : x + 18] = (190, 190, 255)
    calls = []
    out = refine_digits(
        lambda *args: calls.append(1) or {"status": "completed", "lines": []},
        image,
        {"status": "completed", "lines": []},
        recover_red=True,
    )
    assert calls == [1, 1] and not out.get("display_state")
    assert out["red_display_retry_gate"]["native_directions"] == []
    assert out["red_display_retry_gate"]["orientation_degrees"] == [0]
    assert all(attempt["orientation_degrees"] == 0 for attempt in out["red_display_ocr_attempts"])


def test_strict_conflict_without_native_off_skips_more_resized_ocr_votes():
    image = red_off_panel()
    for x in (30, 55):
        image[5:37, x + 14 : x + 18] = (190, 190, 255)
        image[33:37, x : x + 18] = (190, 190, 255)
    raw = {"status": "completed", "lines": [line("95", [5, 5, 60, 30])]}
    receipt = {"status": "completed", "lines": [line("55", [0, 0, 60, 30])]}
    calls = []
    out = refine_digits(
        lambda *args: calls.append(1) or receipt,
        image,
        raw,
        source_image=image,
        recover_red=True,
        require_native_conflict_confirmation=True,
    )
    assert calls == [1] and out["lines"] == []
    assert out["digit_consistency"]["candidates"] == ["95", "55"]
    assert out["digit_consistency"]["native_confirmation_required"]
    assert (
        out["red_display_retry_gate"]["skip_reason"] == "independent_native_confirmation_required"
    )
    assert not out.get("red_display_ocr_attempts") and "digit_ocr_check" not in out
    assert out["panel_ocr"] == raw and out["digit_ocr"] == receipt


def test_native_off_can_settle_photo_numeric_conflict_with_actual_off_receipts():
    image = red_off_panel()
    raw = {"status": "completed", "lines": [line("182", [5, 5, 60, 30])]}
    calls = []

    def predict(view, *args):
        text = "175" if not calls else "OFF"
        receipt = {
            "status": "completed",
            "request_id": f"actual-{len(calls)}",
            "lines": [line(text, [0, 0, view.shape[1] - 1, view.shape[0] - 1])],
        }
        calls.append(receipt)
        return receipt

    out = refine_digits(
        predict,
        image,
        raw,
        source_image=image,
        recover_red=True,
        require_native_conflict_confirmation=True,
    )
    assert len(calls) == 3 and "digit_ocr_check" not in out
    assert out["display_state"]["verification"] == "native_corroborated"
    assert out["digit_consistency"]["status"] == "resolved"
    assert out["digit_consistency_before_red_display"]["candidates"] == ["182", "175"]
    assert out["panel_ocr"] == raw and out["digit_ocr"] == calls[0]
    assert [attempt["ocr"] for attempt in out["red_display_ocr_attempts"]] == calls[1:]
