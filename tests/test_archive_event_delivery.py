"""Current delivery preserves queued history and measures actual publication work."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager

import pytest

from archive_migration import publish_events
from archive_paths import ArchivePaths
from test_archive_incremental import STAMP, job, scan, snapshots  # noqa: F401


def evidence(root):
    return json.loads(next(root.glob('*/VoicePhotoReadings/*/Evidence.json')).read_text())


def test_terminal_delivery_exposes_pending_history_and_late_fifo_cannot_regress(snapshots):
    archive, add, root = snapshots
    source = add('scans', 'photo', scan('photo'))
    old = add('jobs', 'reading', job('reading', 'photo', status='running'))
    terminal = add('jobs', 'reading', job('reading', 'photo'), pending=True)
    with archive.core['db']() as conn:
        conn.execute('UPDATE archive_outbox SET archived_at=NULL,published_at=NULL,readable_at=NULL WHERE seq=?', (old,))
    publish_events(archive, sequences={terminal})
    first = evidence(root)
    assert first['processing_terminal']
    assert [v['sequence'] for v in first['receipt_versions']] == [source, terminal]
    assert first['archive']['history'] == {'total_receipts': 3, 'published_receipts': 2,
        'pending_receipts': 1, 'complete': False,
        'scope': 'event_entity_receipt_versions_not_background_integrity_scan'}
    with archive.core['db']() as conn:
        old_row = conn.execute('SELECT * FROM archive_outbox WHERE seq=?', (old,)).fetchone()
        assert old_row['readable_at'] is None
        conn.execute('UPDATE archive_outbox SET archived_at=?,published_at=? WHERE seq=?', (STAMP, STAMP, old))
    publish_events(archive, sequences={old})
    final = evidence(root)
    assert final['processing_terminal']
    assert final['observations'] == first['observations']
    assert [v['sequence'] for v in final['receipt_versions']] == [source, old, terminal]
    assert final['archive']['history']['complete']
    assert final['archive']['history']['pending_receipts'] == 0
    assert final['publication']['generation'] != first['publication']['generation']


def test_selected_delivery_does_not_publish_unready_unrelated_terminal(snapshots):
    archive, add, root = snapshots
    add('scans', 'first', scan('first'))
    selected = add('jobs', 'reading', job('reading', 'first'), pending=True)
    add('scans', 'second', scan('second'))
    other = add('jobs', 'unready', job('unready', 'second'), pending=True, receipt=False)
    publish_events(archive, sequences={selected})
    assert evidence(root)['task_ids'] == ['reading']
    with archive.core['db']() as conn:
        assert conn.execute('SELECT readable_at FROM archive_outbox WHERE seq=?', (other,)).fetchone()[0] is None


def test_history_counts_a_new_retry_with_no_archived_snapshot(snapshots):
    archive, add, root = snapshots
    add('scans', 'photo', scan('photo'))
    terminal = add('jobs', 'reading', job('reading', 'photo'), pending=True)
    retry = add('jobs', 'retry', job('retry', 'photo', status='queued',
        recognition_root_job_id='reading', supersedes_job_id='reading', recognition_revision=2))
    with archive.core['db']() as conn:
        conn.execute('UPDATE archive_outbox SET archived_at=NULL,published_at=NULL,readable_at=NULL WHERE seq=?', (retry,))
    publish_events(archive, sequences={terminal})
    current = evidence(root)
    assert current['archive']['history']['total_receipts'] == 3
    assert current['archive']['history']['pending_receipts'] == 1
    assert not current['archive']['history']['complete']
    assert current['task_ids'] == ['reading']  # Unarchived values never substitute for evidence.


def test_publication_phase_timings_persist_and_corrupt_dependencies_fail(snapshots):
    archive, add, root = snapshots
    add('scans', 'photo', scan('photo'))
    terminal = add('jobs', 'reading', job('reading', 'photo'), pending=True)
    publish_events(archive, sequences={terminal})
    with archive.core['db']() as conn:
        measured = json.loads(conn.execute("SELECT value FROM archive_meta WHERE key LIKE 'event_publication:%'").fetchone()[0])
        assert measured['status'] == 'published'
        assert measured['clock_basis'] == 'process_monotonic'
        assert measured['sequences'] == [terminal]
        assert measured['duration_ms'] >= sum(measured['durations_ms'].values()) - .1
        assert set(measured['durations_ms']) == {'path_ledger_load_ms', 'event_closure_ms', 'event_prepare_ms',
            'receipt_validation_ms', 'asset_placement_and_view_build_ms', 'event_files_write_ms',
            'confirmation_prepare_ms', 'confirmation_view_build_ms', 'confirmation_files_write_ms'}
        relative = conn.execute('SELECT receipt_path FROM archive_outbox WHERE seq=?', (terminal,)).fetchone()[0]
        conn.execute('UPDATE archive_outbox SET readable_at=NULL WHERE seq=?', (terminal,))
    (root / relative).write_bytes(b'corrupted dependency')
    with pytest.raises(ValueError, match='corrupt receipt'):
        publish_events(archive, sequences={terminal})
    with archive.core['db']() as conn:
        assert conn.execute('SELECT readable_at FROM archive_outbox WHERE seq=?', (terminal,)).fetchone()[0] is None
    assert archive.status['last_event_publication']['status'] == 'failed'


def test_echo_reuses_only_assets_validated_within_that_publication(snapshots, monkeypatch):
    archive, add, root = snapshots
    raw = b'original evidence bytes'
    digest = hashlib.sha256(raw).hexdigest()
    item = {'path': '.System/PendingAssets/' + digest + '.png', 'sha256': digest, 'size_bytes': len(raw)}
    (root / item['path']).parent.mkdir(parents=True, exist_ok=True)
    (root / item['path']).write_bytes(raw)
    source = add('scans', 'photo', scan('photo') | {'image_sha256': digest})
    terminal = add('jobs', 'reading', job('reading', 'photo'), pending=True)
    with archive.core['db']() as conn:
        row = conn.execute('SELECT receipt_path FROM archive_outbox WHERE seq=?', (source,)).fetchone()
        receipt = json.loads((root / row[0]).read_text())
        receipt['artifacts'] = {'image': item}
        payload = json.dumps(receipt).encode()
        relative = f'.System/Receipts/{source:012d}-{hashlib.sha256(payload).hexdigest()}.json'
        (root / relative).write_bytes(payload)
        conn.execute('UPDATE archive_outbox SET receipt_path=? WHERE seq=?', (relative, source))
    place = ArchivePaths.place
    calls = []

    def actual_place(paths, artifact, target):
        calls.append(artifact['sha256'])
        return place(paths, artifact, target)

    monkeypatch.setattr(ArchivePaths, 'place', actual_place)
    publish_events(archive, sequences={terminal})
    assert calls == [digest]
    with archive.core['db']() as conn:
        conn.execute('UPDATE archive_outbox SET readable_at=NULL WHERE seq=?', (terminal,))
    publish_events(archive, sequences={terminal})
    assert calls == [digest, digest]


@pytest.mark.parametrize('corrupt', [False, True])
def test_timing_persistence_failure_cannot_hide_publication_result(snapshots, corrupt):
    archive, add, root = snapshots
    add('scans', 'photo', scan('photo'))
    terminal = add('jobs', 'reading', job('reading', 'photo'), pending=True)
    original_db = archive.core['db']
    if corrupt:
        with original_db() as conn:
            path = conn.execute('SELECT receipt_path FROM archive_outbox WHERE seq=?', (terminal,)).fetchone()[0]
        (root / path).write_bytes(b'corrupt')

    class DiagnosticsUnavailable:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, parameters=()):
            if sql == 'INSERT OR REPLACE INTO archive_meta VALUES(?,?)' and parameters[0].startswith('event_publication:'):
                raise sqlite3.OperationalError('diagnostic store is full')
            return self.conn.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    @contextmanager
    def unavailable_telemetry():
        with original_db() as conn:
            yield DiagnosticsUnavailable(conn)

    archive.core['db'] = unavailable_telemetry
    if corrupt:
        with pytest.raises(ValueError, match='corrupt receipt'):
            publish_events(archive)
    else:
        publish_events(archive)
        assert evidence(root)['observations'][0]['archive']['readable_at'] == STAMP
    assert archive.status['event_timing_persistence_error'] == 'OperationalError'
    assert archive.status['last_event_publication']['status'] == ('failed' if corrupt else 'published')


def test_scoped_delivery_preserves_another_events_failed_confirmation(snapshots, monkeypatch):
    archive, add, root = snapshots
    add('scans', 'first', scan('first'))
    old = add('jobs', 'old', job('old', 'first'), pending=True)
    retire = ArchivePaths.retire_views
    attempts = []

    def fail_echo(paths, views, **kwargs):
        attempts.append(True)
        if len(attempts) == 2:
            raise OSError('old echo unavailable')
        return retire(paths, views, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ArchivePaths, 'retire_views', fail_echo)
        with pytest.raises(OSError):
            publish_events(archive, sequences={old})
    add('scans', 'second', scan('second'))
    new = add('jobs', 'new', job('new', 'second'), pending=True)
    publish_events(archive, sequences={new})
    with archive.core['db']() as conn:
        obligations = json.loads(conn.execute("SELECT value FROM archive_meta WHERE key='event_confirmation_pending'").fetchone()[0])
        assert obligations == [old]
        assert conn.execute('SELECT readable_at FROM archive_outbox WHERE seq=?', (new,)).fetchone()[0] == STAMP
    publish_events(archive, sequences={old})
    with archive.core['db']() as conn:
        assert json.loads(conn.execute("SELECT value FROM archive_meta WHERE key='event_confirmation_pending'").fetchone()[0]) == []
