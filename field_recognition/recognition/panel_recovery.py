"""Bounded exposure/tilt recovery for the registered A/B display detector.

Enhanced images locate boxes only. OCR and evidence always use source pixels.
"""

import math

import cv2
import numpy as np


VERSION = "exposure_tilt_v1"
GAMMA_LUT = np.uint8((np.arange(256) / 255.0) ** 2.2 * 255)


def iou(a, b):
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union > 0 else 0


def source_box(box, matrix, width, height):
    x1, y1, x2, y2 = box
    if matrix is not None:
        corners = np.asarray([[[x1, y1], [x2, y1], [x2, y2], [x1, y2]]], np.float32)
        points = cv2.transform(corners, cv2.invertAffineTransform(matrix))[0]
        x1, y1, x2, y2 = (
            points[:, 0].min(),
            points[:, 1].min(),
            points[:, 0].max(),
            points[:, 1].max(),
        )
    return [max(0, float(x1)), max(0, float(y1)), min(width, float(x2)), min(height, float(y2))]


def has_display_color(hsv, box):
    """A red windows / B blue LCD context; reject recovered sleeve/desk boxes."""
    x1, y1, x2, y2 = box["xyxy"]
    pad = max(2, round(min(x2 - x1, y2 - y1) * 0.15))
    crop = hsv[
        max(0, math.floor(y1) - pad) : min(hsv.shape[0], math.ceil(y2) + pad),
        max(0, math.floor(x1) - pad) : min(hsv.shape[1], math.ceil(x2) + pad),
    ]
    if not crop.size:
        return False
    hue, saturation, value = cv2.split(crop)
    color = ((hue <= 12) | (hue >= 170)) if box["class_id"] == 0 else ((hue >= 90) & (hue <= 135))
    return float(np.mean(color & (saturation >= 30) & (value >= 35))) >= 0.06


def window_pixels(hsv, coords):
    """Independent sparse red glyph/dark-window evidence, not an OCR identity."""
    x1, y1, x2, y2 = [round(v) for v in coords]
    pad = max(2, math.ceil(min(x2 - x1, y2 - y1) * 0.25))
    if x1 - pad < 0 or y1 - pad < 0 or x2 + pad > hsv.shape[1] or y2 + pad > hsv.shape[0]:
        return None
    if min(x2 - x1, y2 - y1) < 8:
        return None
    # Detector boxes can tightly bound illuminated characters. Inspect the
    # surrounding window on original pixels without widening the OCR/evidence crop.
    crop = hsv[y1 - pad : y2 + pad, x1 - pad : x2 + pad]
    hue, saturation, value = cv2.split(crop)
    red = ((hue <= 12) | (hue >= 170)) & (saturation >= 30) & (value >= 35)
    inner_red_fraction = float(red[pad:-pad, pad:-pad].mean())
    # Relative contrast survives exposure changes; a uniform red object cannot
    # supply a distinct dark-window population merely by being bright or dim.
    contrast = float(np.percentile(value, 90) - np.percentile(value, 10))
    threshold, _ = cv2.threshold(value, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    red_fraction, dark_fraction = float(red.mean()), float((value <= threshold).mean())
    ys, xs = np.where(red)
    if (
        not 0.06 <= red_fraction <= 0.5
        # Padding must not dilute a solid red object into apparent glyphs.
        or inner_red_fraction >= 0.9
        or dark_fraction < 0.25
        or contrast < 35
        or not xs.size
        or np.ptp(xs) < 0.2 * crop.shape[1]
        or np.ptp(ys) < 0.25 * crop.shape[0]
    ):
        return None
    return {
        "red_fraction": round(red_fraction, 4),
        "inner_red_fraction": round(inner_red_fraction, 4),
        "dark_fraction": round(dark_fraction, 4),
        "value_contrast": round(contrast, 2),
        "dark_threshold": threshold,
        "evidence_bbox": [x1 - pad, y1 - pad, x2 + pad, y2 + pad],
    }


def photo_pair_context(image, hsv, anchor, infer, imgsz, confidence):
    """Two bounded model views corroborate one missing partner on source pixels."""
    from field_recognition.recognition.panel_layout import roles_for_boxes

    diagnostics = {"attempted": False, "passes": [], "excluded_candidates": []}
    result = {"box": None, "speed_ms": {}, "diagnostics": diagnostics}
    anchor_pixels = window_pixels(hsv, anchor["xyxy"])
    if not anchor_pixels or anchor["confidence"] < confidence:
        diagnostics["reason"] = "anchor_window_evidence_missing"
        return result
    x1, y1, x2, y2 = anchor["xyxy"]
    width, height = x2 - x1, y2 - y1
    diagnostics["attempted"] = True
    candidates = []
    view_signatures = {}
    # A character-only crop removes the chassis context learned by this model.
    # Two fields of view retain the device while exposing distinct model scales.
    for name, horizontal, vertical, size in (
        ("pair_gamma_medium", 6, 8, min(imgsz, 640)),
        ("pair_gamma_wide", 10, 12, imgsz),
    ):
        left = max(0, math.floor(x1 - horizontal * width))
        top = max(0, math.floor(y1 - vertical * height))
        right = min(image.shape[1], math.ceil(x2 + horizontal * width))
        bottom = min(image.shape[0], math.ceil(y2 + vertical * height))
        view_signatures[name] = (left, top, right, bottom, size)
        pixels = cv2.LUT(image[top:bottom, left:right], GAMMA_LUT)
        detection = infer(pixels, size, confidence)
        diagnostics["passes"].append(
            {
                "view": name,
                "candidate_count": len(detection["boxes"]),
                "context_bbox": [left, top, right, bottom],
                "imgsz": size,
            }
        )
        for key, value in detection.get("speed_ms", {}).items():
            result["speed_ms"][key] = result["speed_ms"].get(key, 0) + value
        for item in detection["boxes"]:
            box = dict(item, view=name)
            box["xyxy"] = [
                item["xyxy"][0] + left,
                item["xyxy"][1] + top,
                item["xyxy"][2] + left,
                item["xyxy"][3] + top,
            ]
            bx1, by1, bx2, by2 = box["xyxy"]
            gap = max(bx1 - x2, x1 - bx2, 0)
            gap_limit = max(width, bx2 - bx1)
            box.update(pair_horizontal_gap=gap, pair_horizontal_gap_limit=gap_limit)
            reason = (
                "wrong_pair_class"
                if box["class_id"] != anchor["class_id"]
                else "pair_context_out_of_bounds"
                if not (left <= bx1 < bx2 <= right and top <= by1 < by2 <= bottom)
                else "below_recovery_confidence"
                if box["confidence"] < confidence
                else "duplicate_region"
                if iou(box["xyxy"], anchor["xyxy"]) >= 0.4
                else "pair_window_size_mismatch"
                if not (0.6 <= (bx2 - bx1) / width <= 1.7 and 0.65 <= (by2 - by1) / height <= 1.5)
                # Wider chassis context can include another device in the same row.
                else "pair_window_gap_exceeded"
                if gap > gap_limit
                else "pair_layout_unconfirmed"
                if len(roles_for_boxes([anchor, box])) != 2
                else "pair_window_evidence_missing"
                if not (pixels_evidence := window_pixels(hsv, box["xyxy"]))
                else None
            )
            if reason:
                diagnostics["excluded_candidates"].append(box | {"reason": reason})
            else:
                candidates.append(box | {"window_pixels": pixels_evidence})
    consensus = []
    for box in sorted(candidates, key=lambda b: b["confidence"], reverse=True):
        if any(iou(box["xyxy"], b["xyxy"]) >= 0.5 for b in consensus):
            continue
        support = sorted({b["view"] for b in candidates if iou(box["xyxy"], b["xyxy"]) >= 0.5})
        if len(support) != 2:
            diagnostics["excluded_candidates"].append(
                box | {"reason": "insufficient_supporting_views"}
            )
            continue
        if len({view_signatures[name] for name in support}) < 2:
            diagnostics["excluded_candidates"].append(box | {"reason": "duplicate_context_views"})
            continue
        consensus.append(box | {"supporting_views": support})
    diagnostics["reason"] = (
        "recovered"
        if len(consensus) == 1
        else "ambiguous_pair_partner"
        if consensus
        else "no_pair_consensus"
    )
    if len(consensus) == 1:
        box = consensus[0]
        result["box"] = {k: v for k, v in box.items() if k not in {"view", "window_pixels"}} | {
            "localization_method": "photo_pair_context_v2",
            "pair_anchor_xyxy": list(anchor["xyxy"]),
            "pair_window_pixels": {"anchor": anchor_pixels, "partner": box["window_pixels"]},
            "pair_layout_basis": "model_horizontal_pair_contract",
        }
    return result


def detect(image, infer, *, imgsz=960, confidence=0.25, photo=False):
    """Four video forwards; photos allow two local and eight closeup forwards."""
    height, width = image.shape[:2]
    primary = infer(image, imgsz, confidence)
    accepted = [dict(b, localization_method="original") for b in primary["boxes"]]
    recovery = {"version": VERSION, "attempted": False, "passes": 1, "recovered_count": 0}
    diagnostics = {
        "raw_candidate_count": len(primary["boxes"]),
        "raw_count_basis": "model_returned_boxes_after_model_threshold_and_nms",
        "passes": [{"view": "original", "candidate_count": len(primary["boxes"])}],
        "excluded_candidates": [],
    }

    def exclude(box, reason):
        diagnostics["excluded_candidates"].append(
            {k: box[k] for k in ("class_id", "xyxy", "confidence", "view") if k in box}
            | {"reason": reason}
        )

    def output():
        diagnostics["quality_filtered_count"] = len(accepted)
        return {
            "boxes": accepted,
            "speed_ms": speed,
            "recovery": recovery,
            "diagnostics": diagnostics,
        }

    speed = dict(primary.get("speed_ms", {}))
    # Existing full detections win over augmented duplicates, preserving crops.
    if sum(b["class_id"] == 0 for b in accepted) >= 2 and any(b["class_id"] == 1 for b in accepted):
        return output()
    recovery["attempted"] = True
    dark = cv2.LUT(image, GAMMA_LUT)
    views = [("gamma_scale", dark, min(imgsz, 800), None)]
    for angle in (6, -6):
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
        pixels = cv2.warpAffine(dark, matrix, (width, height), borderMode=cv2.BORDER_REPLICATE)
        views.append((f"gamma_tilt_{angle}", pixels, imgsz, matrix))
    candidates = []
    for name, pixels, size, matrix in views:
        result = infer(pixels, size, max(0.1, confidence * 0.4))
        recovery["passes"] += 1
        diagnostics["raw_candidate_count"] += len(result["boxes"])
        diagnostics["passes"].append({"view": name, "candidate_count": len(result["boxes"])})
        for key, value in result.get("speed_ms", {}).items():
            speed[key] = speed.get(key, 0) + value
        for box in result["boxes"]:
            mapped = source_box(box["xyxy"], matrix, width, height)
            if mapped[2] - mapped[0] >= 4 and mapped[3] - mapped[1] >= 4:
                candidates.append(dict(box, xyxy=mapped, view=name))
            else:
                exclude(dict(box, xyxy=mapped, view=name), "region_too_small")
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    for box in sorted(candidates, key=lambda b: b["confidence"], reverse=True):
        if box["confidence"] < confidence or len(accepted) >= 6:
            exclude(
                box,
                "below_recovery_confidence"
                if box["confidence"] < confidence
                else "candidate_limit",
            )
            continue
        if any(iou(box["xyxy"], b["xyxy"]) >= 0.4 for b in accepted):
            exclude(box, "duplicate_region")
            continue
        supporting = sorted(
            {
                b["view"]
                for b in candidates
                if b["class_id"] == box["class_id"] and iou(box["xyxy"], b["xyxy"]) >= 0.5
            }
        )
        if len(supporting) < 2 or not has_display_color(hsv, box):
            exclude(
                box,
                "insufficient_supporting_views"
                if len(supporting) < 2
                else "display_color_mismatch",
            )
            continue
        accepted.append(
            {k: v for k, v in box.items() if k != "view"}
            | {"localization_method": VERSION, "supporting_views": supporting}
        )
        recovery["recovered_count"] += 1
    anchors = [b for b in accepted if b["class_id"] == 0]
    if photo and len(anchors) == 1 and len(accepted) < 6:
        pair = photo_pair_context(image, hsv, anchors[0], infer, imgsz, confidence)
        pair_diagnostics = pair["diagnostics"]
        diagnostics["photo_pair_context"] = pair_diagnostics
        recovery["passes"] += len(pair_diagnostics["passes"])
        diagnostics["passes"].extend(pair_diagnostics["passes"])
        diagnostics["raw_candidate_count"] += sum(
            p["candidate_count"] for p in pair_diagnostics["passes"]
        )
        diagnostics["excluded_candidates"].extend(pair_diagnostics["excluded_candidates"])
        for key, value in pair["speed_ms"].items():
            speed[key] = speed.get(key, 0) + value
        if pair["box"]:
            if any(iou(pair["box"]["xyxy"], b["xyxy"]) >= 0.4 for b in accepted):
                pair_diagnostics["reason"] = "duplicate_existing_region"
                exclude(pair["box"], "duplicate_existing_region")
            else:
                accepted.append(pair["box"])
                recovery["recovered_count"] += 1
    if photo and sum(b["class_id"] == 0 for b in accepted) < 2:
        # Close photographs can exceed the scale seen during training. Two
        # padded views must agree; this never runs on the live video path.
        closeups = []
        for scale in (0.35, 0.45):
            matrix = cv2.getRotationMatrix2D((width / 2, height / 2), 0, scale)
            pixels = cv2.warpAffine(image, matrix, (width, height), borderValue=(114, 114, 114))
            result = detect(pixels, infer, imgsz=imgsz, confidence=confidence)
            recovery["passes"] += result["recovery"]["passes"]
            nested = result["diagnostics"]
            diagnostics["raw_candidate_count"] += nested["raw_candidate_count"]
            diagnostics["passes"].extend(
                dict(p, view=f"closeup_{scale}/{p['view']}") for p in nested["passes"]
            )
            diagnostics["excluded_candidates"].extend(
                dict(
                    b,
                    view=f"closeup_{scale}/{b.get('view', 'original')}",
                    coordinate_space="scaled_detector_input",
                )
                for b in nested["excluded_candidates"]
            )
            for key, value in result.get("speed_ms", {}).items():
                speed[key] = speed.get(key, 0) + value
            for box in result["boxes"]:
                if box["class_id"] == 0:
                    closeups.append(
                        dict(box, xyxy=source_box(box["xyxy"], matrix, width, height), view=scale)
                    )
        for box in sorted(closeups, key=lambda b: b["confidence"], reverse=True):
            if len(accepted) >= 6 or any(iou(box["xyxy"], b["xyxy"]) >= 0.4 for b in accepted):
                exclude(box, "candidate_limit" if len(accepted) >= 6 else "duplicate_region")
                continue
            support = {b["view"] for b in closeups if iou(box["xyxy"], b["xyxy"]) >= 0.5}
            if len(support) < 2 or not has_display_color(hsv, box):
                exclude(
                    box,
                    "insufficient_supporting_views"
                    if len(support) < 2
                    else "display_color_mismatch",
                )
                continue
            accepted.append(
                {k: v for k, v in box.items() if k != "view"}
                | {
                    "localization_method": "photo_closeup_scale_v1",
                    "supporting_views": sorted(support),
                }
            )
            recovery["recovered_count"] += 1
    return output()
