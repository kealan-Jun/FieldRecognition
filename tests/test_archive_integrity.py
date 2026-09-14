import json
from datetime import datetime, timedelta, timezone

from archive_integrity import ArchiveIntegrity
from test_archive_store import archive  # noqa: F401
from test_demo import app_client, scan  # noqa: F401


def test_daily_check_verifies_deduplicated_objects_and_persists_schedule(archive):
    app, client, root = archive
    capture = scan(client)
    with app.db() as conn:
        conn.execute('UPDATE scans SET document=? WHERE id=?', (json.dumps(capture | {'operator': '实验员'}), capture['scan_id']))
    app.archive_store.step()
    checker = app.archive_store.integrity
    assert checker.due()
    report = checker.run_once()
    assert report['status'] == 'completed' and report['issue_count'] == 0
    assert report['verified_receipts'] == report['expected_receipts'] == 2
    assert report['verified_objects'] == report['expected_objects'] == 2
    assert not report['repair_performed']
    assert not checker.due() and not ArchiveIntegrity(app.archive_store).due()
    app.archive_store.step()
    status = client.get('/api/state').json()['archive']['integrity']
    assert status['report_path'] and not status['report_pending']
    assert json.loads((root / status['report_path']).read_text()) == report
    assert json.loads((root / 'Integrity/Latest.json').read_text())['run_id'] == report['run_id']
    assert json.loads((root / 'Index.json').read_text())['integrity']['last_report']['run_id'] == report['run_id']
    # Tomorrow is due even after constructing a new service instance.
    with app.db() as conn:
        old = report | {'finished_at': (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}
        conn.execute('UPDATE archive_integrity_runs SET document=?', (json.dumps(old),))
    assert ArchiveIntegrity(app.archive_store).due()


def test_missing_and_corrupt_files_are_reported_and_never_repaired(archive):
    app, client, root = archive
    scan(client); scan(client)
    app.archive_store.step()
    records = sorted(root.glob('Receipts/**/*.json'))
    artifact = next(root.glob('Objects/**/*.png'))
    artifact.write_bytes(b'corrupt-object')
    missing = records[-1]; missing.unlink()
    report = app.archive_store.integrity.run_once()
    assert report['status'] == 'findings' and report['issue_count'] == 2
    assert {i['kind'] for i in report['issues']} == {'receipt', 'object'}
    assert any(i['code'] == 'missing' for i in report['issues'])
    assert artifact.read_bytes() == b'corrupt-object' and not missing.exists()
    assert app.archive_store.snapshot()['pending_receipts'] == 0  # Acknowledgement history stays intact.
    app.archive_store.step()
    assert app.archive_store.snapshot()['integrity']['last_report']['issue_count'] == 2


def test_altered_receipt_cannot_redefine_the_expected_object_hash(archive):
    app, client, root = archive
    scan(client); app.archive_store.step()
    receipt = next(root.glob('Receipts/**/*.json'))
    doc = json.loads(receipt.read_text()); doc['document']['operator'] = 'changed'
    receipt.write_text(json.dumps(doc))
    report = app.archive_store.integrity.run_once()
    assert report['status'] == 'findings' and report['verified_receipts'] == 0
    assert report['issues'][0]['kind'] == 'receipt'


def test_lost_mount_keeps_local_report_and_retries_publication(archive, monkeypatch):
    app, client, root = archive
    scan(client); app.archive_store.step()
    monkeypatch.setenv('FIELD_ARCHIVE_MOUNT', '/no-mount')
    checker = app.archive_store.integrity
    report = checker.run_once()
    assert report['status'] == 'unavailable' and report['verified_receipts'] == 0
    assert checker.snapshot()['report_pending']
    assert not checker.due()
    monkeypatch.delenv('FIELD_ARCHIVE_MOUNT')
    app.archive_store.step()
    assert not checker.snapshot()['report_pending']
    assert (root / checker.snapshot()['report_path']).is_file()


def test_shutdown_is_incomplete_and_simultaneous_check_is_skipped(archive):
    app, client, root = archive
    scan(client); app.archive_store.step()
    checker = app.archive_store.integrity
    with checker.lock:
        assert checker.run_once() is None
    checker.stop.set()
    report = checker.run_once()
    assert report['status'] == 'interrupted' and report['verified_receipts'] == 0
