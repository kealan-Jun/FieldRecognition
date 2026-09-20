from readout_timing import update_timing, record_duration, elapsed_ms


def test_timing_keeps_result_generation_and_archive_milestones_separate():
    document = {
        'submitted_at': '2026-09-18T01:00:00+00:00',
        'started_at': '2026-09-18T01:00:01+00:00',
        'result_generation_started_at': '2026-09-18T01:00:03+00:00',
        'result_generated_at': '2026-09-18T01:00:03.250+00:00',
        'finished_at': '2026-09-18T01:00:03.300+00:00',
        'timing': {
            'source_written_at': '2026-09-18T00:59:50+00:00',
            'archive_published_at': '2026-09-18T01:00:05+00:00',
            'archive_readable_at': '2026-09-18T01:00:06+00:00',
        },
    }
    timing = update_timing(document)
    assert timing['durations_ms']['result_generation_ms'] == 250
    assert timing['durations_ms']['result_finalize_ms'] == 50
    assert timing['durations_ms']['archive_publication_ms'] == 1700
    assert timing['durations_ms']['archive_readability_ms'] == 1000
    assert timing['durations_ms']['write_to_readable_result_ms'] == 16000


def test_missing_archive_milestones_remain_null():
    document = {'submitted_at': '2026-09-18T01:00:00+00:00'}
    timing = update_timing(document)
    assert timing['durations_ms']['result_generation_ms'] is None
    assert timing['durations_ms']['archive_publication_ms'] is None
    assert timing['durations_ms']['archive_readability_ms'] is None


def test_process_monotonic_durations_win_when_wall_clock_steps_backwards():
    doc = {'job_id': 'task', 'measurement_context': {'burst_id': 'group'},
           'submitted_at': '2026-09-18T01:00:00+00:00',
           'started_at': '2026-09-18T01:00:10+00:00',
           'finished_at': '2026-09-18T01:00:05+00:00',
           'local_ocr': {'panel_detection': {'duration_ms': 25}, 'ocr_elapsed_ms': 80},
           'fallback': {'duration_ms': 15, 'clock_basis': 'process_monotonic'}}
    record_duration(doc, 'processing_ms', 2, 2.125)
    timing = update_timing(doc)
    assert timing['durations_ms']['processing_ms'] == 125
    assert timing['durations_ms']['panel_detection_ms'] == 25
    assert timing['durations_ms']['ocr_ms'] == 80
    assert timing['durations_ms']['vision_ms'] == 15
    assert timing['duration_clock_basis']['processing_ms'] == 'process_monotonic'
    assert timing['task_id'] == 'task' and timing['group_id'] == 'group'
    assert update_timing(doc)['durations_ms']['processing_ms'] == 125
    assert elapsed_ms('invalid', 'invalid') is None
    assert elapsed_ms('2026-09-18T01:00:00', '2026-09-18T01:00:01') is None


def test_clock_basis_does_not_claim_cross_machine_visibility_is_monotonic():
    doc = {'submitted_at': '2026-09-18T01:00:00+00:00', 'timing': {
        'source_written_at': '2026-09-18T00:59:00+00:00', 'source_written_basis': 'nas_file_mtime',
        'archive_readable_at': '2026-09-18T01:00:01+00:00'}}
    timing = update_timing(doc)
    assert timing['duration_clock_basis']['write_to_readable_result_ms'] == 'wall_clock_timestamp_difference'
    assert 'not independently synchronized' in timing['source_clock_basis']
