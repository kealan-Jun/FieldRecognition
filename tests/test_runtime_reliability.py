"""Exercise delayed dependencies and failed upgrades without touching real units."""
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from database import Database, apply_migrations
import runtime_prepare
from scripts.recover_ocr_api import recover, UNIT
from scripts.upgrade_runtime import DEPLOYMENT_UNITS, apply, non_runtime_path, plan


def test_mount_dependency_failure_is_retried_until_service_runs():
    calls = []
    states = iter(['inactive', 'inactive', 'active'])
    def run(args, **kwargs):
        calls.append(args)
        if args[1] == 'is-enabled':
            return SimpleNamespace(returncode=0, stdout='enabled\n')
        if args[1] == 'show':
            return SimpleNamespace(stdout=next(states))
        assert args == ['systemctl', 'start', '--no-block', UNIT]
        return SimpleNamespace(returncode=0)
    assert recover(run=run, paused=False)['status'] == 'start_requested'
    assert recover(run=run, paused=False)['status'] == 'start_requested'
    assert recover(run=run, paused=False)['status'] == 'active'
    assert sum(command[1] == 'start' for command in calls) == 2


def test_recovery_respects_maintenance_and_disabled_service():
    assert recover(run=lambda *a, **k: pytest.fail('Must not call systemctl'), paused=True)['status'] == 'paused'
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout='disabled')
    assert recover(run=run, paused=False)['status'] == 'disabled'
    assert len(calls) == 1


def old_database(tmp_path):
    path = tmp_path / 'Database.sqlite3'
    apply_migrations(Database(path))
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM schema_migrations WHERE version=14')
        conn.execute("INSERT INTO jobs(id,status,document) VALUES('old','completed',?)",
                     (json.dumps({'job_id':'old','original':'0.0010 g','captured_at':'2026-09-15T12:00:00+08:00'}),))
    return path


def test_boot_rehearses_backs_up_migrates_and_preserves_documents(tmp_path):
    path = old_database(tmp_path)
    before = runtime_prepare.history(path)
    checks = []
    result = runtime_prepare.prepare(path, assert_stopped=lambda: checks.append(True))
    assert len(checks) == 2
    assert result['after']['status']['current_version'] == 14
    assert result['rehearsal'] == result['restore_rehearsal'] == 'passed'
    assert runtime_prepare.history(path) == before == runtime_prepare.history(result['backup'])
    assert Path(result['backup']).stat().st_mode & 0o777 == 0o600
    assert json.loads(Path(result['receipt']).read_text())['history_preserved']
    assert runtime_prepare.prepare(path)['applied'] == []


def test_live_writer_blocks_migration_before_any_backup(tmp_path):
    path = old_database(tmp_path)
    def busy():
        raise RuntimeError('writer still active')
    with pytest.raises(RuntimeError, match='writer still active'):
        runtime_prepare.prepare(path, assert_stopped=busy)
    assert runtime_prepare.inspect_database(path)['status']['current_version'] == 13
    assert not list(tmp_path.glob('*.before-*'))


def test_failed_rehearsal_never_changes_live_database(tmp_path, monkeypatch):
    path = old_database(tmp_path)
    before = path.read_bytes()
    def fail(_):
        raise ValueError('injected migration failure')
    monkeypatch.setattr(runtime_prepare, 'migrate_database', fail)
    with pytest.raises(ValueError, match='injected migration failure'):
        runtime_prepare.prepare(path, assert_stopped=lambda: None)
    assert path.read_bytes() == before


def test_cold_import_failure_prevents_database_mutation(tmp_path, monkeypatch):
    path = old_database(tmp_path)
    before = path.read_bytes()
    def fail():
        raise ImportError('missing code dependency')
    monkeypatch.setattr(runtime_prepare, 'check_imports', fail)
    with pytest.raises(ImportError):
        runtime_prepare.prepare(path, assert_stopped=lambda: None)
    assert path.read_bytes() == before


def test_runtime_import_fault_does_not_exhaust_photos_and_recovers(tmp_path):
    from ocr_worker import OCRWorker
    path = old_database(tmp_path)
    db = Database(path)
    def missing_dependency(task):
        raise ImportError('private configuration must not appear in a receipt')
    worker = OCRWorker('test', None, core={'database': db, 'run_ocr': missing_dependency})
    worker.queue.enqueue({'job_id': 'waiting-photo', 'camera_id': 'Camera', 'capture_id': 'immutable-source'})
    for _ in range(6):
        task = worker.queue.claim_task()
        assert task['attempt_count'] == 1
        worker._process_ocr_task(task)
        with db.connection() as conn:
            row = conn.execute("SELECT status,document,attempt_count FROM jobs WHERE id='waiting-photo'").fetchone()
            assert row['status'] == 'queued' and row['attempt_count'] == 0
            assert 'private configuration' not in row['document']
            assert json.loads(row['document'])['last_error'] == 'ImportError'
            conn.execute("UPDATE jobs SET next_attempt_at=0 WHERE id='waiting-photo'")
    worker.core['run_ocr'] = lambda task: worker.queue.complete_task(task['job_id'], {'lines': []})
    worker._process_ocr_task(worker.queue.claim_task())
    with db.connection() as conn:
        result = json.loads(conn.execute("SELECT document FROM jobs WHERE id='waiting-photo'").fetchone()[0])
    assert result['status'] == 'completed' and result['dependency_wait_count'] == 6
    assert result['capture_id'] == 'immutable-source'


@pytest.mark.parametrize('break_prepare', [False, True])
def test_offline_deploy_checks_before_code_change_and_never_starts_after_failed_prepare(tmp_path, break_prepare):
    calls = []
    def run(root, args, **kwargs):
        calls.append(args)
        out = ''
        if args[:2] == ['git', 'rev-parse']:
            out = 'b' * 40 if '--verify' in args else 'a' * 40
        if args[:2] == ['git', 'diff']:
            out = 'app.py\0'
        if args[:3] == ['systemctl', '--user', 'show']:
            out = 'LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlPID=0\n'
        if args[-2:] == ['-m', 'runtime_prepare'] and break_prepare:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout=out, returncode=0)
    if break_prepare:
        with pytest.raises(subprocess.CalledProcessError):
            apply(tmp_path, 'tested-commit', run=run)
        assert not any(args[:3] == ['systemctl','--user','start'] for args in calls)
    else:
        assert apply(tmp_path, 'tested-commit', run=run)['after'] == 'b' * 40
        checked = max(i for i,a in enumerate(calls) if a[:3] == ['systemctl','--user','show'])
        merge = next(i for i,a in enumerate(calls) if a[:2] == ['git','merge'])
        prepare = next(i for i,a in enumerate(calls) if a[-2:] == ['-m','runtime_prepare'])
        start = next(i for i,a in enumerate(calls) if a[:3] == ['systemctl','--user','start'])
        assert checked < merge < prepare < start
    assert not any(args[:3] == ['systemctl','--user','stop'] for args in calls)


def revision_runner(paths, state='LoadState=loaded\nActiveState=active\nMainPID=42\nControlPID=0\n'):
    calls = []
    def run(root, args, **kwargs):
        calls.append(args)
        out = ''
        if args[:2] == ['git', 'rev-parse']:
            out = 'b' * 40 if '--verify' in args else 'a' * 40
        if args[:2] == ['git', 'diff']:
            out = '\0'.join(paths) + ('\0' if paths else '')
        if args[:3] == ['systemctl', '--user', 'show']:
            out = state
        return SimpleNamespace(stdout=out, returncode=0)
    return run, calls


@pytest.mark.parametrize('state', [
    'LoadState=loaded\nActiveState=active\nMainPID=42\nControlPID=0\n',
    'LoadState=loaded\nActiveState=activating\nMainPID=0\nControlPID=0\n',
    'LoadState=loaded\nActiveState=deactivating\nMainPID=0\nControlPID=42\n',
    '',  # Inability to inspect the service must never become permission to deploy.
])
def test_live_upgrade_rejected_before_code_or_service_changes(tmp_path, state):
    run, calls = revision_runner(['README.md', 'receiver.py'], state)
    with pytest.raises(RuntimeError, match='Live runtime upgrade blocked'):
        apply(tmp_path, 'tested-commit', run=run)
    assert not any(args[:2] == ['git', 'merge'] for args in calls)
    assert not any(args[:2] == ['systemctl', '--user'] and args[2] != 'show' for args in calls)
    assert not any('runtime_prepare' in args or 'scripts/install_runtime.py' in args for args in calls)


@pytest.mark.parametrize('busy_unit', DEPLOYMENT_UNITS[1:])
def test_idle_target_does_not_hide_a_remaining_service_process(tmp_path, busy_unit):
    idle = 'LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlPID=0\n'
    base, calls = revision_runner(['app.py'], idle)
    def run(root, args, **kwargs):
        result = base(root, args, **kwargs)
        if args[:4] == ['systemctl', '--user', 'show', busy_unit]:
            result.stdout = idle.replace('MainPID=0', 'MainPID=42')
        return result
    with pytest.raises(RuntimeError, match='Live runtime upgrade blocked'):
        apply(tmp_path, 'tested-commit', run=run)
    assert not any(args[:2] == ['git', 'merge'] for args in calls)
    assert not any(args[:2] == ['systemctl', '--user'] and args[2] != 'show' for args in calls)


@pytest.mark.parametrize('path', [
    'app.py', 'receiver.py', 'static/app.js', 'static/index.html', '.env',
    'Requirements.lock.txt', 'deployment/field-recognition-api.service',
    'scripts/install_runtime.py', 'docs/assets/overview.svg', 'runtime_prepare.py',
])
def test_live_allowlist_excludes_runtime_and_served_assets(path):
    assert not non_runtime_path(path)


def test_non_runtime_revision_updates_without_service_or_database_calls(tmp_path):
    run, calls = revision_runner(['AGENTS.md', 'README.md', 'AgentTools.md',
        'docs/RuntimeReliability.md', 'tests/test_runtime_reliability.py', 'scripts/upgrade_runtime.py'])
    report = apply(tmp_path, 'tested-commit', run=run)
    assert report['mode'] == 'non_runtime' and not report['runtime_paths']
    assert all(args[0] == 'git' for args in calls)
    assert calls[-1] == ['git', 'merge', '--ff-only', 'b' * 40]


def test_inspection_and_unchanged_revision_never_restart_services(tmp_path):
    run, calls = revision_runner(['receiver.py'])
    report = plan(tmp_path, 'tested-commit', run=run)
    assert report['mode'] == 'offline_required' and report['runtime_paths'] == ['receiver.py']
    assert all(args[0] == 'git' and args[1] != 'merge' for args in calls)
    run, calls = revision_runner([])
    assert apply(tmp_path, run=run)['mode'] == 'unchanged'
    assert all(args[0] == 'git' and args[1] != 'merge' for args in calls)
