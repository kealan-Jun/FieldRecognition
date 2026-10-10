"""Pure source geometry and pixel-preserving OCR view preparation."""

import numpy as np
import pytest

from field_recognition.recognition import digit_regions, red_geometry
from field_recognition.recognition.red_geometry import _red_ocr_views


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


def test_digit_regions_preserves_geometry_import_compatibility():
    for name in ("extract_digits", "red_display_geometry", "_red_ocr_views"):
        assert getattr(digit_regions, name) is getattr(red_geometry, name)


def test_unsaturated_views_use_source_channels_without_mutating_or_drawing_pixels():
    source = np.zeros((20, 40, 3), np.uint8)
    source[:, :, 0] = np.arange(40, dtype=np.uint8) + 20
    source[:, :, 1] = np.arange(40, dtype=np.uint8) * 3 + 10
    source[:, :, 2] = 220
    original = source.copy()
    region = {"polygon": [[0, 0], [39, 0], [39, 19], [0, 19]], "size": [40, 20]}
    views, transform, diagnostic = _red_ocr_views(source, region)
    assert diagnostic["status"] == "prepared" and len(views) == 2
    assert [name for name, pixels in views] == ["max_bg_negative", "green_contrast_negative"]
    assert np.array_equal(source, original)
    assert np.array_equal(views[0][1][:, :, 0], 255 - np.maximum(source[:, :, 0], source[:, :, 1]))
    low, high = source[:, :, 1].min(), source[:, :, 1].max()
    expected = 255 - np.rint(
        np.clip((source[:, :, 1].astype(float) - low) * 255 / (high - low), 0, 255)
    ).astype(np.uint8)
    assert np.array_equal(views[1][1][:, :, 0], expected)
    assert all(
        np.array_equal(pixels[:, :, 0], pixels[:, :, 1])
        and np.array_equal(pixels[:, :, 1], pixels[:, :, 2])
        for _, pixels in views
    )
    assert not np.array_equal(views[0][1], views[1][1])
    assert np.allclose(transform, np.eye(3))
    # R saturation changes neither source view while the same red window remains.
    source[:, :, 2] = 255
    saturated_views, _, _ = _red_ocr_views(source, region)
    assert all(np.array_equal(a[1], b[1]) for a, b in zip(views, saturated_views))


def test_low_source_contrast_or_identical_rendered_views_cannot_supply_two_votes():
    region = {"polygon": [[0, 0], [79, 0], [79, 43], [0, 43]], "size": [80, 44]}
    flat = np.full((44, 80, 3), (20, 20, 100), np.uint8)
    views, _, diagnostic = _red_ocr_views(flat, region)
    assert not views and diagnostic["status"] == "insufficient_source_contrast"
    source = np.full((44, 80, 3), (0, 0, 100), np.uint8)
    source[8:36, 8:72] = (190, 190, 255)
    # A thumbnail that loses every glyph renders both channel methods as the
    # same white pixels. Two algorithms are not two pieces of image evidence.
    views, _, diagnostic = _red_ocr_views(source, dict(region, size=[2, 2]))
    assert not views and diagnostic["status"] == "duplicate_preprocessed_views"


def test_unsupported_multiple_red_rows_do_not_authorize_extra_ocr_calls():
    source = np.zeros((100, 120, 3), np.uint8)
    source[10:35, 15:105] = (60, 40, 255)
    source[45:70, 15:105] = (60, 40, 255)
    original = source.copy()
    assert red_geometry.red_display_geometry(source) is None
    raw = {"status": "completed", "lines": [], "request_id": "original"}
    output = digit_regions.refine_digits(
        lambda *args: pytest.fail("No authorized source row for an OCR retry"),
        source,
        raw,
        recover_red=True,
        require_native_conflict_confirmation=True,
    )
    assert output["panel_ocr"] == raw
    assert not output["lines"] and not output.get("display_state")
    assert not output.get("red_display_ocr_attempts")
    assert np.array_equal(source, original)
