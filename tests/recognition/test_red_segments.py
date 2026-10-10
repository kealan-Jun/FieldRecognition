"""Regression guards for disconnected LED bars; real-photo proof is separate."""

import copy

import cv2
import numpy as np
import pytest

from field_recognition.recognition.led_digits import PATTERNS, native_diagnostics, verify_digits
from field_recognition.recognition.red_segments import check, corroborates


def display(text, *, positions=None):
    image = np.full((72, 40 * len(text) + 20, 3), (10, 15, 190), np.uint8)
    rectangles = (
        (5, 0, 19, 3),
        (21, 6, 24, 17),
        (21, 25, 24, 36),
        (5, 39, 19, 42),
        (0, 25, 3, 36),
        (0, 6, 3, 17),
        (5, 19, 19, 22),
    )
    for i, value in enumerate(text):
        offset = positions[i] if positions is not None else 40 * i
        bits = next(bits for bits, digit in PATTERNS.items() if digit == value)
        for bit, (x1, y1, x2, y2) in zip(bits, rectangles):
            if bit == "1":
                cv2.rectangle(
                    image,
                    (x1 + 10 + offset, y1 + 12),
                    (x2 + 10 + offset, y2 + 12),
                    (65, 120, 255),
                    -1,
                )
    return cv2.GaussianBlur(image, (3, 3), 0.7)


def local(image, final, *attempts):
    h, w = image.shape[:2]
    result = {
        "lines": [{"text": final, "confidence": 0.9}] if final else [],
        "digit_region": {
            "status": "located",
            "polygon": [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]],
            "size": [w, h],
        },
    }
    for name, text in zip(("panel_ocr", "digit_ocr", "digit_ocr_check"), attempts):
        result[name] = {"lines": [{"text": text, "confidence": 0.8}]}
    return result


@pytest.mark.parametrize("text", ["146", "990", "998", "1140", "12", "23", "56", "78"])
def test_separated_bars_are_read_from_exact_patterns(text):
    result = check(display(text))
    assert result["status"] == "consistent" and result["text"] == text
    assert len(result["views"]) >= 2


def test_original_zero_is_not_outvoted_by_two_resampled_eights():
    image = display("990")
    raw = local(image, "998", "990", "998", "998")
    before = copy.deepcopy(raw)
    result = verify_digits(image, raw)
    assert result["lines"][0]["text"] == "990"
    assert result["lines"][0]["original_ocr_text"] == "990"
    assert result["lines"][0]["confidence"] is None
    assert result["pre_segment_lines"][0]["text"] == "998"
    assert result["panel_ocr"] == raw["panel_ocr"] and raw == before


def test_missing_temperature_digit_requires_pixel_and_ocr_support():
    image = display("146")
    raw = local(image, None, "6", "1H6", "16")
    result = verify_digits(image, raw, 100, 200)
    assert result["lines"][0]["text"] == "146"
    assert result["lines"][0]["original_ocr_text"] == "6"
    assert result["digit_region"]["polygon"][0][0] >= 100
    assert result["digit_region"]["polygon"][0][1] >= 200
    assert not verify_digits(image, local(image, None, "6", "H", "99"))["lines"]


def test_eight_is_not_always_changed_to_zero_and_unrelated_conflict_is_not_guessed():
    image = display("998")
    assert verify_digits(image, local(image, "998", "998"))["lines"][0]["text"] == "998"
    result = verify_digits(image, local(image, "990", "990"))
    assert not result["lines"] and result["digit_consistency"]["status"] == "disagreement"
    assert not corroborates("146", ["6", "99", "147"])


@pytest.mark.parametrize("text", ["OFF", "-146", "14.6", "146 °C"])
def test_status_sign_decimal_and_unit_are_not_removed(text):
    raw = local(display("146"), None, text)
    assert verify_digits(display("146"), raw) is raw


def test_tiny_blurred_glyphs_and_plain_background_cannot_settle_ocr():
    assert check(cv2.resize(display("87"), None, fx=0.3, fy=0.3))["status"] == "inconclusive"
    assert check(np.full((80, 180, 3), 125, np.uint8))["status"] == "inconclusive"


def test_a_visible_decimal_dot_prevents_an_integer_recovery():
    image = display("146")
    cv2.rectangle(image, (80, 49), (83, 53), (65, 120, 255), -1)
    assert check(image)["status"] != "consistent"


@pytest.mark.parametrize("actual,ocr", [("385", "3B5"), ("718", "118"), ("871", "B71")])
def test_clear_native_bars_support_only_local_ocr_confusions_without_a_digit_region(actual, ocr):
    image = display(actual)
    raw = local(image, None, ocr)
    raw["digit_region"] = {"status": "not_located"}
    result = verify_digits(image, raw, 100, 200)
    assert result["lines"][0]["text"] == actual
    assert result["lines"][0]["original_ocr_text"] == ocr
    assert result["lines"][0]["confidence"] is None
    assert result["panel_ocr"] == raw["panel_ocr"]
    assert result["digit_region"]["polygon"][0][0] >= 100


@pytest.mark.parametrize("ocr", ["3B5", "3H5", "BB", "3B5 rpm", "3.B5", "-3B5"])
def test_uncorroborated_or_noninteger_ocr_does_not_become_pixel_digit_guess(ocr):
    image = display("395")
    raw = local(image, None, ocr)
    assert not verify_digits(image, raw)["lines"]


def test_low_resolution_b_cannot_be_repaired_by_enlarging_the_region():
    image = cv2.resize(display("385"), None, fx=0.3, fy=0.3)
    raw = local(image, None, "3B5")
    raw["digit_region"]["size"] = [image.shape[1] * 5, image.shape[0] * 5]
    assert not verify_digits(image, raw)["lines"]


def test_stable_merged_projection_group_is_reviewed_without_claiming_a_missing_value():
    image = display("405", positions=[0, 40, 60])
    raw = local(image, "45", "45", "45")
    result = verify_digits(image, raw)
    assert result["lines"][0]["text"] == "45"
    assert result["lines"][0]["quality_issue"] == "ambiguous_led_glyphs"
    assert result["lines"][0]["confidence"] is None
    diagnosis = result["digit_segment_check"]["native_diagnostics"]
    assert diagnosis["group_count"] == 2 and diagnosis["merged_glyphs_possible"]
    assert not diagnosis["below_resolution_gate"]


def test_retry_ocr_receipts_can_corroborate_but_are_preserved_verbatim():
    image = display("385")
    raw = local(image, None, "H")
    raw["red_display_ocr_attempts"] = [
        {"view": "red_mask", "orientation_degrees": 0, "ocr": {"lines": [{"text": "3B5"}]}}
    ]
    before = copy.deepcopy(raw)
    result = verify_digits(image, raw)
    assert result["lines"][0]["text"] == "385"
    assert result["red_display_ocr_attempts"] == before["red_display_ocr_attempts"]
    assert raw == before


def test_disconnected_parts_of_one_glyph_are_not_counted_as_extra_digits():
    image = display("870", positions=[0, 40, 56])
    diagnosis = native_diagnostics(image)
    assert diagnosis["status"] == "inconclusive"
    raw = local(image, "80", "80")
    result = verify_digits(image, raw)
    assert result["lines"] == raw["lines"]
    assert not result.get("digit_consistency", {}).get("quality_issues")
