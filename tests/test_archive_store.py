import hashlib
import json
from pathlib import Path

import pytest

from archive_store import ArchiveStore, immutable_write
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
    return [json.loads(p.read_text()) for p in root.glob('Receipts/**/*.json')]


def test_original_photo_and_decoding_evidence_are_archived(archive):
    app, client, root = archive
    capture = scan(client)
    source = (app.BASE / 'static/labels/InstrumentA.png').read_bytes()
    assert (app.DATA / capture['original_blob']).read_bytes() == source
    app.archive_store.step()
    record = next(r for r in receipts(root) if r['entity_id'] == capture['scan_id'])
    for artifact in record['artifacts'].values():
        raw = (root / artifact['path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == artifact['sha256']
        assert len(raw) == artifact['size_bytes']
    assert record['document']['received_at'] == capture['received_at']
    assert not record['physical_action_confirmed']
    assert record['artifacts']['original']['sha256'] == capture['source_sha256']
    assert client.get('/api/state').json()['archive']['pending_receipts'] == 0
    assert (root / 'Readme.html').is_file() and (root / 'Index.json').is_file()


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
    before = {str(p): p.read_bytes() for p in root.glob('Receipts/**/*.json')}
    recovered = ArchiveStore(vars(app))
    recovered.step()
    assert {str(p): p.read_bytes() for p in root.glob('Receipts/**/*.json')} == before
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
    assert (root / 'Index.json').is_file()


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
        assert hashlib.sha256((root / artifact['path']).read_bytes()).hexdigest() == artifact['sha256']
    assert app.archive_store.snapshot()['pending_receipts'] == 0
    stored = app.get_job(job['job_id'])
    assert stored['archive']['status'] == 'archived'
    assert stored['archive']['archived_at'] and stored['archive']['archive_queue_ms'] >= 0
