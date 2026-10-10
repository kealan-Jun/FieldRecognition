"""Pure source-pixel OFF evidence; these synthetic cases are not OCR accuracy."""

import json

import cv2
import numpy as np
import pytest

from field_recognition.recognition import digit_regions


def _off_panel():
    image = np.full((44, 80, 3), (5, 5, 100), np.uint8)
    for x, character in ((5, "O"), (30, "F"), (55, "F")):
        width, height, thickness, top = 18, 32, 4, 5
        bars = [
            (x, top, width, thickness),
            (x, top + height // 2 - thickness // 2, width, thickness),
            (x, top, thickness, height),
        ]
        if character == "O":
            bars += [
                (x + width - thickness, top, thickness, height),
                (x, top + height - thickness, width, thickness),
            ]
        for left, y, w, h in bars:
            image[y : y + h, left : left + w] = (190, 190, 255)
        if character == "O":
            image[19:23, x + thickness : x + width - thickness] = (5, 5, 100)
    return image


def _narrow_off_panel():
    """A 7px F's 3px left stem intersects a fixed bottom-center probe."""
    image = np.full((34, 46, 3), (5, 5, 100), np.uint8)
    for x, character in ((5, "O"), (17, "F"), (29, "F")):
        width, height, top = 7, 15, 5
        thickness = 2 if character == "O" else 3
        image[top : top + thickness, x : x + width] = (190, 190, 255)
        image[top : top + height, x : x + thickness] = (190, 190, 255)
        if character == "O":
            image[top : top + height, x + width - thickness : x + width] = (190, 190, 255)
            image[top + height - thickness : top + height, x : x + width] = (190, 190, 255)
        else:
            image[top + 6 : top + 9, x : x + width] = (190, 190, 255)
    return image


def test_thick_narrow_f_uses_measured_arms_without_rewriting_raw_probe_patterns():
    image = _narrow_off_panel()
    before = image.copy()
    result = digit_regions._off_shape(image)

    assert result["status"] == "consistent"
    assert result["method"] == "native_red_off_bars_v3"
    assert all(view["patterns"] == ["1111110", "1001111", "1001111"] for view in result["views"])
    assert result["views"][0]["threshold"] != result["views"][1]["threshold"]
    for view in result["views"]:
        for proof in view["glyph_proofs"][1:]:
            assert proof["basis"] == "native_source_f_bar_topology"
            assert proof["source_f_bars"]["arm_rows"] == [[0, 3], [6, 9]]
            assert proof["source_f_bars"]["gap_rows"] == [[3, 6], [9, 15]]
            assert proof["source_f_bars"]["right_counterevidence_levels"] == [0.0, 0.0]
    assert np.array_equal(image, before)
    json.dumps(result)


def test_separate_short_reflection_rows_do_not_join_the_three_source_glyphs():
    image = _narrow_off_panel()
    image[1:3, 23:26] = (190, 190, 255)
    image[27:29, 9:34] = (190, 190, 255)
    before = image.copy()
    result = digit_regions._off_shape(image)

    assert result["status"] == "consistent"
    for view in result["views"]:
        assert view["group_count"] == 3
        assert view["row_grouping"]["row_bounds"] == [5, 20]
        assert view["row_grouping"]["excluded_row_pixel_count"] == 56
        assert all(box[1] == 5 and box[3] == 15 for box in view["glyph_boxes"])
    assert np.array_equal(image, before)


@pytest.mark.parametrize("mutation", ["8", "G", "E", "P", "right_stem", "missing_left"])
def test_source_row_grouping_keeps_inside_glyph_counterevidence(mutation):
    image = _narrow_off_panel()
    image[27:29, 9:34] = (190, 190, 255)
    if mutation == "8":
        image[11:14, 5:12] = (190, 190, 255)
    elif mutation == "G":
        image[11:14, 8:12] = (190, 190, 255)
    elif mutation == "E":
        image[17:20, 17:24] = (190, 190, 255)
    elif mutation in {"P", "right_stem"}:
        image[8:11, 22:24] = (190, 190, 255)
        if mutation == "right_stem":
            image[14:20, 22:24] = (190, 190, 255)
    else:
        image[8:11, 17:20] = (5, 5, 100)
    result = digit_regions._off_shape(image)

    assert result["status"] == "inconclusive"
    assert not any(view["matches_off"] for view in result["views"])


def test_two_complete_off_rows_cannot_be_selected_as_one_native_direction():
    panel = _narrow_off_panel()
    image = np.full((74, 46, 3), (5, 5, 100), np.uint8)
    image[:34] = panel
    image[40:] = panel
    result = digit_regions._off_shape(image)

    assert result["status"] == "inconclusive"
    assert all(view["row_grouping"]["eligible_row_count"] == 2 for view in result["views"])
    assert all(view["row_grouping"]["excluded_row_pixel_count"] == 0 for view in result["views"])


def test_narrow_single_glyph_does_not_supply_an_off_direction():
    image = _narrow_off_panel()[:, :15]
    result = digit_regions._off_shape(image)
    assert result["status"] == "inconclusive"
    assert all(view["group_count"] == 1 for view in result["views"])


@pytest.mark.parametrize("dtype", [np.float32, np.uint16, np.int16])
def test_non_native_pixel_dtype_cannot_be_wrapped_into_off_evidence(dtype):
    image = _narrow_off_panel().astype(dtype)
    if dtype == np.float32:
        image[0, 0] = np.nan
    result = digit_regions._off_shape(image)
    assert result["reason"] == "invalid_source_image"
    assert result["views"] == []


@pytest.mark.parametrize("texts", [("OFF", "GFF"), ("OFF", "100"), ("0FF", "0FF")])
def test_complete_native_proof_cannot_replace_two_actual_matching_off_ocr_views(texts):
    image = _narrow_off_panel()
    assert digit_regions._native_off_geometry(image, 0, 0)[3] == [0]
    calls = []

    def predict(view, x, y):
        text = texts[len(calls)]
        calls.append(text)
        h, w = view.shape[:2]
        return {
            "lines": [
                {
                    "text": text,
                    "confidence": 0.9,
                    "polygon": [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]],
                }
            ]
        }

    output = digit_regions._red_recovery(predict, image, {"lines": []}, 0, 0)

    assert calls == list(texts)
    assert "display_state" not in output
    assert output["lines"] == []
    assert output["red_display_retry_gate"]["native_directions"] == [0]
    assert [
        attempt["ocr"]["lines"][0]["text"] for attempt in output["red_display_ocr_attempts"]
    ] == list(texts)


def test_single_actual_off_ocr_view_still_cannot_complete_native_proof(monkeypatch):
    image = _narrow_off_panel()
    calls = []

    def one_view(source, region):
        w, h = region["size"]
        return [("only_actual_view", np.zeros((h, w, 3), np.uint8))], np.eye(3), {}

    def predict(view, x, y):
        calls.append(1)
        h, w = view.shape[:2]
        return {
            "lines": [
                {
                    "text": "OFF",
                    "confidence": 0.9,
                    "polygon": [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]],
                }
            ]
        }

    monkeypatch.setattr(digit_regions, "_red_ocr_views", one_view)
    output = digit_regions._red_recovery(predict, image, {"lines": []}, 0, 0)

    assert calls == [1]
    assert "display_state" not in output
    assert output["red_display_retry_gate"]["native_directions"] == [0]
    assert len(output["red_display_ocr_attempts"]) == 1


@pytest.mark.parametrize("gray", [190, 245])
def test_neutral_frame_does_not_join_off_glyphs_or_set_their_thresholds(gray):
    panel = _off_panel()
    image = np.full((74, 130, 3), gray, np.uint8)
    image[15:59, 25:105] = panel
    before = image.copy()

    result = digit_regions._off_shape(image)
    reference = digit_regions._off_shape(panel)

    assert result["status"] == reference["status"] == "consistent"
    assert result["source_threshold"] == reference["source_threshold"]
    assert result["source_contrast"] == reference["source_contrast"]
    assert result["red_source_pixel_count"] == panel.shape[0] * panel.shape[1]
    assert [view["group_count"] for view in result["views"]] == [3, 3]
    assert all(view["patterns"] == ["1111110", "1000111", "1000111"] for view in result["views"])
    assert result["views"][0]["threshold"] != result["views"][1]["threshold"]
    assert np.array_equal(image, before)
    json.dumps(result)


def test_rotated_off_requires_both_thresholds_in_the_same_native_direction():
    image = cv2.rotate(_off_panel(), cv2.ROTATE_180)
    assert digit_regions._off_shape(image)["status"] == "inconclusive"
    restored = digit_regions._off_shape(cv2.rotate(image, cv2.ROTATE_180))
    assert restored["status"] == "consistent"
    assert all(view["matches_off"] for view in restored["views"])


@pytest.mark.parametrize("groups", [0, 1, 2, 4])
def test_failed_column_grouping_has_two_actual_threshold_receipts(groups):
    image = np.full((44, 110, 3), (5, 5, 100), np.uint8)
    for index in range(groups):
        image[5:37, 5 + index * 25 : 23 + index * 25] = (190, 190, 255)
    # For zero foreground, low constant pixels have no meaningful contrast.
    result = digit_regions._off_shape(image)
    assert result["status"] == "inconclusive"
    if groups == 0:
        assert result["views"] == []
        assert result["reason"] == "insufficient_source_contrast"
    else:
        assert len(result["views"]) == 2
        assert [view["group_count"] for view in result["views"]] == [groups, groups]
        assert all(view["reason"] == "glyph_group_count" for view in result["views"])
        assert not any(view["matches_off"] for view in result["views"])


def test_one_matching_threshold_cannot_supply_native_off_direction(monkeypatch):
    image = _off_panel()
    # Isolate the consensus gate: one actual mask includes a faint middle bar,
    # while the second excludes it. No OCR text or duplicated view is involved.
    image[19:23, 34:48] = (70, 70, 255)
    monkeypatch.setattr(digit_regions.cv2, "threshold", lambda *args: (5.0, None))
    result = digit_regions._off_shape(image)

    assert [view["matches_off"] for view in result["views"]] == [True, False]
    assert result["views"][1]["reason"] == "off_pattern_mismatch"
    assert result["status"] == "inconclusive"


def test_scattered_complete_glyphs_are_not_one_off_row():
    panel = _off_panel()
    image = np.full((94, 80, 3), (5, 5, 100), np.uint8)
    image[:44, :25] = panel[:, :25]
    image[50:94, 25:] = panel[:, 25:]
    result = digit_regions._off_shape(image)

    assert result["status"] == "inconclusive"
    assert all(view["patterns"] == ["1111110", "1000111", "1000111"] for view in result["views"])
    assert all(view["reason"] == "glyph_row_alignment" for view in result["views"])
    assert not any(view["matches_off"] for view in result["views"])


@pytest.mark.parametrize("mutation", ["extra_o_bar", "missing_f_bar", "clipped_o", "small"])
def test_complete_pattern_and_native_pixel_gates_reject_counterevidence(mutation):
    image = _off_panel()
    if mutation == "extra_o_bar":
        image[19:23, 9:19] = (190, 190, 255)  # 8/F/F is not O/F/F.
    elif mutation == "missing_f_bar":
        image[5:9, 59:73] = (5, 5, 100)
    elif mutation == "clipped_o":
        image = image[:, 5:]
    else:
        image = cv2.resize(image, (20, 8), interpolation=cv2.INTER_NEAREST)

    result = digit_regions._off_shape(image)
    assert result["status"] == "inconclusive"
    assert not any(view["matches_off"] for view in result["views"])
    if mutation == "clipped_o":
        assert all(view["reason"] == "source_glyph_clipped" for view in result["views"])


@pytest.mark.parametrize(
    "image", [np.zeros((44, 80), np.uint8), np.full((44, 80, 3), 245, np.uint8)]
)
def test_non_red_source_has_no_off_evidence(image):
    result = digit_regions._off_shape(image)
    assert result["status"] == "inconclusive"
    assert result["views"] == []
    assert result["reason"] in {"invalid_source_image", "insufficient_red_source_pixels"}


def test_saved_preclip_size_cannot_enlarge_source_pixels_for_off_proof(monkeypatch):
    image = cv2.resize(_off_panel(), (20, 8), interpolation=cv2.INTER_NEAREST)
    geometry = {
        "polygon": [[11, 13], [30, 13], [30, 20], [11, 20]],
        "native_size": [80, 44],
    }
    monkeypatch.setattr(digit_regions, "red_display_geometry", lambda *args: geometry)
    measured, ocr_region, shapes, directions = digit_regions._native_off_geometry(image, 11, 13)

    assert measured is geometry
    assert ocr_region == {"polygon": [[0, 0], [19, 0], [19, 7], [0, 7]], "size": [80, 44]}
    assert directions == []
    assert all(shape["sampling"]["source_size"] == [19, 7] for shape in shapes.values())
    assert all(shape["status"] == "inconclusive" for shape in shapes.values())


def test_native_geometry_keeps_ocr_coordinates_and_independent_direction_receipts():
    image = _off_panel()
    geometry, region, shapes, directions = digit_regions._native_off_geometry(image, 17, 23)

    assert directions == [0]
    assert region["size"] == geometry["native_size"]
    assert np.allclose(np.asarray(region["polygon"]) + [17, 23], geometry["polygon"])
    assert shapes[0]["status"] == "consistent"
    assert shapes[180]["status"] == "inconclusive"
    assert all(shape["sampling"]["method"] == "source_quad_nearest" for shape in shapes.values())
    assert all(
        shape["sampling"]["source_size"][axis] <= geometry["native_size"][axis]
        for shape in shapes.values()
        for axis in (0, 1)
    )
    json.dumps(shapes)
