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
from scripts.upgrade_runtime import apply


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


@pytest.mark.parametrize('break_prepare', [False, True])
def test_deploy_stops_before_code_change_and_never_starts_after_failed_prepare(tmp_path, break_prepare):
    calls = []
    def run(root, args, **kwargs):
        calls.append(args)
        out = ''
        if args[:2] == ['git', 'rev-parse']:
            out = 'b' * 40 if '--verify' in args else 'a' * 40
        if 'MainPID' in args:
            out = '0'
        if 'LoadState' in args:
            out = 'not-found'
        if args[-2:] == ['-m', 'runtime_prepare'] and break_prepare:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout=out, returncode=0)
    if break_prepare:
        with pytest.raises(subprocess.CalledProcessError):
            apply(tmp_path, 'tested-commit', run=run)
        assert not any(args[:3] == ['systemctl','--user','start'] for args in calls)
    else:
        assert apply(tmp_path, 'tested-commit', run=run)['after'] == 'b' * 40
        stop = next(i for i,a in enumerate(calls) if a[:3] == ['systemctl','--user','stop'])
        merge = next(i for i,a in enumerate(calls) if a[:2] == ['git','merge'])
        prepare = next(i for i,a in enumerate(calls) if a[-2:] == ['-m','runtime_prepare'])
        start = next(i for i,a in enumerate(calls) if a[:3] == ['systemctl','--user','start'])
        assert stop < merge < prepare < start
