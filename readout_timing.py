"""Measured stage times; NAS wall-clock durations explicitly retain their basis."""
from datetime import datetime


def elapsed_ms(start, end):
    if not start or not end:
        return None
    value = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000
    return round(value, 2) if value >= 0 else None


def update_timing(document):
    timing = document.setdefault('timing', {})
    timing['queued_at'] = document['submitted_at']
    for source, target in [('started_at', 'run_started_at'), ('finished_at', 'result_finished_at')]:
        if document.get(source):
            timing[target] = document[source]
    local = document.get('local_ocr') or {}
    for key in ('ocr_started_at', 'ocr_finished_at'):
        if local.get(key):
            timing[key] = local[key]
    fallback = document.get('fallback') or {}
    if fallback.get('attempted_at'):
        timing['vision_started_at'] = fallback['attempted_at']
    if fallback.get('finished_at'):
        timing['vision_finished_at'] = fallback['finished_at']
    pairs = {
        'write_to_detect_ms': ('source_written_at', 'first_observed_at'),
        'file_stability_ms': ('first_observed_at', 'stable_at'),
        'ingest_wait_ms': ('stable_at', 'read_started_at'),
        'file_read_ms': ('read_started_at', 'photo_read_at'),
        'import_ms': ('photo_read_at', 'imported_at'),
        'queue_wait_ms': ('queued_at', 'run_started_at'),
        'ocr_wait_ms': ('run_started_at', 'ocr_started_at'),
        'ocr_ms': ('ocr_started_at', 'ocr_finished_at'),
        'vision_ms': ('vision_started_at', 'vision_finished_at'),
        'write_to_ocr_ms': ('source_written_at', 'ocr_started_at'),
        'write_to_result_ms': ('source_written_at', 'result_finished_at'),
        'detect_to_result_ms': ('first_observed_at', 'result_finished_at'),
    }
    timing['durations_ms'] = {name: elapsed_ms(timing.get(start), timing.get(end))
                              for name, (start, end) in pairs.items()}
    timing['source_clock_basis'] = {
        'nas_file_mtime': 'NAS file mtime; clocks not independently synchronized',
        'agent_receipt': 'Agent-declared write time; clock not independently verified',
        'not_recorded': 'Write time not available',
    }.get(timing.get('source_written_basis'), 'Source write clock not independently verified')
    if timing.get('source_written_at') and timing.get('first_observed_at'):
        timing['source_clock_ahead'] = elapsed_ms(timing['source_written_at'], timing['first_observed_at']) is None
    return timing
