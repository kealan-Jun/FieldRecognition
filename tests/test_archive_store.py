import hashlib
import json
from pathlib import Path

import pytest

from archive_store import ArchiveStore, immutable_write, receipt_status
from archive_integrity import relative_file
from test_demo import app_client, register, scan  # noqa: F401


@pytest.fixture
def archive(app_client, monkeypatch, tmp_path):
    app, client = app_client
    root = tmp_path / 'NasMount' / 'FieldRecognitionArchive'
    monkeypatch.setenv('FIELD_ARCHIVE_ENABLED', '1')
    monkeypatch.setenv('FIELD_ARCHIVE_ROOT', str(root))
    monkeypatch.delenv('FIELD_ARCHIVE_MOUNT', raising=False)
    return app, client, root


def receipts(root):
    return [json.loads(p.read_text()) for p in root.glob('.System/Receipts/**/*.json')]


def test_original_photo_and_decoding_evidence_are_archived(archive):
    app, client, root = archive
    capture = scan(client)
    source = (app.BASE / 'static/labels/InstrumentA.png').read_bytes()
    assert (app.DATA / capture['original_blob']).read_bytes() == source
    app.archive_store.step()
    record = next(r for r in receipts(root) if r['entity_id'] == capture['scan_id'])
    for artifact in record['artifacts'].values():
        raw = relative_file(root, artifact['path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == artifact['sha256']
        assert len(raw) == artifact['size_bytes']
    assert record['document']['received_at'] == capture['received_at']
    assert not record['physical_action_confirmed']
    assert record['artifacts']['original']['sha256'] == capture['source_sha256']
    assert client.get('/api/state').json()['archive']['pending_receipts'] == 0
    assert (root / 'Readme.html').is_file() and (root / '.System/Index.json').is_file()


def test_archive_reports_progress_during_index_build(archive):
    from archive_progress import observe
    app, client, root = archive
    scan(client)
    updates = []
    with observe(updates.append, interval=0):
        app.archive_store.step()
    verified = [p for p in updates if p.get('work_phase') == 'checking_receipts']
    assert verified and verified[-1]['progress_current'] == verified[-1]['progress_total']
    assert (root / '.System/Index.json').is_file()


def test_archive_progress_is_scoped_and_never_a_background_timer(monkeypatch):
    import archive_progress as progress
    import threading
    now = [0]
    monkeypatch.setattr(progress.time, 'monotonic', lambda: now[0])
    updates = []
    with progress.observe(updates.append):
        progress.advance('checking_receipts', 1, 3)
        assert updates == []
        now[0] = 1000  # Time alone is not proof of forward progress.
        thread = threading.Thread(target=progress.advance)
        thread.start(); thread.join()
        assert updates == []
        progress.advance('checking_receipts', 2, 3)
        assert updates == [{'work_phase':'checking_receipts','progress_current':2,'progress_total':3}]
    now[0] += 1000
    progress.advance('checking_receipts', 3, 3)
    assert len(updates) == 1


def test_empty_directory_cleanup_does_not_follow_symlinks(tmp_path):
    from archive_paths import empty_directories
    root = tmp_path / 'Archive'; root.mkdir()
    (root / 'Empty' / 'Nested').mkdir(parents=True)
    outside = tmp_path / 'Outside'; outside.mkdir()
    (outside / 'Keep').mkdir()
    (root / 'Linked').symlink_to(outside, target_is_directory=True)
    empty_directories(root)
    assert root.exists() and not (root / 'Empty').exists()
    assert (root / 'Linked').is_symlink() and (outside / 'Keep').exists()


def test_outbox_rolls_back_with_business_transaction_and_keeps_versions(archive):
    app, client, root = archive
    capture = scan(client)
    with app.db() as conn:
        before = conn.execute('SELECT count(*) FROM archive_outbox').fetchone()[0]
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE scans SET document=? WHERE id=?', (json.dumps(capture | {'operator': 'rolled-back'}), capture['scan_id']))
        conn.rollback()
        assert conn.execute('SELECT count(*) FROM archive_outbox').fetchone()[0] == before
        conn.execute('UPDATE scans SET document=? WHERE id=?', (json.dumps(capture | {'operator': '测试员'}), capture['scan_id']))
    app.archive_store.step()
    versions = [r for r in receipts(root) if r['entity_id'] == capture['scan_id']]
    assert len(versions) == 2
    assert {r['document'].get('operator') for r in versions} == {None, '测试员'}


def test_missing_nas_retains_queue_then_retry_is_idempotent(archive, monkeypatch):
    app, client, root = archive
    scan(client)
    monkeypatch.setenv('FIELD_ARCHIVE_MOUNT', '/missing/archive-mount')
    with pytest.raises(OSError):
        app.archive_store.step()
    assert not root.exists()
    assert app.archive_store.snapshot()['pending_receipts'] > 0
    monkeypatch.delenv('FIELD_ARCHIVE_MOUNT')
    with app.db() as conn:
        conn.execute('UPDATE archive_outbox SET retry_after=0')
    app.archive_store.step()
    before = {str(p): p.read_bytes() for p in root.glob('.System/Receipts/**/*.json')}
    recovered = ArchiveStore(vars(app))
    recovered.step()
    assert {str(p): p.read_bytes() for p in root.glob('.System/Receipts/**/*.json')} == before
    assert recovered.snapshot()['pending_receipts'] == 0


def test_wrong_hash_blocks_ack_but_does_not_block_other_receipts(archive):
    app, client, root = archive
    capture = scan(client)
    (app.DATA / 'Images' / (capture['scan_id'] + '.png')).write_bytes(b'corrupt')
    client.put('/api/automation', json={'enabled': False, 'operator': '测试员'})
    app.archive_store.step()
    state = app.archive_store.snapshot()
    assert state['status'] == 'retrying'
    assert state['pending_receipts'] == 1 and state['archived_receipts'] >= 1
    assert not any(r['entity_id'] == capture['scan_id'] for r in receipts(root))


def test_immutable_write_refuses_different_existing_contents(tmp_path):
    path = tmp_path / 'receipt.json'
    immutable_write(path, b'original')
    immutable_write(path, b'original')
    with pytest.raises(ValueError):
        immutable_write(path, b'changed')
    assert path.read_bytes() == b'original'
    assert not list(tmp_path.glob('*.tmp'))


def test_reference_archive_is_never_a_write_target(archive, monkeypatch):
    app, client, root = archive
    scan(client)
    protected = root.parent / 'VisionCortexExperimentArchive' / 'FieldRecognitionArchive'
    monkeypatch.setenv('FIELD_ARCHIVE_ROOT', str(protected))
    with pytest.raises(ValueError):
        app.archive_store.step()
    assert not protected.exists()


def test_index_publish_failure_is_retried_after_receipts_acknowledged(archive, monkeypatch):
    app, client, root = archive
    scan(client)
    original = app.archive_store.write_index
    def fail():
        raise OSError('index unavailable')
    monkeypatch.setattr(app.archive_store, 'write_index', fail)
    with pytest.raises(OSError):
        app.archive_store.step()
    assert app.archive_store.snapshot()['pending_receipts'] == 0
    monkeypatch.setattr(app.archive_store, 'write_index', original)
    app.archive_store.step()
    assert (root / '.System/Index.json').is_file()


def test_legacy_normalized_only_photo_is_marked_explicitly(archive):
    app, client, root = archive
    capture = scan(client)
    legacy = {k: v for k, v in capture.items() if k != 'original_blob'}
    with app.db() as conn:
        conn.execute('UPDATE scans SET document=? WHERE id=?', (json.dumps(legacy), capture['scan_id']))
    app.archive_store.step()
    records = [r for r in receipts(root) if r['entity_id'] == capture['scan_id']]
    assert any(r['artifacts'].get('original_status') == 'not_retained_by_earlier_version' for r in records)


def test_binding_lifecycle_and_ocr_result_retain_linked_photo_and_crop(archive, monkeypatch):
    from concurrent.futures import Future
    from test_panel_readout import Clock, complete, execute, local_result

    app, client, root = archive
    capture = scan(client)
    instrument_id = capture['matches'][0]['id']
    register(client, instrument_id)
    binding = client.post('/api/bindings', json={'scan_id': capture['scan_id'],
        'instrument_id': instrument_id, 'operator': '测试员'}).json()
    assert app.ocr_warmup_future.result(timeout=2)
    monkeypatch.setattr(app.readout_pool, 'submit', lambda *args: Future())
    job = client.post('/api/ocr', json={'capture_id': capture['capture_id'],
        'binding_id': binding['binding_id'], 'crop': [10, 20, 160, 140]}).json()
    completed = execute(app, job, complete(local_result('12.34 g')), Clock())
    ended = client.post('/api/bindings/' + binding['binding_id'] + '/end').json()
    app.archive_store.step(batch_size=100)
    records = receipts(root)
    versions = [r['document'] for r in records if r['entity'] == 'bindings']
    assert {r.get('ended_at') for r in versions} == {None, ended['ended_at']}
    assert all(r['operator'] == '测试员' and r['started_at'] == binding['started_at'] for r in versions)
    assert any(r['entity'] == 'scene_visits' for r in records)
    output = next(r for r in records if r['entity_id'] == job['job_id'] and r['document']['status'] == 'completed')
    assert output['document']['lines'] == completed['lines']
    assert output['document']['binding_id'] == binding['binding_id']
    assert output['artifacts']['image']['sha256'] == capture['image_sha256']
    assert output['artifacts']['panel']['sha256'] == completed['crop_image_sha256']
    assert output['artifacts']['original']['sha256'] == capture['source_sha256']
    for artifact in output['artifacts'].values():
        assert hashlib.sha256(relative_file(root, artifact['path']).read_bytes()).hexdigest() == artifact['sha256']
    assert app.archive_store.snapshot()['pending_receipts'] == 0
    stored = app.get_job(job['job_id'])
    assert stored['archive']['status'] == 'archived'
    assert stored['archive']['archived_at'] and stored['archive']['archive_queue_ms'] >= 0
    assert stored['timing']['archive_readable_at'] == stored['archive']['readable_at']
    assert stored['timing']['durations_ms']['archive_publication_ms'] is not None
    evidence = [json.loads(p.read_text()) for p in root.glob('*/PhotoReadings/*/Evidence.json')]
    observation = next(o for e in evidence for o in e.get('observations', []) if o['job_id'] == job['job_id'])
    assert observation['timing']['archive_readable_at'] == stored['archive']['readable_at']


def test_archive_status_separates_receipt_publication_and_readability(archive):
    app, client, root = archive
    capture = scan(client)
    app.archive_store.step(batch_size=100)
    with app.db() as conn:
        row = conn.execute("SELECT * FROM archive_outbox WHERE entity='scans' AND entity_id=? ORDER BY seq DESC LIMIT 1",
                           (capture['scan_id'],)).fetchone()
        status = receipt_status(conn, 'scans', capture['scan_id'])
    assert row['archived_at'] and row['published_at'] and row['readable_at']
    assert row['readability_basis'] == 'archive_writer_derived_views_published'
    assert status['published_at'] == row['published_at'] and status['readable_at'] == row['readable_at']
    index = json.loads((root / '.System/Index.json').read_text())
    item = next(item for item in index['items'] if item['entity'] == 'scans' and item['entity_id'] == capture['scan_id'])
    assert item['archive_published_at'] == row['published_at']
    assert item['archive_readable_at'] == row['readable_at']


def test_readability_retries_after_interrupted_views_without_fabricating_legacy_times(archive, monkeypatch):
    app, client, root = archive
    old = scan(client)
    app.archive_store.step(batch_size=100)
    with app.db() as conn:
        conn.execute('UPDATE archive_outbox SET published_at=NULL,readable_at=NULL,readability_basis=NULL')
    fresh = scan(client)
    with monkeypatch.context() as patch:
        patch.setattr(app.archive_store, 'write_index', lambda: None)
        app.archive_store.step(batch_size=100)
    with app.db() as conn:
        pending = receipt_status(conn, 'scans', fresh['scan_id'])
        assert pending['published_at'] and pending['readable_at'] is None
    app.archive_store.step(batch_size=100)
    with app.db() as conn:
        assert receipt_status(conn, 'scans', fresh['scan_id'])['readable_at']
        legacy = receipt_status(conn, 'scans', old['scan_id'])
        assert legacy['published_at'] is None and legacy['readable_at'] is None
