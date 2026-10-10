"""Reusable measurement_records fixtures and doubles."""

from field_recognition.measurements.reading_results import build_readings
from tests.support.demo import app_client, register, scan  # noqa: F401


def document():
    a = {
        "id": "a",
        "model": "MS-H280-Pro",
        "device_no": "0007",
        "measurement_ranges": {"温度": {"unit": "°C", "range": [None, 280]}},
    }
    b = {"id": "b", "model": "", "device_no": None}
    doc = {
        "job_id": "job",
        "capture_id": "photo",
        "status": "completed",
        "image_url": "/api/images/photo",
        "image_sha256": "a" * 64,
        "operator": "徐荣炜",
        "device": "gpu:0",
        "external_photo": {"captured_at": "2026-09-14T05:43:10.123456+00:00"},
        "all_binding_snapshots": [
            {
                "binding_id": "ba",
                "instrument": a,
                "wearer_id": "wearer-7",
                "qr_hash": "b" * 64,
                "scan_id": "scan-a",
            },
            {"binding_id": "bb", "instrument": b},
        ],
        "panel_regions": [],
        "lines": [],
        "panel_detection": {"status": "completed"},
    }
    for instrument, cls, binding, names, texts in [
        (a, 0, "ba", ["温度", "转速"], ["179", "250"]),
        (b, 1, "bb", ["质量"], ["-32.4223 g"]),
    ]:
        for index, (name, text) in enumerate(zip(names, texts)):
            pid = f"panel-{cls}-{index}"
            doc["panel_regions"].append(
                {
                    "panel_id": pid,
                    "class_id": cls,
                    "instrument": instrument,
                    "instrument_id": instrument["id"],
                    "binding_id": binding,
                    "association_basis": "panel_detector_session_binding",
                    "measurement_name": name,
                }
            )
            doc["lines"].append({"panel_id": pid, "text": text})
    doc["readings"] = build_readings(doc)
    return doc
