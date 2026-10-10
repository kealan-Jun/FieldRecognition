import copy

import cv2
import numpy as np
import pytest

from field_recognition.measurements.measurement_records import measurement_value
from field_recognition.recognition.led_digits import (
    PATTERNS,
    native_diagnostics,
    segment_check,
    verify_digits,
)


def display(text):
    image = np.full((70, len(text) * 44 + 12, 3), (15, 20, 125), np.uint8)
    boxes = [
        (5, 0, 27, 8),
        (22, 5, 30, 25),
        (22, 25, 30, 45),
        (5, 42, 27, 50),
        (0, 25, 8, 45),
        (0, 5, 8, 25),
        (5, 21, 27, 29),
    ]
    for i, char in enumerate(text):
        pattern = next(k for k, v in PATTERNS.items() if v == char)
        for flag, (x1, y1, x2, y2) in zip(pattern, boxes):
            if flag == "1":
                cv2.rectangle(
                    image,
                    (12 + i * 44 + x1, 10 + y1),
                    (12 + i * 44 + x2, 10 + y2),
                    (235, 245, 255),
                    -1,
                )
    return image


def local(text, image):
    h, w = image.shape[:2]
    return {
        "lines": [{"text": text, "confidence": 0.99}],
        "digit_region": {
            "status": "located",
            "polygon": [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]],
            "size": [w, h],
        },
    }


def test_visible_top_segment_resolves_one_seven_without_overwriting_raw_ocr():
    image = display("72")
    raw = local("12", image)
    before = copy.deepcopy(raw)
    result = verify_digits(image, raw)
    assert result["lines"][0]["text"] == "72"
    assert result["lines"][0]["original_ocr_text"] == "12"
    assert result["lines"][0]["confidence"] is None  # Do not invent calibrated accuracy.
    assert result["digit_segment_check"]["status"] == "consistent"
    assert len(result["digit_segment_check"]["views"]) >= 2
    assert raw == before


@pytest.mark.parametrize("text", ["56", "78", "90"])
def test_unambiguous_segment_patterns_agree(text):
    result = segment_check(display(text), len(text))
    assert result["status"] == "consistent" and result["text"] == text


def test_marginal_segment_geometry_keeps_ocr_instead_of_claiming_a_correction():
    image = display("23")
    raw = local("23", image)
    result = verify_digits(image, raw)
    assert result["digit_segment_check"]["status"] == "inconclusive"
    assert result["lines"] == raw["lines"]


def test_other_numeric_disagreement_requires_fallback_instead_of_replacement():
    image = display("72")
    result = verify_digits(image, local("82", image))
    assert not result["lines"] and result["digit_consistency"]["status"] == "disagreement"


@pytest.mark.parametrize("text", ["OFF", "-12", "1.2", "12 g"])
def test_noninteger_status_sign_decimal_or_unit_is_not_reinterpreted(text):
    image = display("72")
    raw = local(text, image)
    assert verify_digits(image, raw) is raw


def test_plain_background_cannot_correct_ocr_and_offset_coordinates_are_supported():
    image = np.full((70, 100, 3), 128, np.uint8)
    raw = local("12", image)
    assert verify_digits(image, raw)["lines"] == raw["lines"]
    image = display("72")
    raw = local("12", image)
    raw["digit_region"]["polygon"] = (
        np.asarray(raw["digit_region"]["polygon"]) + [300, 400]
    ).tolist()
    assert verify_digits(image, raw, 300, 400)["lines"][0]["text"] == "72"


def test_b_eight_correction_requires_complete_pixel_patterns_and_ocr_support():
    image = display("78")
    raw = local("1B", image)
    before = copy.deepcopy(raw)
    result = verify_digits(image, raw)
    assert result["lines"][0]["text"] == "78"
    assert result["lines"][0]["original_ocr_text"] == "1B"
    assert result["lines"][0]["confidence"] is None
    assert raw == before
    assert not verify_digits(display("79"), local("1B", display("79")))["lines"]


@pytest.mark.parametrize("text", ["BB", "8H", "8b", "8B rpm", "8.B", "+8B", "OFF"])
def test_letters_units_and_noninteger_states_are_not_numeric_confusion_candidates(text):
    image = display("88")
    raw = local(text, image)
    assert verify_digits(image, raw) is raw


@pytest.mark.parametrize("text", ["358", "405", "581"])
def test_visible_extra_low_resolution_group_marks_candidate_without_inventing_digit(text):
    image = cv2.resize(display(text), None, fx=0.3, fy=0.3)
    raw = local(text[1:], image)
    raw["panel_ocr"] = {"lines": copy.deepcopy(raw["lines"])}
    raw["digit_region"]["size"] = [image.shape[1] * 4, image.shape[0] * 4]
    before = copy.deepcopy(raw)
    result = verify_digits(image, raw)
    line = result["lines"][0]
    assert line["text"] == text[1:]
    assert line["confidence"] is None
    assert line["quality_issue"] == "low_native_resolution"
    assert "possible_missing_digit" in line["quality_issues"]
    assert result["pre_segment_lines"] == before["lines"]
    assert result["panel_ocr"] == before["panel_ocr"] and raw == before
    diagnosis = result["digit_segment_check"]["native_diagnostics"]
    assert diagnosis["basis"] == "original_panel_pixels"
    assert diagnosis["group_count"] == len(text)
    assert diagnosis["glyph_height_range"][1] < 20
    assert result["digit_consistency"]["status"] == "quality_issue"
    # The existing consumer keeps candidate text while withholding a formal value.
    normalized, _ = measurement_value(
        "speed", {"value": line["text"], "quality_issue": line["quality_issue"]}, {}
    )
    assert normalized["value"] is None


@pytest.mark.parametrize("text", ["87", "72", "358"])
def test_low_resolution_alone_keeps_ocr_but_cannot_support_enlarged_pixel_repair(text):
    image = cv2.resize(display(text), None, fx=0.3, fy=0.3)
    raw = local(text, image)
    raw["digit_region"]["size"] = [image.shape[1] * 4, image.shape[0] * 4]
    result = verify_digits(image, raw)
    assert result["lines"] == raw["lines"]
    assert "digit_consistency" not in result
    assert result["digit_segment_check"]["status"] == "inconclusive"
    assert result["digit_segment_check"]["reason"] == "insufficient_native_resolution"
    assert result["digit_segment_check"]["native_diagnostics"]["below_resolution_gate"]


def test_low_resolution_geometry_is_not_a_colorless_or_flat_red_background_rejection():
    for color in (128, (10, 15, 190)):
        image = np.full((22, 38, 3), color, np.uint8)
        raw = local("56", image)
        assert native_diagnostics(image)["status"] == "inconclusive"
        assert verify_digits(image, raw)["lines"] == raw["lines"]


def test_visible_baseline_dot_blocks_integer_repair_without_inserting_decimal():
    image = display("78")
    cv2.rectangle(image, (48, 56), (51, 59), (235, 245, 255), -1)
    raw = local("18", image)
    result = verify_digits(image, raw)
    assert result["lines"][0]["text"] == "18"
    assert result["lines"][0]["quality_issue"] == "possible_decimal_point"
    assert result["digit_segment_check"]["native_diagnostics"]["possible_decimal_point"]
    assert "." not in result["lines"][0]["text"]


@pytest.mark.parametrize("background", [190, 220])
@pytest.mark.parametrize("scale", [0.25, 0.3])
def test_inconclusive_native_color_diagnostics_cannot_bypass_original_stroke_height(
    background, scale
):
    image = display("72")
    image[(image == (15, 20, 125)).all(axis=2)] = (15, 20, background)
    image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    raw = local("12", image)
    raw["digit_region"]["size"] = [image.shape[1] * 4, image.shape[0] * 4]
    before = copy.deepcopy(raw)
    result = verify_digits(image, raw)
    assert result["lines"] == raw["lines"]
    assert result.get("digit_consistency", {}).get("status") != "resolved"
    assert result["digit_segment_check"]["status"] == "inconclusive"
    assert raw == before
