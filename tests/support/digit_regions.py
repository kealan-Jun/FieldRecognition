"""Reusable digit_regions fixtures and doubles."""


def line(text, box, confidence=0.9):
    x, y, w, h = box
    return {
        "text": text,
        "confidence": confidence,
        "polygon": [[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
    }
