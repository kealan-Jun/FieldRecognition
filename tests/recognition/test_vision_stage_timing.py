import numpy as np

import field_recognition.recognition.panel_readout as panel_readout
from field_recognition.measurements.readout_timing import update_timing
from tests.support.panel_readout import Clock, cloud_result


def test_primary_provider_slot_wait_is_separate_from_provider_execution(monkeypatch):
    clock = Clock()

    class Slot:
        released = False

        def acquire(self, blocking=False):
            return clock.value >= 0.3

        def release(self):
            self.released = True

    slot = Slot()
    core = {"vision_call_lock": slot, "now": lambda: "2026-10-08T01:00:00+00:00"}
    document = {"submitted_at": core["now"](), "status": "running"}
    monkeypatch.setattr(panel_readout, "save", lambda *_: None)
    monkeypatch.delenv("FIELD_VISION_DISPLAY_RULES", raising=False)

    def invoke(_):
        assert clock.value >= 0.3
        clock.value += 0.2
        return cloud_result()

    monkeypatch.setattr(panel_readout.vision, "read_panel", invoke)
    assert panel_readout.primary_unbound_photo(
        core,
        document,
        np.zeros((10, 10, 3), dtype=np.uint8),
        valid=lambda: True,
        clock=clock,
        pause=clock.pause,
        started=0,
    )
    timing = update_timing(document)
    assert timing["durations_ms"]["vision_queue_wait_ms"] == 300
    assert timing["durations_ms"]["vision_ms"] == 200
    assert timing["duration_clock_basis"]["vision_queue_wait_ms"] == "process_monotonic"
    assert slot.released


def test_panel_provider_wait_uses_sum_without_counting_provider_time():
    document = {
        "submitted_at": "2026-10-08T01:00:00+00:00",
        "fallback": {
            "attempted_at": "2026-10-08T01:00:00+00:00",
            "finished_at": "2026-10-08T01:00:09+00:00",
            "queue_wait_ms": 6000,
            "queue_wait_clock_basis": "process_monotonic",
            "duration_ms": 3000,
            "clock_basis": "process_monotonic",
        },
    }
    timing = update_timing(document)
    assert timing["durations_ms"]["vision_queue_wait_ms"] == 6000
    assert timing["durations_ms"]["vision_ms"] == 3000
    assert update_timing(document)["durations_ms"]["vision_queue_wait_ms"] == 6000
