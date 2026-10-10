import pytest

from field_recognition.archive.archive_catalog import build_index, render_index
from field_recognition.archive.archive_progress import checkpoint


def test_new_receipts_can_interrupt_index_before_loading_next_historical_document():
    visited = []

    def rows():
        for number in range(100):
            visited.append(number)
            yield {
                "seq": number,
                "entity": "scans",
                "entity_id": str(number),
                "document": "{}",
                "recorded_at": "2026-10-08T01:00:00+00:00",
                "receipt_path": "unused.json",
                "archived_at": "2026-10-08T01:00:00+00:00",
            }

    def pending():
        if len(visited) == 2:
            raise InterruptedError("new receipt waiting")

    with checkpoint(pending, interval=0), pytest.raises(InterruptedError):
        build_index(rows(), "instance", "2026-10-08T01:00:00+00:00", {})
    assert visited == [0, 1]


def test_navigation_json_encoding_can_yield_before_finishing_large_history():
    checks = []

    def pending():
        checks.append(True)
        if len(checks) == 2:
            raise InterruptedError("new receipt waiting")

    with checkpoint(pending, interval=0), pytest.raises(InterruptedError):
        render_index({"items": [{"reading": "<&实验"} for _ in range(10_000)]})
    assert len(checks) == 2
