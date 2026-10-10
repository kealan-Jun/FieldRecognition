"""Reusable panel_regions fixtures and doubles."""

import copy

import field_recognition.recognition.panel_regions as panel_regions
from tests.support.archive_store import archive  # noqa: F401
from tests.support.demo import app_client, wait_for_job  # noqa: F401

A = "e9434a0a-3319-414a-b988-4cc6884edce4"


B = "eae17924-9fa7-4445-ac45-3987f5687be9"


BOXES = [
    dict(class_id=0, instrument_id=A, confidence=0.8, xyxy=[20, 30, 80, 50]),
    dict(class_id=1, instrument_id=B, confidence=0.9, xyxy=[210, 100, 300, 140]),
]


def configure(app, monkeypatch, boxes=BOXES):
    # These tests isolate association/crop routing; digit refinement has its own tests.
    import field_recognition.recognition.lcd_digits as lcd_digits

    monkeypatch.setattr(
        lcd_digits, "refine_lcd", lambda predict, image, initial, *args, **kw: initial
    )
    monkeypatch.setattr(
        panel_regions, "refine_digits", lambda predict, image, initial, x=0, y=0, **kw: initial
    )
    monkeypatch.setenv("FIELD_PANEL_DETECTOR_ENABLED", "1")
    monkeypatch.setattr(app.panel_detector, "warmup", lambda: True)
    monkeypatch.setattr(
        app.panel_detector,
        "predict",
        lambda image, **kw: {
            "status": "completed",
            "boxes": copy.deepcopy(boxes),
            "weights_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        app,
        "predict_panel",
        lambda image, x=0, y=0: {
            "status": "completed",
            "device": "cpu",
            "model": "test",
            "actual_model_invocation": True,
            "lines": [
                {
                    "text": "12.3 g" if x < 100 else "200 g",
                    "polygon": [[x, y], [x + 10, y], [x + 10, y + 8], [x, y + 8]],
                }
            ],
        },
    )
