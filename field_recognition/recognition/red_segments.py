"""Read separated red LED bars in an authorized integer display crop.

Only exact seven-segment patterns are accepted across multiple thresholds.
This is pixel evidence, not a calibrated probability or a general OCR model.
"""

import cv2
import numpy as np

from field_recognition.recognition.led_digits import PATTERNS

VERSION = "fragmented-red-segments/1"
CENTERS = (
    (0.5, 0.08),
    (0.82, 0.28),
    (0.78, 0.75),
    (0.45, 0.92),
    (0.12, 0.73),
    (0.2, 0.26),
    (0.5, 0.51),
)


def _glyph(mask, box):
    x, y, w, h = box
    glyph = mask[y : y + h, x : x + w]
    if w / h < 0.30:
        # A narrow 1 consists of two vertical bars. Do not mistake a decimal
        # point, minus sign, or a clipped sliver of another glyph for a 1.
        components = cv2.connectedComponentsWithStats(glyph)[2][1:]
        if (
            len(components) == 2
            and all(c[3] >= h * 0.25 and c[3] > c[2] for c in components)
            and abs(float(components[0][1]) - float(components[1][1])) >= h * 0.35
        ):
            return "1", None
        return None, None
    if not 0.35 <= w / h <= 0.85:
        return None, None
    levels = []
    for cx, cy in CENTERS:
        left, right = max(0, round((cx - 0.18) * w)), min(w, round((cx + 0.18) * w) + 1)
        top, bottom = max(0, round((cy - 0.105) * h)), min(h, round((cy + 0.105) * h) + 1)
        levels.append(float(glyph[top:bottom, left:right].mean()))
    if any(0.16 < v < 0.24 for v in levels):
        return None, levels
    pattern = "".join("1" if v >= 0.24 else "0" for v in levels)
    return PATTERNS.get(pattern), levels


def check(image):
    evidence = {"rule_version": VERSION, "status": "inconclusive", "views": []}
    if image.ndim != 3 or min(image.shape[:2]) < 16:
        return evidence
    blue, green, red = cv2.split(image.astype(np.float32))
    channel = np.maximum(blue, green)  # Either unsaturated channel can retain a faint bar.
    color = (red - channel > 60) & (red > 180)
    if np.count_nonzero(color) < 40:
        return evidence
    threshold, _ = cv2.threshold(
        channel[color].astype(np.uint8), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    if threshold < 20 or np.percentile(channel[color], 95) - threshold < 15:
        return evidence
    for factor in (0.60, 0.70, 0.75):
        mask = ((channel > threshold * factor) & color).astype(np.uint8)
        _, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        areas = stats[1:, 4]
        if not np.any(areas >= 2):
            continue
        minimum = max(2, float(np.median(areas[areas >= 2])) * 0.15)
        small = []
        for index, (x, y, w, h, area) in enumerate(stats[1:], 1):
            if area < minimum:
                mask[labels == index] = 0
                if area >= 2:
                    small.append((x, y, w, h))
        columns = np.flatnonzero(mask.sum(axis=0))
        if not len(columns):
            continue
        groups = np.split(columns, np.flatnonzero(np.diff(columns) > 1) + 1)
        if not 2 <= len(groups) <= 6:
            continue
        boxes = []
        for group in groups:
            left, right = int(group[0]), int(group[-1]) + 1
            rows = np.flatnonzero(mask[:, left:right].sum(axis=1))
            boxes.append((left, int(rows[0]), right - left, int(rows[-1] - rows[0]) + 1))
        height = max(b[3] for b in boxes)
        top = min(b[1] for b in boxes)
        # At very low native resolution the glow fills unlit bars (7 can look
        # like 9). Enlarging pixels cannot supply that missing evidence.
        if (
            any(b[3] < 20 or b[3] < height * 0.65 for b in boxes)
            or max(b[1] + b[3] / 2 for b in boxes) - min(b[1] + b[3] / 2 for b in boxes)
            > height * 0.4
        ):
            continue
        # An isolated small mark at the baseline could be a decimal point.
        if any(
            y + h > top + height * 0.75 and not any(bx <= x < bx + bw for bx, _, bw, _ in boxes)
            for x, y, w, h in small
        ):
            continue
        text, fills = "", []
        for box in boxes:
            digit, levels = _glyph(mask, box)
            if digit is None:
                break
            text += digit
            fills.append(None if levels is None else [round(v, 4) for v in levels])
        if len(text) == len(boxes):
            evidence["views"].append(
                {
                    "threshold": round(threshold * factor, 2),
                    "text": text,
                    "glyph_boxes": boxes,
                    "segment_fill": fills,
                }
            )
    values = {v["text"] for v in evidence["views"]}
    if len(values) == 1 and len(evidence["views"]) >= 2:
        boxes = [b for v in evidence["views"] for b in v["glyph_boxes"]]
        left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
        right, bottom = max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)
        evidence.update(
            status="consistent", text=values.pop(), crop=[left, top, right - left, bottom - top]
        )
    elif len(values) > 1:
        evidence["status"] = "disagreement"
    return evidence


def corroborates(value, texts):
    """Require an OCR view to agree or omit just one independently visible digit."""
    return any(
        text == value
        or (
            len(text) + 1 == len(value)
            and any(value[:i] + value[i + 1 :] == text for i in range(len(value)))
        )
        for text in texts
    )
