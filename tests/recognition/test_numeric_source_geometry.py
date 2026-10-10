"""Source-only numeric retry geometry and monotonic channel preparation."""

import cv2
import numpy as np
import pytest
from types import SimpleNamespace

from field_recognition.recognition import digit_regions, panel_regions, red_geometry
from field_recognition.recognition.digit_regions import (
    _red_ocr_views,
    refine_digits,
    source_bar_geometry,
)
from tests.support.digit_regions import line
from tests.support.panel_regions import A


def bar_row(*, positions=(15, 45, 75), y=20, shape=(80, 120)):
    image = np.full((*shape, 3), (5, 5, 100), np.uint8)
    for left in positions:
        for dx, dy, w, h in (
            (0, 0, 18, 3),
            (0, 14, 18, 3),
            (0, 29, 18, 3),
            (0, 0, 3, 32),
            (15, 0, 3, 32),
        ):
            xx, yy = left + dx, y + dy
            patch = image[yy : yy + h, xx : xx + w]
            if patch.size:
                patch[:] = (60, 40, 255)
                patch[:, ::2] = (150, 120, 255)
    return image


def test_complete_source_row_is_measured_twice_without_decoding_or_mutating_pixels():
    image = bar_row()
    before = image.copy()
    geometry = source_bar_geometry(image, 100, 200)
    assert geometry is not None
    assert geometry["purpose"] == "numeric_retry_geometry_only"
    assert "text" not in geometry and "value" not in geometry
    assert [view["threshold_factor"] for view in geometry["supporting_views"]] == [0.7, 0.8]
    assert all(len(view["glyph_boxes"]) == 3 for view in geometry["supporting_views"])
    assert all(
        box[0] >= 100 and box[1] >= 200
        for view in geometry["supporting_views"]
        for box in view["glyph_boxes"]
    )
    assert geometry["coordinate_space"] == "source_image"
    assert geometry["native_size"][1] == 35
    assert np.array_equal(image, before)
    assert np.isfinite(geometry["polygon"]).all()
    assert min(p[0] for p in geometry["polygon"]) >= 100
    assert min(p[1] for p in geometry["polygon"]) >= 200


def test_measured_tilt_and_offset_map_to_the_original_source_row():
    original = bar_row(shape=(100, 140), positions=(20, 50, 80), y=30)
    matrix = cv2.getRotationMatrix2D((70, 50), 10, 1)
    tilted = cv2.warpAffine(original, matrix, (140, 100), flags=cv2.INTER_NEAREST)
    geometry = source_bar_geometry(tilted, 11, 13)
    assert geometry is not None
    assert abs(geometry["roll_degrees"] + 10) < 1
    assert all(
        abs(v["roll_degrees"] - geometry["roll_degrees"]) <= 3 for v in geometry["supporting_views"]
    )
    source_quad = np.asarray(geometry["polygon"]) - [11, 13]
    inverse = cv2.invertAffineTransform(matrix)
    mapped = cv2.transform(np.asarray([source_quad], np.float32), inverse)[0]
    assert mapped.min(0) == pytest.approx([18, 28], abs=2)
    assert mapped.max(0) == pytest.approx([99, 63], abs=2)


@pytest.mark.parametrize("positions", [(0, 30, 60), (15,), (15, 16, 17)])
def test_clipped_single_or_merged_source_groups_do_not_authorize_a_row(positions):
    assert source_bar_geometry(bar_row(positions=positions)) is None


def test_two_rows_remain_ambiguous_even_when_their_columns_align():
    first = bar_row(shape=(130, 120), y=10)
    second = bar_row(shape=(130, 120), y=80)
    image = np.maximum(first, second)
    assert source_bar_geometry(image) is None


def test_glare_or_color_only_background_cannot_supply_complete_bar_groups():
    white = np.full((80, 120, 3), 230, np.uint8)
    assert source_bar_geometry(white) is None
    image = np.full((80, 120, 3), (20, 10, 220), np.uint8)
    image[30:34, 15:95] = (150, 120, 255)
    assert source_bar_geometry(image) is None


def test_disagreeing_threshold_groups_are_rejected_instead_of_selecting_a_view():
    image = bar_row()
    image[20:52, 33:45] = (45, 30, 255)
    assert source_bar_geometry(image) is None


def test_green_view_uses_all_source_levels_including_bright_non_red_pixels():
    image = np.full((20, 40, 3), (30, 20, 220), np.uint8)
    # This bright source bar is outside the red-only chroma subset. Its
    # successive levels must remain distinguishable after the monotonic map.
    levels = np.arange(100, 200, 5, dtype=np.uint8)
    image[:, 20:, 1] = levels
    image[:, 20:, 0] = levels + 20
    image[:, 20:, 2] = 210
    before = image.copy()
    region = {"polygon": [[0, 0], [39, 0], [39, 19], [0, 19]], "size": [40, 20]}
    views, _, diagnostic = _red_ocr_views(image, region)
    assert diagnostic["status"] == "prepared"
    assert diagnostic["green_range_basis"] == "complete_source_polygon_min_max"
    assert diagnostic["green_low"] == 20 and diagnostic["green_high"] == 195
    assert diagnostic["red_source_pixel_count"] < diagnostic["source_pixel_count"]
    green = views[1][1][0, 20:, 0]
    assert len(np.unique(green)) == len(levels)
    assert np.all(np.diff(green.astype(int)) < 0)
    assert np.array_equal(image, before)


def test_channel_range_ignores_pixels_outside_the_authorized_source_quad():
    image = np.full((20, 60, 3), (250, 250, 255), np.uint8)
    image[:, 10:50] = (40, 30, 220)
    image[:, 30:50, 1] = 110
    region = {"polygon": [[10, 0], [49, 0], [49, 19], [10, 19]], "size": [40, 20]}
    views, _, diagnostic = _red_ocr_views(image, region)
    assert views and diagnostic["green_low"] == 30 and diagnostic["green_high"] == 110


def test_numeric_source_fallback_preserves_both_receipts_and_original_coordinates(monkeypatch):
    source_checks = []
    monkeypatch.setattr(
        digit_regions, "_native_off_geometry", lambda *args: source_checks.append(1)
    )
    raw = {"status": "completed", "lines": [], "request_id": "original"}
    receipts = []

    def predict(image, *args):
        receipt = {
            "status": "completed",
            "request_id": f"numeric-source-{len(receipts)}",
            "lines": [line("888", [0, 0, image.shape[1] - 1, image.shape[0] - 1])],
            "actual_model_invocation": True,
            "ocr_finished_at": f"finished-{len(receipts)}",
        }
        receipts.append(receipt)
        return receipt

    output = refine_digits(predict, bar_row(), raw, 100, 200, recover_red=True)
    assert len(receipts) == 2 and source_checks == [1]
    assert output["panel_ocr"] == raw
    assert output["lines"][0]["text"] == "888"
    assert np.allclose(output["lines"][0]["polygon"], output["digit_region"]["polygon"], atol=0.001)
    assert output["digit_region"]["method"] == "source_red_bar_row_geometry_v1"
    assert output["red_display_off_shape"] == {}
    assert not output["red_display_retry_gate"]["off_state_allowed"]
    assert output["red_display_retry_gate"]["orientation_degrees"] == [0]
    assert output["red_display_retry_gate"]["native_directions"] == []
    assert output["red_display_off_basis"] == {
        "status": "unavailable",
        "reason": "numeric_source_geometry_only",
        "native_off_measured": False,
    }
    assert [attempt["ocr"] for attempt in output["red_display_ocr_attempts"]] == receipts
    assert output["ocr_finished_at"] == "finished-1"


def test_numeric_source_fallback_cannot_authorize_off_or_rotated_direction(monkeypatch):
    monkeypatch.setattr(digit_regions, "_native_off_geometry", lambda *args: None)
    receipts = []

    def predict(image, *args):
        receipt = {
            "lines": [line("OFF", [0, 0, image.shape[1] - 1, image.shape[0] - 1])],
            "request_id": str(len(receipts)),
        }
        receipts.append(receipt)
        return receipt

    output = refine_digits(
        predict, bar_row(), {"status": "completed", "lines": []}, recover_red=True
    )
    assert len(receipts) == 2 and not output["lines"] and not output.get("display_state")
    assert output["red_display_orientation"]["status"] == "unconfirmed"
    assert output["red_display_retry_gate"]["orientation_degrees"] == [0]
    assert all(
        attempt["orientation_degrees"] == 0 for attempt in output["red_display_ocr_attempts"]
    )
    assert [attempt["ocr"] for attempt in output["red_display_ocr_attempts"]] == receipts


def test_numeric_source_fallback_never_supplies_a_third_vote_for_real_conflict(monkeypatch):
    monkeypatch.setattr(digit_regions, "_native_off_geometry", lambda *args: None)
    raw = {"status": "completed", "lines": [line("95", [15, 20, 78, 32])], "request_id": "original"}
    calls = []

    def predict(image, *args):
        calls.append(1)
        return {
            "lines": [line("55", [0, 0, image.shape[1] - 1, image.shape[0] - 1])],
            "request_id": "warped",
        }

    output = refine_digits(
        predict, bar_row(), raw, recover_red=True, require_native_conflict_confirmation=True
    )
    assert calls == [1] and not output["lines"]
    assert output["panel_ocr"] == raw
    assert output["digit_consistency"]["candidates"] == ["95", "55"]
    assert output["digit_consistency"]["native_confirmation_required"]
    assert (
        output["red_display_retry_gate"]["skip_reason"]
        == "independent_native_confirmation_required"
    )
    assert not output.get("red_display_ocr_attempts")
    assert not output["red_display_off_basis"]["native_off_measured"]


def test_existing_valid_numeric_keeps_the_original_refinement_call_budget(monkeypatch):
    monkeypatch.setattr(digit_regions, "_native_off_geometry", lambda *args: None)
    raw = {"status": "completed", "lines": [line("888", [15, 20, 78, 32])]}
    calls = []
    output = refine_digits(
        lambda image, *args: calls.append(1)
        or {"lines": [line("888", [0, 0, image.shape[1] - 1, image.shape[0] - 1])]},
        bar_row(),
        raw,
        recover_red=True,
        require_native_conflict_confirmation=True,
    )
    assert calls == [1] and output["lines"][0]["text"] == "888"
    assert "red_display_ocr_attempts" not in output


def test_digit_regions_preserves_geometry_import_compatibility():
    for name in ("extract_digits", "red_display_geometry", "source_bar_geometry", "_red_ocr_views"):
        assert getattr(digit_regions, name) is getattr(red_geometry, name)


def test_panel_pipeline_cannot_promote_numeric_geometry_off_retry_to_formal_state(monkeypatch):
    from field_recognition.recognition import led_digits

    monkeypatch.setattr(digit_regions, "_native_off_geometry", lambda *args: None)
    monkeypatch.setattr(led_digits, "verify_digits", lambda image, local, *args: local)
    receipts = []

    def predict(image, *args):
        receipt = {
            "status": "completed",
            "request_id": f"panel-actual-{len(receipts)}",
            "lines": []
            if not receipts
            else [line("OFF", [0, 0, image.shape[1] - 1, image.shape[0] - 1])],
            "actual_model_invocation": True,
            "ocr_model_call_count": 1,
            "ocr_model_call_ms": 1.0,
            "ocr_model_lock_wait_ms": 0.0,
            "ocr_model_prepare_ms": 0.0,
            "ocr_model_call_scope": "synthetic-test",
        }
        receipts.append(receipt)
        return receipt

    core = {
        "panel_detector": SimpleNamespace(
            enabled=lambda: True,
            predict=lambda image, **kwargs: {
                "status": "completed",
                "boxes": [dict(class_id=0, instrument_id=A, confidence=0.8, xyxy=[0, 0, 120, 80])],
            },
        ),
        "now": lambda: "test-time",
        "ocr_state": {"engine": "mock-only"},
        "OCR_DEVICE": "cpu",
        "predict_panel": predict,
    }
    result = panel_regions.predict(core, bar_row(), allowed_instrument_ids=[A])
    assert len(receipts) == result["ocr_model_call_count"] == 3
    assert result["lines"] == [] and result["ocr_model_call_ms"] == 3.0
    region = result["panel_regions"][0]
    assert region["display_state"] is None and region["measurement_name"] is None
    assert region["local_ocr"]["panel_ocr"] == receipts[0]
    assert [
        attempt["ocr"] for attempt in region["local_ocr"]["red_display_ocr_attempts"]
    ] == receipts[1:]
    assert region["local_ocr"]["red_display_off_basis"]["reason"] == "numeric_source_geometry_only"
    assert panel_regions.needs_fallback(result) == "no_text"
