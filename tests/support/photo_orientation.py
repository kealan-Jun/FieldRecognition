"""Synthetic quarter-turn scenes and explicit detector/OCR doubles."""

import copy

import numpy as np

from tests.support.panel_regions import B


def scene():
    rows, columns = np.indices((120, 100))
    upright = np.stack([rows, columns, (rows * 3 + columns * 5) % 256], axis=-1).astype(np.uint8)
    return np.ascontiguousarray(np.rot90(upright, 3)), upright


class Detector:
    def __init__(self, source, *, instrument=B, generic=False):
        self.source = source
        self.instrument = instrument
        self.generic = generic
        self.calls = []

    def enabled(self):
        return True

    def predict(self, pixels, **kwargs):
        degrees = next(
            d for d in (0, 90, 180, 270) if np.array_equal(pixels, np.rot90(self.source, d // 90))
        )
        self.calls.append(degrees)
        boxes = []
        if degrees in (90, 270):
            boxes = [
                {
                    "class_id": 1,
                    "confidence": 0.85 if degrees == 90 else 0.99,
                    "instrument_id": None if self.generic else self.instrument,
                    "xyxy": [20, 50, 80, 80] if degrees == 90 else [20, 40, 80, 70],
                }
            ]
            if self.generic:
                boxes[0]["type_id"] = "balance"
        return {
            "status": "completed",
            "boxes": copy.deepcopy(boxes),
            "weights_sha256": "f" * 64,
            "model_version": "synthetic-rotation",
        }


class Ocr:
    def __init__(self, lettering="one_direction"):
        self.lettering = lettering
        self.calls = []

    def __call__(self, pixels, x=0, y=0):
        self.calls.append(pixels.copy())
        context = pixels.shape[1] in (92, 184, 100)
        forward = int(pixels[0, 0, 0]) < 70
        if context:
            text = (
                "ACME"
                if self.lettering == "both" or (self.lettering == "one_direction" and forward)
                else "27902"
            )
            scale = 2 if pixels.shape[1] == 184 else 1
            polygon = (np.asarray([[16, 12], [56, 12], [56, 22], [16, 22]]) * scale).tolist()
        else:
            text, polygon = "12.3456 g", [[3, 3], [25, 3], [25, 12], [3, 12]]
        polygon = (np.asarray(polygon) + [x, y]).tolist()
        return {
            "status": "completed",
            "lines": [{"text": text, "confidence": 0.95, "polygon": polygon}],
            "actual_model_invocation": True,
            "ocr_model_call_count": 1,
            "ocr_model_lock_wait_ms": 0,
            "ocr_model_prepare_ms": 0,
            "ocr_model_call_ms": 1,
            "ocr_model_call_scope": "synthetic",
        }


def core(source, *, lettering="one_direction", generic=False):
    detector, ocr = Detector(source, generic=generic), Ocr(lettering)
    return (
        {
            "panel_detector": detector,
            "predict_panel": ocr,
            "now": lambda: "2026-10-22T07:00:00+00:00",
            "OCR_DEVICE": "cpu",
            "ocr_state": {"engine": "synthetic"},
            "get_instrument": lambda iid: {"id": iid, "type_id": "balance"},
        },
        detector,
        ocr,
    )
