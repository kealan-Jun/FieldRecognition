import copy

import cv2
import numpy as np
import pytest

from field_recognition.recognition.panel_layout import resolve_type_boxes, roles_for_boxes
from field_recognition.recognition.panel_recovery import (
    detect,
    photo_pair_context,
    photo_weak_pair_context,
    source_box,
    window_pixels,
)


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


@pytest.mark.parametrize("window_height", [8, 25])
def test_photo_closeup_maps_two_scale_consensus_back_to_source(window_height):
    image = np.full((100, 100, 3), (20, 20, 180), np.uint8)
    calls = []
    target = [[10, 30, 35, 30 + window_height], [55, 30, 85, 30 + window_height]]

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
    assert result["diagnostics"]["photo_closeup"] == {
        "executed_scales": [0.35, 0.45],
        "skipped_scales": [],
    }
    for got, want in zip(result["boxes"], target):
        assert got["localization_method"] == "photo_closeup_scale_v1"
        assert np.allclose(got["xyxy"], want, atol=1e-4)
        assert got["supporting_views"] == [0.35, 0.45]


ANCHOR = [80, 90, 110, 115]
PARTNER = [116, 90, 146, 115]
CONTEXT_ORIGIN = [0, 0]


def panel_image(windows=(ANCHOR, PARTNER)):
    """Synthetic dark windows with sparse red segments; no real evaluation photo."""
    image = np.full((210, 260, 3), 200, np.uint8)
    for x1, y1, x2, y2 in windows:
        image[y1:y2, x1:x2] = 35
        for offset in (4, 16):
            cv2.rectangle(image, (x1 + offset, y1 + 4), (x1 + offset + 7, y2 - 5), (0, 0, 210), 1)
            cv2.line(image, (x1 + offset, y1 + 12), (x1 + offset + 7, y1 + 12), (0, 0, 210), 1)
    return image


def dense_red_window():
    """A dim red surround, actual dark gaps and native synthetic OFF strokes."""
    image = np.full((100, 100, 3), 35, np.uint8)
    image[28:67, 28:72] = (0, 0, 90)
    image[35:60, 39:45] = 35
    image[35:60, 51:57] = 35
    cv2.putText(
        image, "OFF", (35, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 220), 2, lineType=cv2.LINE_8
    )
    return image


def test_dense_red_window_separates_native_light_from_dim_hue_without_changing_dark_evidence():
    image = dense_red_window()
    before = image.copy()
    evidence = window_pixels(cv2.cvtColor(image, cv2.COLOR_BGR2HSV), [35, 35, 65, 60])
    assert np.array_equal(image, before)
    assert evidence["pixel_basis"] == "source_bright_red_otsu"
    assert evidence["red_fraction"] > 0.5 and evidence["inner_red_fraction"] < 0.9
    assert 0.06 <= evidence["bright_red_fraction"] <= 0.5
    assert evidence["red_hue_also_dark_count"] > 0
    assert evidence["dark_fraction"] >= 0.25 and evidence["value_contrast"] >= 35
    assert evidence["bright_native_fill"] < 0.9
    assert evidence["bright_native_span"] == [24, 10]
    assert evidence["evidence_bbox"] == [28, 28, 72, 67]


@pytest.mark.parametrize(
    "failure",
    [
        "solid",
        "gradient_label",
        "bright_block",
        "reflection",
        "narrow_light",
        "clipped",
        "tiny",
        "no_dark_window",
        "low_contrast",
    ],
)
def test_dense_native_red_gate_keeps_solid_gradient_reflection_and_source_geometry_counterevidence(
    failure,
):
    image = dense_red_window()
    coords = [35, 35, 65, 60]
    if failure == "solid":
        image[28:67, 28:72] = (0, 0, 220)
    elif failure == "gradient_label":
        image[35:60, 35:65] = np.stack(
            [
                np.zeros((25, 30), np.uint8),
                np.zeros((25, 30), np.uint8),
                np.tile(np.linspace(0, 255, 30, dtype=np.uint8), (25, 1)),
            ],
            axis=2,
        )
    elif failure == "bright_block":
        image = dense_red_window()
        image[35:60, 35:65] = 35
        image[39:55, 38:59] = (0, 0, 220)
    elif failure == "reflection":
        image[35:60, 35:65] = 35
        image[39:55, 38:59] = 255  # Specular white light supplies no red source strokes.
    elif failure == "narrow_light":
        image[35:60, 35:65] = 35
        image[35:60, 49:51] = (0, 0, 220)
    elif failure == "clipped":
        coords = [0, 35, 30, 60]
    elif failure == "tiny":
        coords = [35, 35, 65, 42]
    elif failure == "no_dark_window":
        image[28:67, 28:72] = (0, 0, 220)
        image[35:60, 39:45] = 255
        image[35:60, 51:57] = 255
    elif failure == "low_contrast":
        image[(image == 35).all(axis=2)] = 90
        image[(image == (0, 0, 220)).all(axis=2)] = (0, 0, 110)
    assert window_pixels(cv2.cvtColor(image, cv2.COLOR_BGR2HSV), coords) is None


def test_original_sparse_window_evidence_return_remains_exactly_unchanged():
    evidence = window_pixels(cv2.cvtColor(panel_image(), cv2.COLOR_BGR2HSV), ANCHOR)
    assert set(evidence) == {
        "red_fraction",
        "inner_red_fraction",
        "dark_fraction",
        "value_contrast",
        "dark_threshold",
        "evidence_bbox",
    }
    assert evidence["red_fraction"] <= 0.5 and "pixel_basis" not in evidence


def weak_votes(coords=PARTNER):
    return [
        box(0.19, coords=coords) | {"view": "gamma_scale"},
        box(0.16, coords=coords) | {"view": "gamma_tilt_6"},
    ]


def run_weak_context(image, responses, *, candidates=None, imgsz=960, existing_boxes=()):
    calls = []

    def infer(pixels, size, threshold):
        calls.append((pixels.copy(), size, threshold))
        assert len(calls) <= 2, "weak context must never authorize a third detector vote"
        return {"boxes": copy.deepcopy(responses[len(calls) - 1]), "speed_ms": {"inference": 2.0}}

    result = photo_weak_pair_context(
        image,
        cv2.cvtColor(image, cv2.COLOR_BGR2HSV),
        weak_votes() if candidates is None else candidates,
        infer,
        imgsz,
        0.25,
        existing_boxes,
    )
    return result, calls


def test_weak_photo_proposal_is_replaced_by_two_actual_complete_pair_views():
    image = panel_image()
    before = image.copy()
    first = [box(0.4, coords=ANCHOR), box(0.5, coords=PARTNER)]
    second = [box(0.6, coords=ANCHOR), box(0.7, coords=PARTNER)]
    result, calls = run_weak_context(image, [first, second])
    assert np.array_equal(image, before) and len(calls) == 2
    assert [c[1:] for c in calls] == [(640, 0.25), (960, 0.25)]
    assert result["speed_ms"] == {"inference": 4.0}
    assert [b["xyxy"] for b in result["boxes"]] == [ANCHOR, PARTNER]
    assert [b["confidence"] for b in result["boxes"]] == [0.6, 0.7]
    assert len(roles_for_boxes(result["boxes"])) == 2
    for b in result["boxes"]:
        assert b["localization_method"] == "photo_weak_pair_context_v1"
        assert b["supporting_views"] == ["weak_pair_gamma_medium", "weak_pair_gamma_wide"]
        assert b["pair_proposal_xyxy"] == PARTNER
        assert set(b["pair_window_pixels"]) == {"left", "right"}
        assert "instrument_id" not in b and "source_orientation" not in b
    receipt = result["diagnostics"]
    assert receipt["proposal_only"] and receipt["proposal"]["confidence"] == 0.19
    assert [p["eligible_window_count"] for p in receipt["passes"]] == [2, 2]
    assert receipt["passes"][1]["model_candidates"] == [
        b | {"source_xyxy": b["xyxy"]} for b in second
    ]
    assert receipt["reason"] == "recovered"


@pytest.mark.parametrize(
    "failure",
    [
        "no_votes",
        "one_vote",
        "same_view",
        "wrong_class",
        "ambiguous_cue",
        "no_pixels",
        "clipped_cue",
        "nonfinite_cue",
    ],
)
def test_weak_photo_context_requires_unique_two_view_source_proposal_before_new_calls(failure):
    image = panel_image()
    candidates = weak_votes()
    if failure == "no_votes":
        candidates = []
    elif failure == "one_vote":
        candidates.pop()
    elif failure == "same_view":
        candidates[1]["view"] = candidates[0]["view"]
    elif failure == "wrong_class":
        for candidate in candidates:
            candidate["class_id"] = 1
    elif failure == "ambiguous_cue":
        candidates += weak_votes(ANCHOR)
    elif failure == "no_pixels":
        image[:] = 220
    elif failure == "clipped_cue":
        candidates = weak_votes([0, 90, 30, 115])
        image = panel_image(([0, 90, 30, 115],))
    elif failure == "nonfinite_cue":
        candidates = weak_votes([float("nan"), 90, float("inf"), 115])
    result, calls = run_weak_context(image, [], candidates=candidates)
    assert not calls and not result["boxes"] and not result["diagnostics"]["attempted"]


@pytest.mark.parametrize(
    "failure",
    [
        "single_window",
        "single_complete_view",
        "low_confidence",
        "overlap",
        "adjacent_row",
        "distant_device",
        "multiple_devices",
        "solid_red",
        "different_pair",
        "duplicate_context",
        "existing_region",
        "not_reconfirmed",
        "nonfinite_confidence",
    ],
)
def test_weak_context_never_assigns_roles_without_two_complete_unambiguous_source_pairs(failure):
    left, right = list(ANCHOR), list(PARTNER)
    image = panel_image()
    responses = [[box(coords=left), box(coords=right)]] * 2
    kwargs = {}
    expected_status = None
    if failure == "single_window":
        responses = [[box(coords=right)]] * 2
        expected_status = "incomplete_pair"
    elif failure == "single_complete_view":
        responses[1] = [box(coords=right)]
    elif failure == "low_confidence":
        responses = [[box(0.24, coords=left), box(coords=right)]] * 2
    elif failure == "overlap":
        left = [92, 90, 122, 115]
        image = panel_image((left, right))
        responses = [[box(coords=left), box(coords=right)]] * 2
        expected_status = "pair_layout_unconfirmed"
    elif failure == "adjacent_row":
        left = [80, 60, 110, 85]
        image = panel_image((left, right))
        responses = [[box(coords=left), box(coords=right)]] * 2
        expected_status = "pair_layout_unconfirmed"
    elif failure == "distant_device":
        left = [30, 90, 60, 115]
        image = panel_image((left, right))
        responses = [[box(coords=left), box(coords=right)]] * 2
        expected_status = "pair_window_gap_exceeded"
    elif failure == "multiple_devices":
        extra = [44, 90, 74, 115]
        image = panel_image((extra, left, right))
        responses = [[box(coords=extra), box(coords=left), box(coords=right)]] * 2
        expected_status = "ambiguous_pair_windows"
    elif failure == "solid_red":
        image[90:115, 80:110] = (0, 0, 210)
    elif failure == "different_pair":
        other = [152, 90, 182, 115]
        image = panel_image((left, right, other))
        responses[1] = [box(coords=right), box(coords=other)]
    elif failure == "duplicate_context":
        kwargs["imgsz"] = 640
    elif failure == "existing_region":
        kwargs["existing_boxes"] = [box(class_id=1, coords=left)]
    elif failure == "not_reconfirmed":
        left, right = [44, 90, 74, 115], list(ANCHOR)
        image = panel_image((left, right, PARTNER))
        responses = [[box(coords=left), box(coords=right)]] * 2
        expected_status = "proposal_window_not_reconfirmed"
    elif failure == "nonfinite_confidence":
        responses = [[box(float("inf"), coords=left), box(coords=right)]] * 2
    result, calls = run_weak_context(image, responses, **kwargs)
    assert len(calls) == (2 if failure in {"single_complete_view", "different_pair"} else 1)
    assert not result["boxes"] and result["diagnostics"]["reason"] == "no_complete_pair_consensus"
    if expected_status:
        assert all(p["pair_status"] == expected_status for p in result["diagnostics"]["passes"])


def test_weak_context_maps_two_actual_crops_and_preserves_native_window_provenance():
    image = np.full((740, 900, 3), 200, np.uint8)
    image[350:375, 330:396] = panel_image()[90:115, 80:146]
    candidates = weak_votes(FRACTIONAL_ANCHOR)
    before = copy.deepcopy(candidates)
    calls = []

    def infer(pixels, size, threshold):
        origin = [152, 153] if not calls else [34, 54]
        calls.append((pixels.shape[:2], size, threshold))
        return {
            "boxes": [
                box(coords=[v - origin[i % 2] for i, v in enumerate(coords)])
                for coords in (FRACTIONAL_ANCHOR, FRACTIONAL_PARTNER)
            ]
        }

    result = photo_weak_pair_context(
        image, cv2.cvtColor(image, cv2.COLOR_BGR2HSV), candidates, infer, 960, 0.25
    )
    assert candidates == before
    assert calls == [((419, 386), 640, 0.25), ((617, 622), 960, 0.25)]
    assert [b["xyxy"] for b in result["boxes"]] == [FRACTIONAL_ANCHOR, FRACTIONAL_PARTNER]
    assert [p["view_to_source_matrix"] for p in result["diagnostics"]["passes"]] == [
        [[1, 0, 152], [0, 1, 153]],
        [[1, 0, 34], [0, 1, 54]],
    ]


@pytest.mark.parametrize("photo", [False, True])
def test_weak_context_integrates_only_in_photos_and_preserves_all_actual_forward_costs(photo):
    image = panel_image()
    # The actual rotation envelope contains the native dark window surround.
    image[84:121, 74:152] = 35
    image[90:115, 80:146] = panel_image()[90:115, 80:146]
    for offset in (4, 16):
        cv2.rectangle(image, (116 + offset, 94), (116 + offset + 7, 110), (0, 0, 210), 2)
    before = image.copy()
    calls = []

    def infer(pixels, size, threshold):
        calls.append((pixels.copy(), size, threshold))
        index = len(calls)
        if index == 2:
            boxes = [box(0.19, coords=PARTNER)]
        elif index == 3:
            matrix = cv2.getRotationMatrix2D((130, 105), 6, 1.0)
            x1, y1, x2, y2 = PARTNER
            points = cv2.transform(
                np.asarray([[[x1, y1], [x2, y1], [x2, y2], [x1, y2]]], np.float32), matrix
            )[0]
            boxes = [
                box(
                    0.16,
                    coords=[
                        float(points[:, 0].min()),
                        float(points[:, 1].min()),
                        float(points[:, 0].max()),
                        float(points[:, 1].max()),
                    ],
                )
            ]
        elif index in {5, 6}:
            boxes = [box(coords=ANCHOR), box(coords=PARTNER)]
        else:
            boxes = []
        return {"boxes": boxes, "speed_ms": {"inference": 2.0}}

    result = detect(image, infer, photo=photo)
    count = 6 if photo else 4
    assert np.array_equal(image, before)
    assert len(calls) == result["recovery"]["passes"] == count
    assert result["speed_ms"]["inference"] == count * 2
    assert result["diagnostics"]["raw_candidate_count"] == (6 if photo else 2)
    assert result["recovery"]["recovered_count"] == (2 if photo else 0)
    if photo:
        assert [b["xyxy"] for b in result["boxes"]] == [ANCHOR, PARTNER]
        assert (
            "photo_closeup" not in result["diagnostics"]
            and "photo_pair_context" not in result["diagnostics"]
        )
    else:
        assert not result["boxes"] and "photo_weak_pair_context" not in result["diagnostics"]


def test_weak_pair_failure_does_not_promote_single_window_or_start_strong_anchor_recovery():
    image = panel_image()
    calls = []

    def infer(pixels, size, threshold):
        calls.append((size, threshold))
        if len(calls) in {2, 3}:
            # Both views use the actual source mapping; a proposal can never
            # become an accepted anchor even after two strong single windows.
            xyxy = PARTNER
            if len(calls) == 3:
                matrix = cv2.getRotationMatrix2D((130, 105), 6, 1.0)
                xyxy = source_box(PARTNER, cv2.invertAffineTransform(matrix), 260, 210)
            boxes = [box(0.19, coords=xyxy)]
        elif len(calls) == 5:
            boxes = [box(coords=PARTNER)]
        else:
            boxes = []
        return {"boxes": boxes, "speed_ms": {"inference": 2.0}}

    image[84:121, 74:152] = 35
    image[90:115, 80:146] = panel_image()[90:115, 80:146]
    for offset in (4, 16):
        cv2.rectangle(image, (116 + offset, 94), (116 + offset + 7, 110), (0, 0, 210), 2)
    result = detect(image, infer, photo=True)
    assert len(calls) == result["recovery"]["passes"] == 9
    assert result["speed_ms"]["inference"] == 18.0
    assert not result["boxes"] and result["recovery"]["recovered_count"] == 0
    assert "photo_pair_context" not in result["diagnostics"]
    pair = result["diagnostics"]["photo_weak_pair_context"]
    assert len(pair["passes"]) == 1 and pair["reason"] == "no_complete_pair_consensus"
    assert all(p["pair_status"] == "incomplete_pair" for p in pair["passes"])


def context_box(coords=PARTNER, score=0.6, class_id=0):
    return box(score, class_id, [v - CONTEXT_ORIGIN[i % 2] for i, v in enumerate(coords)])


def run_pair(image, local_responses=None, *, primary=None, photo=True, imgsz=960):
    responses = [primary or [box(0.3, coords=ANCHOR)], [], [], []]
    responses.extend(local_responses or [[context_box()], [context_box()]])
    # Empty legacy closeup passes isolate the new recovery contract on negatives.
    responses.extend([[] for _ in range(8)])
    calls = []
    context_pixels = cv2.LUT(image, np.uint8((np.arange(256) / 255.0) ** 2.2 * 255))
    local_call = 0

    def infer(pixels, size, threshold):
        nonlocal local_call
        calls.append((pixels.copy(), size, threshold))
        if len(calls) <= 4:
            boxes = responses[len(calls) - 1]
        elif pixels.shape == context_pixels.shape and np.array_equal(pixels, context_pixels):
            boxes = responses[4 + local_call]
            local_call += 1
        else:
            boxes = []
        return {"boxes": copy.deepcopy(boxes), "speed_ms": {"inference": 2.0}}

    return detect(image, infer, photo=photo, imgsz=imgsz), calls


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
    assert partner["localization_method"] == "photo_pair_context_v2"
    assert partner["supporting_views"] == ["pair_gamma_medium", "pair_gamma_wide"]
    assert partner["pair_anchor_xyxy"] == ANCHOR
    assert set(partner["pair_window_pixels"]) == {"anchor", "partner"}
    assert all(p["dark_fraction"] >= 0.25 for p in partner["pair_window_pixels"].values())
    assert partner["pair_window_pixels"]["partner"]["evidence_bbox"] == [109, 83, 153, 122]
    assert "instrument_id" not in partner and "source_orientation" not in partner
    assert len(roles_for_boxes(result["boxes"])) == 2
    assert calls[4][1:] == (640, 0.25) and calls[5][1:] == (960, 0.25)
    assert calls[4][0].shape[:2] == (210, 260)
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
        ([[context_box([116, -25, 146, 0])]] * 2, "pair_context_out_of_bounds"),
        ([[context_box([116, 65, 146, 90])]] * 2, "pair_layout_unconfirmed"),
        ([[context_box([116, 90, 276, 115])]] * 2, "pair_context_out_of_bounds"),
    ],
)
def test_photo_pair_requires_two_full_confidence_views_and_bounded_geometry(responses, reason):
    result, calls = run_pair(panel_image(), responses)
    assert len(result["boxes"]) == 1
    expected = 10 if reason == "insufficient_supporting_views" else 9
    assert len(calls) == result["recovery"]["passes"] == expected
    assert reason in {b["reason"] for b in result["diagnostics"]["excluded_candidates"]}
    pair = result["diagnostics"]["photo_pair_context"]
    if expected == 9:
        assert [p["view"] for p in pair["passes"]] == ["pair_gamma_medium"]
        assert [p["view"] for p in pair["skipped_views"]] == [
            "pair_gamma_wide",
            "pair_gamma_medium_canonical",
            "pair_gamma_wide_canonical",
        ]
        assert pair["skipped_views"][1]["reason"] == "duplicate_context_views"
    else:
        assert len(pair["passes"]) == 2
        assert pair["skipped_views"][0]["view"] == "pair_gamma_wide_canonical"
        assert pair["skipped_views"][0]["reason"] == "duplicate_context_views"


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


def test_photo_pair_padding_cannot_turn_solid_red_patch_on_black_desk_into_a_window():
    image = panel_image()
    image[80:125, 112:156] = 35
    x1, y1, x2, y2 = PARTNER
    image[y1:y2, x1:x2] = (0, 0, 210)
    result, _ = run_pair(image)
    assert len(result["boxes"]) == 1
    assert "pair_window_evidence_missing" in {
        b["reason"] for b in result["diagnostics"]["excluded_candidates"]
    }


def test_photo_pair_does_not_choose_an_anchor_among_multiple_primary_windows():
    result, calls = run_pair(panel_image(), primary=[box(coords=ANCHOR), box(coords=PARTNER)])
    assert len(calls) == 4 and len(result["boxes"]) == 2
    assert "photo_pair_context" not in result["diagnostics"]


def test_photo_pair_identical_clipped_contexts_and_model_scales_are_not_two_views():
    result, calls = run_pair(panel_image(), imgsz=640)
    assert len(result["boxes"]) == 1 and len(calls) == 9
    assert "duplicate_context_views" in {
        b["reason"] for b in result["diagnostics"]["excluded_candidates"]
    }


def test_photo_pair_rejects_same_class_window_on_a_distant_device_in_the_same_row():
    distant = [180, 90, 210, 115]
    image = panel_image((ANCHOR, distant))
    responses = [[context_box(distant)]] * 2
    result, _ = run_pair(image, responses)
    assert len(result["boxes"]) == 1
    assert not roles_for_boxes(result["boxes"])
    rejected = [
        b
        for b in result["diagnostics"]["excluded_candidates"]
        if b["reason"] == "pair_window_gap_exceeded"
    ]
    assert len(rejected) == 1
    assert all(
        b["pair_horizontal_gap"] == 70 and b["pair_horizontal_gap_limit"] == 30 for b in rejected
    )


@pytest.mark.parametrize("fractional", [False, True])
def test_photo_pair_maps_each_chassis_context_back_to_the_same_source_window(fractional):
    anchor = [330.2, 350.2, 359.8, 374.8] if fractional else [330, 350, 360, 375]
    partner = [366.2, 350.2, 395.8, 374.8] if fractional else [366, 350, 396, 375]
    image = np.full((740, 900, 3), 200, np.uint8)
    small = panel_image()
    image[350:375, 330:396] = small[90:115, 80:146]
    calls = []
    origins = [[152, 153], [34, 54]] if fractional else [[150, 150], [30, 50]]

    def infer(pixels, size, threshold):
        calls.append((pixels.shape[:2], size, threshold))
        if len(calls) == 1:
            return {"boxes": [box(0.3, coords=anchor)]}
        if len(calls) <= 4:
            return {"boxes": []}
        origin = origins[len(calls) - 5]
        return {"boxes": [box(coords=[v - origin[i % 2] for i, v in enumerate(partner)])]}

    result = detect(image, infer, photo=True)
    assert len(calls) == 6
    assert [b["xyxy"] for b in result["boxes"]] == [anchor, partner]
    shapes = [(419, 386), (617, 622)] if fractional else [(425, 390), (625, 630)]
    assert calls[4:] == [(shapes[0], 640, 0.25), (shapes[1], 960, 0.25)]
    pair = result["diagnostics"]["photo_pair_context"]
    assert pair["context_anchor_bbox"] == [330, 350, 360, 375]
    assert pair["context_coordinate_basis"] == "continuous_source_box"
    assert "fallback_trigger" not in pair and "skipped_views" not in pair
    pixels_evidence = result["boxes"][1]["pair_window_pixels"]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    assert pixels_evidence == {
        "anchor": window_pixels(hsv, anchor),
        "partner": window_pixels(hsv, partner),
    }
    assert result["boxes"][1]["pair_anchor_xyxy"] == anchor
    passes = pair["passes"]
    contexts = (
        [[152, 153, 538, 572], [34, 54, 656, 671]]
        if fractional
        else [[150, 150, 540, 575], [30, 50, 660, 675]]
    )
    assert [p["context_bbox"] for p in passes] == contexts
    assert all(p["context_basis"] == "continuous_source_box" for p in passes)


def test_photo_pair_does_not_replace_another_primary_class_region():
    primary = [box(0.3, coords=ANCHOR), box(class_id=1, coords=PARTNER)]
    result, calls = run_pair(panel_image(), primary=primary)
    assert [b["class_id"] for b in result["boxes"]] == [0, 1]
    assert len(calls) == result["recovery"]["passes"] == 9
    pair = result["diagnostics"]["photo_pair_context"]
    assert pair["reason"] == "no_eligible_partner_first_view"
    assert pair["excluded_candidates"][0]["reason"] == "duplicate_existing_region"
    assert len(pair["passes"]) == 1


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
    assert len(calls) == 8
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


def test_empty_first_context_skips_second_model_view_without_fabricating_a_receipt():
    result, calls = run_pair(panel_image(), [[], [context_box()]])
    assert len(result["boxes"]) == 1
    assert len(calls) == result["recovery"]["passes"] == 9
    assert result["speed_ms"]["inference"] == 18.0
    assert result["diagnostics"]["raw_candidate_count"] == 1
    pair = result["diagnostics"]["photo_pair_context"]
    assert pair["passes"] == [
        {
            "view": "pair_gamma_medium",
            "candidate_count": 0,
            "context_bbox": [0, 0, 260, 210],
            "imgsz": 640,
            "context_basis": "continuous_source_box",
            "eligible_partner_count": 0,
        }
    ]
    assert pair["reason"] == "no_eligible_partner_first_view"
    assert [p["view"] for p in pair["skipped_views"]] == [
        "pair_gamma_wide",
        "pair_gamma_medium_canonical",
        "pair_gamma_wide_canonical",
    ]
    assert pair["skipped_views"][1]["same_as_view"] == "pair_gamma_medium"
    assert not any(
        p["view"] == "pair_gamma_wide" or p["view"].startswith("closeup_0.45/")
        for p in result["diagnostics"]["passes"]
    )


def scaled_boxes(coords, scale):
    return [box(coords=[50 + (value - 50) * scale for value in xyxy]) for xyxy in coords]


@pytest.mark.parametrize("failure", ["empty", "blue_only", "wrong_color", "duplicate", "outside"])
def test_closeup_skips_second_scale_when_first_has_no_eligible_new_a_window(failure):
    image = np.full((100, 100, 3), (20, 20, 180), np.uint8)
    targets = [[10, 30, 35, 55], [55, 30, 85, 55]]
    primary = []
    first_scale = []
    if failure == "blue_only":
        first_scale = [box(class_id=1)]
    elif failure == "wrong_color":
        image[:] = 220
        first_scale = scaled_boxes(targets, 0.35) + [box(class_id=1)]
    elif failure == "duplicate":
        primary = [box(coords=targets[0])]
        first_scale = scaled_boxes([targets[0], targets[0]], 0.35) + [box(class_id=1)]
    elif failure == "outside":
        first_scale = scaled_boxes([[-25, 30, 0, 55], [100, 30, 125, 55]], 0.35)
        first_scale.append(box(class_id=1))
    calls = []

    def infer(pixels, size, threshold):
        calls.append((size, threshold))
        index = len(calls)
        if index == 1:
            boxes = primary
        elif index == 5:
            boxes = first_scale
        elif index <= 8:
            boxes = []
        else:
            pytest.fail("second scale must not execute without eligible first-scale evidence")
        return {"boxes": copy.deepcopy(boxes), "speed_ms": {"inference": 2.0}}

    result = detect(image, infer, photo=True)
    expected_passes = 8 if failure in {"empty", "blue_only"} else 5
    assert len(calls) == result["recovery"]["passes"] == expected_passes
    assert result["speed_ms"]["inference"] == expected_passes * 2
    assert result["recovery"]["recovered_count"] == 0
    assert [b["xyxy"] for b in result["boxes"]] == [b["xyxy"] for b in primary]
    assert result["diagnostics"]["photo_closeup"] == {
        "executed_scales": [0.35],
        "skipped_scales": [{"scale": 0.45, "reason": "no_eligible_new_window_first_scale"}],
    }
    assert not any(p["view"].startswith("closeup_0.45/") for p in result["diagnostics"]["passes"])
    expected_raw = len(primary) + len(first_scale)
    assert result["diagnostics"]["raw_candidate_count"] == expected_raw
    if failure == "outside":
        assert {b["reason"] for b in result["diagnostics"]["excluded_candidates"]} == {
            "region_too_small"
        }


def test_closeup_first_scale_alone_never_outputs_a_recovered_window():
    image = np.full((100, 100, 3), (20, 20, 180), np.uint8)
    first_scale = scaled_boxes([[10, 30, 35, 55], [55, 30, 85, 55]], 0.35)
    calls = []

    def infer(pixels, size, threshold):
        calls.append(1)
        boxes = first_scale + [box(class_id=1)] if len(calls) == 5 else []
        return {"boxes": copy.deepcopy(boxes), "speed_ms": {"inference": 2.0}}

    result = detect(image, infer, photo=True)
    assert len(calls) == result["recovery"]["passes"] == 9
    assert not result["boxes"]
    assert result["diagnostics"]["photo_closeup"] == {
        "executed_scales": [0.35, 0.45],
        "skipped_scales": [],
    }
    assert all(
        b["reason"] == "insufficient_supporting_views"
        for b in result["diagnostics"]["excluded_candidates"]
    )


def test_photo_early_stops_do_not_cancel_blue_display_recovery():
    image = np.full((100, 100, 3), 200, np.uint8)
    image[30:65, 35:75] = (180, 20, 20)
    calls = []

    def infer(pixels, size, threshold):
        calls.append(1)
        boxes = [box(class_id=1)] if 2 <= len(calls) <= 4 else []
        return {"boxes": copy.deepcopy(boxes)}

    result = detect(image, infer, photo=True)
    assert len(calls) == result["recovery"]["passes"] == 8
    assert len(result["boxes"]) == 1 and result["boxes"][0]["class_id"] == 1
    assert len(result["boxes"][0]["supporting_views"]) == 3


def test_two_primary_red_windows_still_allow_b_recovery_with_four_video_forwards():
    image = panel_image()
    primary = [box(coords=ANCHOR), box(coords=PARTNER)]
    result, calls = run_pair(image, primary=primary, photo=False)
    assert len(calls) == result["recovery"]["passes"] == 4
    assert [b["xyxy"] for b in result["boxes"]] == [ANCHOR, PARTNER]


FRACTIONAL_ANCHOR = [330.2, 350.2, 359.8, 374.8]
FRACTIONAL_PARTNER = [366.2, 350.2, 395.8, 374.8]
FRACTIONAL_CONTEXTS = {
    "pair_gamma_medium": ([152, 153, 538, 572], 640),
    "pair_gamma_wide": ([34, 54, 656, 671], 960),
    "pair_gamma_medium_canonical": ([150, 150, 540, 575], 640),
    "pair_gamma_wide_canonical": ([30, 50, 660, 675], 960),
}


def run_fractional_context(responses, *, partner_pixels=True, existing_boxes=()):
    image = np.full((740, 900, 3), 200, np.uint8)
    image[350:375, 330:396] = panel_image()[90:115, 80:146]
    if not partner_pixels:
        image[350:375, 366:396] = 220
    calls = []

    def infer(pixels, size, threshold):
        for name, (coords, view_size) in FRACTIONAL_CONTEXTS.items():
            left, top, right, bottom = coords
            if view_size == size and pixels.shape[:2] == (bottom - top, right - left):
                expected = cv2.LUT(
                    image[top:bottom, left:right], np.uint8((np.arange(256) / 255) ** 2.2 * 255)
                )
                assert np.array_equal(pixels, expected)
                assert threshold == 0.25 and name not in calls
                calls.append(name)
                returned = [
                    b
                    | {"xyxy": [v - (left if i % 2 == 0 else top) for i, v in enumerate(b["xyxy"])]}
                    for b in responses.get(name, [])
                ]
                return {"boxes": returned, "speed_ms": {"inference": 2.0}}
        pytest.fail("unexpected or repeated context")

    anchor = box(0.3, coords=FRACTIONAL_ANCHOR)
    before = copy.deepcopy(anchor)
    result = photo_pair_context(
        image, cv2.cvtColor(image, cv2.COLOR_BGR2HSV), anchor, infer, 960, 0.25, existing_boxes
    )
    assert anchor == before
    return result, calls


def test_failed_float_medium_can_use_two_distinct_canonical_views_with_three_actual_forwards():
    partner = box(coords=FRACTIONAL_PARTNER)
    result, calls = run_fractional_context(
        {"pair_gamma_medium_canonical": [partner], "pair_gamma_wide_canonical": [partner]}
    )
    assert calls == [
        "pair_gamma_medium",
        "pair_gamma_medium_canonical",
        "pair_gamma_wide_canonical",
    ]
    assert result["speed_ms"] == {"inference": 6.0}
    assert result["box"]["xyxy"] == FRACTIONAL_PARTNER
    assert result["box"]["pair_anchor_xyxy"] == FRACTIONAL_ANCHOR
    assert result["box"]["supporting_views"] == [
        "pair_gamma_medium_canonical",
        "pair_gamma_wide_canonical",
    ]
    receipt = result["diagnostics"]
    assert receipt["fallback_trigger"] == "float_medium_no_eligible_partner"
    assert [p["context_basis"] for p in receipt["passes"]] == [
        "continuous_source_box",
        "source_pixel_floor_ceil",
        "source_pixel_floor_ceil",
    ]
    assert [p["eligible_partner_count"] for p in receipt["passes"]] == [0, 1, 1]
    assert [p["view"] for p in receipt["skipped_views"]] == ["pair_gamma_wide"]


def test_failed_float_wide_can_be_corroborated_by_one_canonical_wide_without_replacing_success():
    partner = box(coords=FRACTIONAL_PARTNER)
    result, calls = run_fractional_context(
        {"pair_gamma_medium": [partner], "pair_gamma_wide_canonical": [partner]}
    )
    assert calls == ["pair_gamma_medium", "pair_gamma_wide", "pair_gamma_wide_canonical"]
    assert result["box"]["xyxy"] == FRACTIONAL_PARTNER
    assert result["box"]["supporting_views"] == ["pair_gamma_medium", "pair_gamma_wide_canonical"]
    assert result["diagnostics"]["fallback_trigger"] == "float_wide_no_eligible_partner"


def test_canonical_medium_without_second_actual_support_never_outputs_a_partner():
    result, calls = run_fractional_context(
        {"pair_gamma_medium_canonical": [box(coords=FRACTIONAL_PARTNER)]}
    )
    assert len(calls) == len(result["diagnostics"]["passes"]) == 3
    assert not result["box"] and result["diagnostics"]["reason"] == "no_pair_consensus"
    assert "insufficient_supporting_views" in {
        b["reason"] for b in result["diagnostics"]["excluded_candidates"]
    }


def test_two_failed_medium_views_skip_both_wide_views_and_report_only_actual_calls():
    result, calls = run_fractional_context(
        {
            "pair_gamma_wide": [box(coords=FRACTIONAL_PARTNER)],
            "pair_gamma_wide_canonical": [box(coords=FRACTIONAL_PARTNER)],
        }
    )
    assert calls == ["pair_gamma_medium", "pair_gamma_medium_canonical"]
    assert not result["box"] and result["speed_ms"]["inference"] == 4.0
    assert [p["view"] for p in result["diagnostics"]["skipped_views"]] == [
        "pair_gamma_wide",
        "pair_gamma_wide_canonical",
    ]


def test_eligible_contrary_float_wide_is_not_outvoted_by_a_canonical_third_view():
    other = [294.2, 350.2, 323.8, 374.8]
    # The synthetic source only contains the right partner, so create the left
    # pixel evidence separately without reading or inferring a field value.
    image = np.full((740, 900, 3), 200, np.uint8)
    image[350:375, 294:324] = panel_image((ANCHOR,))[90:115, 80:110]
    image[350:375, 330:396] = panel_image()[90:115, 80:146]
    calls = []

    def infer(pixels, size, threshold):
        calls.append(size)
        assert len(calls) <= 2, "contrary eligible views must not authorize a third vote"
        origin = [152, 153] if len(calls) == 1 else [34, 54]
        source = FRACTIONAL_PARTNER if len(calls) == 1 else other
        return {"boxes": [box(coords=[v - origin[i % 2] for i, v in enumerate(source)])]}

    result = photo_pair_context(
        image,
        cv2.cvtColor(image, cv2.COLOR_BGR2HSV),
        box(coords=FRACTIONAL_ANCHOR),
        infer,
        960,
        0.25,
    )
    assert calls == [640, 960] and not result["box"]
    assert [p["eligible_partner_count"] for p in result["diagnostics"]["passes"]] == [1, 1]
    assert "fallback_trigger" not in result["diagnostics"]


@pytest.mark.parametrize(
    "failure", ["low_confidence", "wrong_class", "layout", "overlap", "pixels"]
)
def test_canonical_fallback_keeps_all_source_partner_gates(failure):
    candidate = box(coords=FRACTIONAL_PARTNER)
    existing = ()
    if failure == "low_confidence":
        candidate["confidence"] = 0.24
    elif failure == "wrong_class":
        candidate["class_id"] = 1
    elif failure == "layout":
        candidate["xyxy"] = [366.2, 325.2, 395.8, 349.8]
    elif failure == "overlap":
        existing = [box(class_id=1, coords=FRACTIONAL_PARTNER)]
    result, calls = run_fractional_context(
        {
            "pair_gamma_medium_canonical": [candidate],
            "pair_gamma_wide_canonical": [box(coords=FRACTIONAL_PARTNER)],
        },
        partner_pixels=failure != "pixels",
        existing_boxes=existing,
    )
    assert calls == ["pair_gamma_medium", "pair_gamma_medium_canonical"]
    assert not result["box"]
    reasons = {
        "low_confidence": "below_recovery_confidence",
        "wrong_class": "wrong_pair_class",
        "layout": "pair_layout_unconfirmed",
        "overlap": "duplicate_existing_region",
        "pixels": "pair_window_evidence_missing",
    }
    assert reasons[failure] in {b["reason"] for b in result["diagnostics"]["excluded_candidates"]}
