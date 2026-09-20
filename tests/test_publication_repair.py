import copy
import hashlib
import json
from pathlib import Path
import threading

import pytest

from archive_events import processing_outcome, standard_records
from archive_paths import ArchivePaths
from archive_store import replace_view
from scripts.measure_publication_latency import initialize, inject_group, observe, parse_visible, summarize
from test_archive_events import publish_document
from test_measurement_records import document


def test_atomic_json_replacement_never_exposes_partial_bytes(tmp_path):
    path = tmp_path / 'Evidence.json'
    old, new = {'value': 'old' * 10000}, {'value': 'new' * 10000}
    replace_view(path, json.dumps(old).encode())
    done = threading.Event()
    failures, reads = [], []
    def reader():
        while not done.is_set():
            try:
                value = json.loads(path.read_bytes())
                assert value in (old, new)
                reads.append(True)
            except Exception as exc:
                failures.append(exc)
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(25):
            replace_view(path, json.dumps(new if index % 2 else old).encode())
    finally:
        done.set(); thread.join(timeout=2)
    assert reads and not failures
    assert not list(tmp_path.glob('*.tmp'))


def test_interrupted_atomic_write_preserves_previous_json(tmp_path, monkeypatch):
    path = tmp_path / 'Result.json'
    replace_view(path, b'{"values":[]}')
    import archive_store
    monkeypatch.setattr(archive_store.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('unavailable')))
    with pytest.raises(OSError):
        replace_view(path, b'{"values":[1]}')
    assert json.loads(path.read_bytes()) == {'values': []}
    assert not list(tmp_path.glob('*.tmp'))


def test_manifest_published_after_results_and_hashes_match(tmp_path, monkeypatch):
    import archive_store
    calls = []
    real = archive_store.replace_view
    def record(path, raw):
        calls.append(Path(path).name)
        return real(path, raw)
    monkeypatch.setattr(archive_store, 'replace_view', record)
    ArchivePaths(tmp_path).retire_views({'Day/Event/Evidence.json': b'{}', 'Day/Event/Result.json': b'{}'})
    assert calls.index('Result.json') < calls.index('Evidence.json')
    file = publish_document(tmp_path, document() | {'camera_id': 'Cam'})
    evidence = json.loads(file.read_bytes())
    assert evidence['processing_terminal'] and evidence['processing_outcome'] == 'succeeded'
    assert evidence['publication']['atomic_scope'] == 'one_file_same_directory_rename'
    for item in evidence['measurement_files']:
        assert hashlib.sha256((tmp_path / item['result']).read_bytes()).hexdigest() == item['sha256']


@pytest.mark.parametrize('status,skipped,outcome', [('completed', True, 'skipped'), ('failed', True, 'failed'),
                                                ('failed', False, 'failed'), ('cancelled', False, 'failed')])
def test_skipped_and_failed_without_result_have_terminal_evidence(tmp_path, status, skipped, outcome):
    doc = document() | {'camera_id': 'Cam', 'status': status, 'panel_regions': [], 'readings': [],
        'recognition_skipped': skipped, 'skip_reason': 'no_active_instrument_binding' if skipped else None,
        'error': 'detector_failed' if status == 'failed' else None}
    file = publish_document(tmp_path, doc)
    evidence = json.loads(file.read_bytes())
    assert evidence['processing_terminal'] and evidence['processing_outcome'] == outcome
    assert evidence['task_outcomes'][0]['outcome'] == outcome
    assert not list(file.parent.glob('Result*.json'))
    assert not processing_outcome(doc | {'resume_pending': True})['terminal']


def test_same_numeric_candidates_with_different_clarity_keep_raw_precision():
    from photo_measurements import candidates
    first = document()
    first['readings'][-1].update(text='0.0010 g', value='0.0010', clarity='clear')
    second = copy.deepcopy(first)
    second.update(job_id='job-2', capture_id='photo-2')
    second['readings'][-1]['clarity'] = 'medium'
    group = {'fields': candidates([first, second]), 'status': 'draft'}
    rows = [{'seq': index, 'doc': doc} for index, doc in enumerate([first, second])]
    record = next(r for r in standard_records(rows, group) if r['instrument_id'] == 'b')
    assert record['record']['values'][0]['value'] == .001
    field = next(f for f in group['fields'] if f['name'] == '质量')
    assert all(c['raw_value'] == '0.0010' and c['raw_text'] == '0.0010 g' for c in field['original_candidates'])
    second['readings'][-1].update(value='0.0020', text='0.0020 g')
    group['fields'] = candidates([first, second])
    record = next(r for r in standard_records(rows, group) if r['instrument_id'] == 'b')
    assert record['record']['values'][0]['value'] is None
    assert record['evidence']['conflicting_fields'] == ['质量']
    assert len(record['evidence']['observations']) == 2


def test_group_waits_for_all_three_tasks_but_not_human_confirmation(tmp_path):
    from archive_events import make_views, prepare
    def build(count):
        docs = [document() | {'job_id': f'task-{i}', 'capture_id': f'photo-{i}', 'camera_id': 'Cam'} for i in range(count)]
        group = {'measurement_id': 'measurement', 'camera_id': 'Cam', 'status': 'draft',
            'job_ids': [f'task-{i}' for i in range(3)], 'context': {'burst_id': 'group', 'expected_photos': 3}, 'sources': []}
        rows = [{'entity': entity, 'entity_id': ident, 'seq': index, 'document': json.dumps(doc),
                 'recorded_at': '2026-09-14T05:43:10Z', 'receipt_path': f'.System/Receipts/{index}.json'}
                for index, (entity, ident, doc) in enumerate([('photo_measurements', 'measurement', group)] +
                    [('jobs', doc['job_id'], doc) for doc in docs], 1)]
        paths = ArchivePaths(tmp_path)
        bundles, _ = prepare(rows, paths)
        views = make_views(bundles, paths, {row['seq']: {'artifacts': {}} for row in rows})
        return next(json.loads(raw) for name, raw in views.items() if name.endswith('/Evidence.json'))
    assert not build(2)['processing_terminal']
    value = build(3)
    assert value['status'] == 'draft' and value['processing_terminal']
    assert value['group_id'] == 'group' and len(value['task_outcomes']) == 3


def test_human_correction_cannot_publish_an_instrument_absent_from_task_binding():
    doc = document()
    group = {'fields': [{'instrument': {'id': 'unbound', 'model': 'Laptop'},
                        'name': '质量', 'value': '12.3400', 'unit': 'g', 'corrected': True}]}
    records = standard_records([{'seq': 1, 'doc': doc}], group)
    assert {record['instrument_id'] for record in records} == {'a', 'b'}
    doc.update(panel_regions=[], readings=[], all_binding_snapshots=[])
    assert standard_records([{'seq': 1, 'doc': doc}], group) == []


def test_burst_crossing_handoff_never_combines_different_wearers():
    first = document()
    second = copy.deepcopy(first)
    second.update(job_id='after-handoff', capture_id='after-photo')
    second['all_binding_snapshots'][0].update(binding_id='new-session', wearer_id='new-wearer')
    for region in second['panel_regions']:
        if region['instrument_id'] == 'a':
            region['binding_id'] = 'new-session'
    for reading in second['readings']:
        if (reading.get('instrument') or {}).get('id') == 'a':
            reading['binding_id'] = 'new-session'
    diagnostics = []
    records = standard_records([{'seq': 1, 'doc': first}, {'seq': 2, 'doc': second}], None,
                               diagnostics=diagnostics)
    assert [r['instrument_id'] for r in records] == ['b']
    assert diagnostics[0]['reason'] == 'binding_session_conflict'
    assert {r['binding_id'] for r in diagnostics[0]['observations']} == {'ba', 'new-session'}


def make_group(tmp_path):
    root = initialize(tmp_path / 'isolated')
    image = tmp_path / 'input.png'; image.write_bytes(b'test-source-bytes')
    ticks = iter([1, 2, 3, 4])
    group = inject_group(root, 'TestCamera', [image] * 3, clock=lambda: next(ticks))
    assert group['t0_monotonic'] > max(group['photo_write_completed_monotonic'])
    assert len(list((root / 'voice_photos').glob('*/*/*/*.png'))) == 3
    return root, group


def test_latency_separates_result_evidence_first_parse_and_group_task_denominators(tmp_path):
    root, group = make_group(tmp_path)
    path = root / 'archive' / 'Day' / 'Event'
    path.mkdir(parents=True)
    raw = b'{"values":[{"value":63}]}'
    result_name = 'Day/Event/Result.json'
    (path / 'Result.json').write_bytes(raw)
    reads = {}
    parse_visible(root, reads, clock=lambda: 6)
    evidence = {'group_id': group['group_id'], 'processing_terminal': True, 'processing_outcome': 'succeeded',
        'task_outcomes': [{'task_id': f'task-{i}', 'outcome': 'succeeded'} for i in range(3)],
        'measurement_files': [{'result': result_name, 'sha256': hashlib.sha256(raw).hexdigest()}]}
    (path / 'Evidence.json').write_text(json.dumps(evidence))
    observe(group, parse_visible(root, reads, clock=lambda: 8), clock=lambda: 9)
    assert group['result_first_parse_ms'] == 2000
    assert group['evidence_first_parse_ms'] == 4000
    assert group['manifest_verified_ms'] == 5000
    skipped = copy.deepcopy(group)
    skipped.update(group_id='skipped', outcome='skipped', result_first_parse_ms=None, task_outcomes=[
        {'task_id': f'skipped-{i}', 'outcome': 'skipped'} for i in range(3)])
    report = summarize([group, skipped])
    assert report['group_count'] == 2 and report['observed_task_count'] == 6
    assert report['evidence']['sample_count'] == 2 and report['result']['sample_count'] == 1
    assert report['result']['target_ms'] is None


def test_partial_or_mismatched_result_does_not_finish_waiting(tmp_path):
    root, group = make_group(tmp_path)
    path = root / 'archive' / 'Day' / 'Event'; path.mkdir(parents=True)
    (path / 'Result.json').write_bytes(b'{"values":')
    evidence = {'group_id': group['group_id'], 'processing_terminal': True, 'processing_outcome': 'succeeded',
        'measurement_files': [{'result': 'Day/Event/Result.json', 'sha256': '0' * 64}]}
    (path / 'Evidence.json').write_text(json.dumps(evidence))
    observe(group, parse_visible(root, {}, clock=lambda: 8), clock=lambda: 9)
    assert group['outcome'] == 'pending' and group['result_first_parse_ms'] is None
    (path / 'Result.json').write_bytes(b'{"values":[]}')
    observe(group, parse_visible(root, {}, clock=lambda: 8), clock=lambda: 9)
    assert group['outcome'] == 'pending'  # Valid JSON from another generation is insufficient.
    evidence.update(processing_outcome='skipped', measurement_files=[])
    (path / 'Evidence.json').write_text(json.dumps(evidence))
    observe(group, parse_visible(root, {}, clock=lambda: 8), clock=lambda: 9)
    assert group['outcome'] == 'skipped' and group['result_status'] == 'absent_terminal'
