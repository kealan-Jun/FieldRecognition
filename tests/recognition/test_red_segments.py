"""Regression guards for disconnected LED bars; real-photo proof is separate."""

import copy

import cv2
import numpy as np
import pytest

from field_recognition.recognition.led_digits import PATTERNS, native_diagnostics, verify_digits
from field_recognition.recognition.red_segments import check, corroborates
from field_recognition.measurements.measurement_records import measurement_value


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
        bits = (
            "1000111"
            if value == "F"
            else next(bits for bits, digit in PATTERNS.items() if digit == value)
        )
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


def tilted_display(text, angle):
    image = cv2.copyMakeBorder(
        display(text), 25, 25, 25, 25, cv2.BORDER_CONSTANT, value=(15, 15, 15)
    )
    height, width = image.shape[:2]
    transform = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    return cv2.warpAffine(
        image,
        transform,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
    )


@pytest.mark.parametrize(
    "text,angle", [("123", 8), ("672", -5), ("718", 8), ("405", 5), ("871", -8)]
)
def test_measured_native_roll_can_read_complete_patterns_in_both_directions(text, angle):
    image = tilted_display(text, angle)
    result = check(image)
    assert result["status"] == "consistent" and result["text"] == text
    deskew = result["native_deskew"]
    assert deskew["scale"] == 1.0 and deskew["interpolation"] == "nearest"
    assert len(result["views"]) >= 2
    assert result["native_window_size"][1] >= 20
    polygon = np.asarray(result["polygon"])
    assert (polygon >= 0).all()
    assert (polygon[:, 0] < image.shape[1]).all()
    assert (polygon[:, 1] < image.shape[0]).all()
    assert abs(deskew["roll_degrees"] + angle) < 0.1


def test_tilted_recovery_requires_complete_source_digits_and_corroborating_ocr():
    image = tilted_display("672", -5)
    raw = local(image, None, "62", "6H2")
    before = copy.deepcopy(raw)
    result = verify_digits(image, raw, 100, 200)
    assert result["lines"][0]["text"] == "672"
    assert result["lines"][0]["confidence"] is None
    source_polygon = np.asarray(result["digit_segment_check"]["polygon"])
    assert np.allclose(result["lines"][0]["polygon"], source_polygon + [100, 200])
    assert result["panel_ocr"] == before["panel_ocr"] and raw == before
    assert not verify_digits(image, local(image, None, "99", "6H2"))["lines"]


@pytest.mark.parametrize("scale", [0.25, 0.3, 0.4])
def test_native_floor_applies_before_rotation_even_with_enlarged_saved_window(scale):
    image = cv2.resize(tilted_display("672", -5), None, fx=scale, fy=scale)
    diagnosis = native_diagnostics(image)
    result = check(image)
    assert result["status"] == "inconclusive"
    assert "native_deskew" not in result
    if diagnosis["status"] == "measured":
        assert diagnosis["below_resolution_gate"]
    raw = local(image, None, "62", "6H2")
    raw["digit_region"]["size"] = [image.shape[1] * 8, image.shape[0] * 8]
    assert not verify_digits(image, raw)["lines"]


def test_decimal_dot_and_clipped_glyphs_do_not_gain_a_deskew_integer():
    image = display("672")
    cv2.rectangle(image, (80, 49), (83, 53), (65, 120, 255), -1)
    image = cv2.copyMakeBorder(image, 25, 25, 25, 25, cv2.BORDER_CONSTANT)
    height, width = image.shape[:2]
    image = cv2.warpAffine(
        image,
        cv2.getRotationMatrix2D((width / 2, height / 2), -5, 1),
        (width, height),
        flags=cv2.INTER_NEAREST,
    )
    assert check(image)["status"] != "consistent"
    clipped = tilted_display("672", -5)[:, 45:]
    assert check(clipped)["status"] != "consistent"


def test_clear_native_diagnostics_are_retained_when_ocr_conflict_removed_formal_lines():
    image = cv2.GaussianBlur(tilted_display("672", -5), (15, 15), 3)
    raw = local(image, None, "62", "672")
    raw["digit_consistency"] = {"status": "disagreement", "candidates": ["62", "672"]}
    before = copy.deepcopy(raw)
    result = verify_digits(image, raw)
    assert result["lines"] == []
    assert result["digit_consistency"] == before["digit_consistency"]
    assert result["digit_segment_check"]["status"] == "inconclusive"
    assert result["digit_segment_check"]["native_diagnostics"]["basis"] == "original_panel_pixels"
    assert result["panel_ocr"] == before["panel_ocr"] and raw == before


def test_tilted_off_bars_remain_status_letters_instead_of_numeric_zero():
    image = tilted_display("0FF", -8)
    assert check(image)["status"] == "inconclusive"
    raw = local(image, None, "OFF")
    assert verify_digits(image, raw) is raw
    raw = local(image, "12", "12")
    result = verify_digits(image, raw)
    assert result.get("digit_consistency", {}).get("status") != "resolved"
    assert result["lines"][0]["text"] == "12"
    assert result["lines"][0]["quality_issue"] == "possible_missing_digit"


def test_existing_complete_source_pattern_vetoes_a_contrary_deskew(monkeypatch):
    import field_recognition.recognition.red_segments as segments

    image = tilted_display("672", -5)
    original_check = segments._check

    def conflicting_source(candidate, factors=(0.60, 0.70, 0.75)):
        if candidate is image:
            return {
                "rule_version": segments.VERSION,
                "status": "inconclusive",
                "views": [{"text": "612", "threshold": 50}],
            }
        return original_check(candidate, factors)

    monkeypatch.setattr(segments, "_check", conflicting_source)
    result = segments.check(image)
    assert result["status"] == "disagreement" and "text" not in result
    assert result["native_deskew"]["views"][0]["text"] == "612"
    assert not verify_digits(image, local(image, None, "62", "672"))["lines"]


def test_complete_native_recovery_does_not_reuse_a_superseded_quality_flag():
    image = tilted_display("672", -5)
    raw = local(image, "62", "62", "672")
    raw["lines"][0].update(
        quality_issue="possible_missing_digit", quality_issues=["possible_missing_digit"]
    )
    result = verify_digits(image, raw)
    assert result["pre_segment_lines"][0]["quality_issue"] == "possible_missing_digit"
    assert not result["lines"][0].get("quality_issue")
    normalized, _ = measurement_value("speed", result["lines"][0], {})
    assert normalized["value"] == 672
