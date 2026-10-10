import copy

import cv2
import numpy as np
import pytest

from field_recognition.recognition.panel_layout import resolve_type_boxes, roles_for_boxes
from field_recognition.recognition.panel_recovery import detect, source_box


def box(score=0.6, class_id=0, coords=None):
    return {"class_id": class_id, "confidence": score, "xyxy": coords or [35, 30, 75, 65]}


def run_rescue(image, responses):
    calls = []

    def infer(pixels, size, threshold):
        calls.append((pixels.copy(), size, threshold))
        return {"boxes": copy.deepcopy(responses[len(calls) - 1]), "speed_ms": {"inference": 2.0}}

    return detect(image, infer), calls


def test_complete_primary_detection_needs_one_pass_and_keeps_original_boxes():
    original = [box(coords=[0, 0, 20, 20]), box(coords=[30, 0, 50, 20]), box(class_id=1)]
    result, calls = run_rescue(np.zeros((100, 100, 3), np.uint8), [original])
    assert len(calls) == 1 and not result["recovery"]["attempted"]
    assert [r["xyxy"] for r in result["boxes"]] == [b["xyxy"] for b in original]
    assert all(r["localization_method"] == "original" for r in result["boxes"])


def test_recovery_requires_two_views_preserves_input_and_deduplicates():
    image = np.full((100, 100, 3), (20, 20, 180), np.uint8)
    before = image.copy()
    result, calls = run_rescue(image, [[], [box()], [box(0.4)], [box(0.5)]])
    assert len(calls) == 4 and np.array_equal(before, image)
    assert len(result["boxes"]) == result["recovery"]["recovered_count"] == 1
    assert len(result["boxes"][0]["supporting_views"]) == 3
    assert result["boxes"][0]["xyxy"] == [35, 30, 75, 65]
    assert calls[1][1:] == (800, 0.1)
    assert result["speed_ms"]["inference"] == 8.0


@pytest.mark.parametrize(
    "responses,color",
    [
        ([[], [box()], [], []], (20, 20, 180)),  # one transformed view is not enough
        ([[], [box(0.15)], [box(0.15)], [box(0.15)]], (20, 20, 180)),  # threshold unchanged
        ([[], [box()], [box()], [box()]], (220, 220, 220)),  # sleeve/background
        ([[], [box(class_id=1)], [box(class_id=1)], [box(class_id=1)]], (20, 20, 180)),
    ],
)
def test_uncorroborated_low_score_or_wrong_display_color_is_rejected(responses, color):
    result, calls = run_rescue(np.full((100, 100, 3), color, np.uint8), responses)
    assert len(calls) == 4 and not result["boxes"]


def test_augmented_overlap_does_not_replace_existing_evidence_crop():
    result, _ = run_rescue(
        np.full((100, 100, 3), (20, 20, 180), np.uint8),
        [[box(0.3)], [box(0.9)], [box(0.8)], [box(0.7)]],
    )
    assert len(result["boxes"]) == 1 and result["boxes"][0]["localization_method"] == "original"
    assert result["boxes"][0]["confidence"] == 0.3


def test_affine_coordinates_return_to_source_and_clip_to_image():
    matrix = np.asarray([[1, 0, 12], [0, 1, -5]], np.float32)
    assert source_box([17, 15, 27, 25], matrix, 100, 100) == [5, 20, 15, 30]
    assert source_box([-10, -10, 120, 120], None, 100, 100) == [0, 0, 100, 100]


def test_photo_closeup_maps_two_scale_consensus_back_to_source():
    image = np.full((100, 100, 3), (20, 20, 180), np.uint8)
    calls = []
    target = [[10, 30, 35, 55], [55, 30, 85, 55]]

    def infer(pixels, size, threshold):
        calls.append(1)
        if len(calls) <= 4:
            return {"boxes": []}
        scale = 0.35 if len(calls) == 5 else 0.45
        boxes = [
            box(
                coords=[
                    50 + (x - 50) * scale,
                    50 + (y - 50) * scale,
                    50 + (ex - 50) * scale,
                    50 + (ey - 50) * scale,
                ]
            )
            for x, y, ex, ey in target
        ]
        # Complete primary detections skip the ordinary exposure passes.
        return {"boxes": boxes + [box(class_id=1)]}

    result = detect(image, infer, photo=True)
    assert len(calls) == 6
    assert len(result["boxes"]) == 2
    for got, want in zip(result["boxes"], target):
        assert got["localization_method"] == "photo_closeup_scale_v1"
        assert np.allclose(got["xyxy"], want, atol=1e-4)
        assert got["supporting_views"] == [0.35, 0.45]


ANCHOR = [80, 90, 110, 115]
PARTNER = [116, 90, 146, 115]
CONTEXT_ORIGIN = [20, 65]


def panel_image(windows=(ANCHOR, PARTNER)):
    """Synthetic dark windows with sparse red segments; no real evaluation photo."""
    image = np.full((210, 260, 3), 200, np.uint8)
    for x1, y1, x2, y2 in windows:
        image[y1:y2, x1:x2] = 35
        for offset in (4, 16):
            cv2.rectangle(image, (x1 + offset, y1 + 4), (x1 + offset + 7, y2 - 5), (0, 0, 210), 1)
            cv2.line(image, (x1 + offset, y1 + 12), (x1 + offset + 7, y1 + 12), (0, 0, 210), 1)
    return image


def context_box(coords=PARTNER, score=0.6, class_id=0):
    return box(score, class_id, [v - CONTEXT_ORIGIN[i % 2] for i, v in enumerate(coords)])


def run_pair(image, local_responses=None, *, primary=None, photo=True):
    responses = [primary or [box(0.3, coords=ANCHOR)], [], [], []]
    responses.extend(local_responses or [[context_box()], [context_box()]])
    # Empty legacy closeup passes isolate the new recovery contract on negatives.
    responses.extend([[] for _ in range(8)])
    calls = []

    def infer(pixels, size, threshold):
        calls.append((pixels.copy(), size, threshold))
        return {"boxes": copy.deepcopy(responses[len(calls) - 1]), "speed_ms": {"inference": 2.0}}

    return detect(image, infer, photo=photo), calls


def test_photo_pair_restores_missing_window_with_source_evidence_and_coordinates():
    image = panel_image()
    before = image.copy()
    result, calls = run_pair(image)
    assert np.array_equal(image, before)
    assert len(calls) == result["recovery"]["passes"] == 6
    assert result["speed_ms"]["inference"] == 12.0
    assert [b["xyxy"] for b in result["boxes"]] == [ANCHOR, PARTNER]
    assert result["boxes"][0]["confidence"] == 0.3  # original crop/evidence wins
    partner = result["boxes"][1]
    assert partner["localization_method"] == "photo_pair_context_v1"
    assert partner["supporting_views"] == ["pair_gamma", "pair_original"]
    assert partner["pair_anchor_xyxy"] == ANCHOR
    assert set(partner["pair_window_pixels"]) == {"anchor", "partner"}
    assert all(p["dark_fraction"] >= 0.25 for p in partner["pair_window_pixels"].values())
    assert partner["pair_window_pixels"]["partner"]["evidence_bbox"] == [112, 86, 150, 119]
    assert "instrument_id" not in partner and "source_orientation" not in partner
    assert len(roles_for_boxes(result["boxes"])) == 2
    assert calls[4][1:] == calls[5][1:] == (320, 0.25)
    assert calls[4][0].shape[:2] == (75, 150)
    assert result["diagnostics"]["photo_pair_context"]["reason"] == "recovered"


def test_photo_pair_window_evidence_uses_relative_contrast_without_expanding_ocr_boxes():
    image = panel_image()
    image[(image == 35).all(axis=2)] = 130  # exposure lifts the complete dark window
    image[(image == 200).all(axis=2)] = 255
    result, _ = run_pair(image)
    assert [b["xyxy"] for b in result["boxes"]] == [ANCHOR, PARTNER]
    pixels = result["boxes"][1]["pair_window_pixels"]["partner"]
    assert pixels["dark_threshold"] > 100 and pixels["value_contrast"] >= 35


@pytest.mark.parametrize(
    "responses,reason",
    [
        ([[context_box()], []], "insufficient_supporting_views"),
        ([[context_box(score=0.24)], [context_box(score=0.24)]], "below_recovery_confidence"),
        ([[context_box(class_id=1)], [context_box(class_id=1)]], "wrong_pair_class"),
        ([[context_box([116, 45, 146, 70])]] * 2, "pair_context_out_of_bounds"),
        ([[context_box([116, 65, 146, 90])]] * 2, "pair_layout_unconfirmed"),
        ([[context_box([116, 90, 176, 115])]] * 2, "pair_context_out_of_bounds"),
    ],
)
def test_photo_pair_requires_two_full_confidence_views_and_bounded_geometry(responses, reason):
    result, calls = run_pair(panel_image(), responses)
    assert len(result["boxes"]) == 1
    assert len(calls) == result["recovery"]["passes"] == 14
    assert reason in {b["reason"] for b in result["diagnostics"]["excluded_candidates"]}


@pytest.mark.parametrize("partner_fill", [None, (0, 0, 210), (220, 220, 220)])
def test_photo_pair_rejects_single_window_red_object_and_laptop_background(partner_fill):
    image = panel_image(windows=(ANCHOR,))
    if partner_fill is not None:
        x1, y1, x2, y2 = PARTNER
        image[y1:y2, x1:x2] = partner_fill
    result, _ = run_pair(image)
    assert len(result["boxes"]) == 1
    assert "pair_window_evidence_missing" in {
        b["reason"] for b in result["diagnostics"]["excluded_candidates"]
    }


def test_photo_pair_rejects_two_plausible_partners_instead_of_choosing_a_field():
    other = [44, 90, 74, 115]
    image = panel_image((other, ANCHOR, PARTNER))
    responses = [[context_box(other), context_box()]] * 2
    result, _ = run_pair(image, responses)
    assert len(result["boxes"]) == 1
    assert result["diagnostics"]["photo_pair_context"]["reason"] == "ambiguous_pair_partner"


def test_photo_pair_does_not_replace_another_primary_class_region():
    primary = [box(0.3, coords=ANCHOR), box(class_id=1, coords=PARTNER)]
    result, _ = run_pair(panel_image(), primary=primary)
    assert [b["class_id"] for b in result["boxes"]] == [0, 1]
    assert result["diagnostics"]["photo_pair_context"]["reason"] == "duplicate_existing_region"


@pytest.mark.parametrize("kind", ["red_object", "clipped_frame", "laptop"])
def test_photo_pair_requires_an_intact_class_zero_anchor(kind):
    image = panel_image()
    anchor = box(0.3, coords=ANCHOR)
    if kind == "red_object":
        x1, y1, x2, y2 = ANCHOR
        image[y1:y2, x1:x2] = (0, 0, 210)
    elif kind == "clipped_frame":
        anchor["xyxy"] = [0, 90, 30, 115]
        image = panel_image((anchor["xyxy"], PARTNER))
    else:
        anchor["class_id"] = 1
    result, calls = run_pair(image, primary=[anchor])
    assert len(result["boxes"]) == 1
    assert len(calls) == 12
    if kind == "laptop":
        assert "photo_pair_context" not in result["diagnostics"]
    else:
        assert (
            result["diagnostics"]["photo_pair_context"]["reason"]
            == "anchor_window_evidence_missing"
        )


def test_photo_pair_keeps_video_cost_and_requires_physical_instance_evidence():
    result, calls = run_pair(panel_image(), photo=False)
    assert len(calls) == 4 and len(result["boxes"]) == 1
    assert "photo_pair_context" not in result["diagnostics"]
    photo, _ = run_pair(panel_image())
    typed = [b | {"type_id": "synthetic_stirrer_type"} for b in photo["boxes"]]
    resolved = resolve_type_boxes(
        typed, {"bound_asset"}, lambda _: {"type_id": "synthetic_stirrer_type"}
    )
    assert len(resolved) == 2
    assert all(b["instrument_id"] is None for b in resolved)
    assert all(b["association_issue"] == "instance_evidence_required" for b in resolved)
