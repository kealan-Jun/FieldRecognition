"""Migration rehearsals use synthetic SQLite files under pytest's temporary directory."""
import json
import sqlite3
from contextlib import closing

import pytest

import database
import migrate_db


def legacy_database(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript('''
            CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,role TEXT NOT NULL,password_hash TEXT,
                created_at TEXT NOT NULL,disabled INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE camera_users(camera_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id));
            CREATE TABLE bindings(id TEXT PRIMARY KEY,camera TEXT,ended TEXT,document TEXT NOT NULL);
            CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT,document TEXT NOT NULL);
            INSERT INTO users VALUES('person-a','alice','甲','operator',NULL,'2026-09-01T00:00:00Z',0);
            INSERT INTO users VALUES('person-b','bob','乙','operator',NULL,'2026-09-01T00:00:00Z',0);
            INSERT INTO camera_users VALUES('camera','person-a');
        ''')
        old_binding = {'binding_id': 'old-binding', 'operator': '甲', 'wearer_id': 'person-a',
                       'started_at': '2026-09-18T09:00:00+08:00', 'ended_at': '2026-09-18T10:00:00+08:00'}
        old_job = {'job_id': 'old-job', 'operator': '甲', 'binding': old_binding,
                   'result': {'raw_text': '0.0010 g'}}
        conn.execute('INSERT INTO bindings VALUES(?,?,?,?)',
                     ('old-binding', 'camera', old_binding['ended_at'], json.dumps(old_binding)))
        conn.execute('INSERT INTO jobs VALUES(?,?,?)', ('old-job', 'completed', json.dumps(old_job)))
        conn.commit()
    return path


def history(path):
    with closing(sqlite3.connect(path)) as conn:
        return {table: conn.execute('SELECT id,document FROM ' + table + ' ORDER BY id').fetchall()
                for table in ('bindings', 'jobs')}


def test_read_only_missing_path_and_no_implicit_write(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert migrate_db.main(['--status']) == 0
    assert not (tmp_path / 'Data').exists()
    with pytest.raises(SystemExit) as exc:
        migrate_db.main([])
    assert exc.value.code == 2
    missing = tmp_path / 'nested' / 'missing.sqlite3'
    assert migrate_db.main(['--db', str(missing), '--dry-run']) == 1
    assert migrate_db.main(['--db', str(missing)]) == 1
    assert not missing.parent.exists()
    capsys.readouterr()


def test_preflight_does_not_change_old_schema_or_rows(tmp_path):
    path = legacy_database(tmp_path / 'old.sqlite3')
    before = path.read_bytes()
    report = migrate_db.inspect_database(path)
    assert not report['errors']
    assert report['integrity_check'] == ['ok'] and report['foreign_key_check'] == []
    assert report['counts']['camera_users'] == 1
    assert report['status']['pending']
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()


def test_old_membership_migration_preserves_history_and_backup(tmp_path):
    path = legacy_database(tmp_path / 'old.sqlite3')
    before = history(path)
    backup = tmp_path / 'before.sqlite3'
    result = migrate_db.migrate_database(path, backup)
    assert result['backup'] == str(backup)
    assert result['after']['status']['pending'] == []
    assert history(path) == before == history(backup)
    with closing(sqlite3.connect(backup)) as conn:
        assert [(row[1], row[5]) for row in conn.execute('PRAGMA table_info(camera_users)')] == [
            ('camera_id', 1), ('user_id', 0)]
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('SELECT * FROM camera_active_users').fetchall() == [('camera', 'person-a')]
        conn.execute("INSERT INTO camera_users VALUES('camera','person-b')")
        conn.execute("INSERT OR IGNORE INTO camera_users VALUES('camera','person-a')")
        conn.commit()
        assert conn.execute('SELECT count(*) FROM camera_users').fetchone()[0] == 2
        # Withdrawing eligibility cannot erase the old person's bindings or readings.
        conn.execute("DELETE FROM camera_users WHERE user_id='person-a'")
        conn.commit()
    assert history(path) == before
    assert migrate_db.migrate_database(path)['applied'] == []
    assert backup.stat().st_mode & 0o777 == 0o600


def test_backup_includes_committed_wal_and_restore_refuses_overwrite(tmp_path):
    path = legacy_database(tmp_path / 'wal.sqlite3')
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA wal_autocheckpoint=0')
        conn.execute("INSERT INTO camera_users VALUES('second-camera','person-b')")
        conn.commit()
        backup = migrate_db.backup_database(path, tmp_path / 'backup.sqlite3')
        with closing(sqlite3.connect(backup)) as check:
            assert check.execute('SELECT count(*) FROM camera_users').fetchone()[0] == 2
    restored = tmp_path / 'restored.sqlite3'
    assert migrate_db.main(['--restore-from', str(backup), '--db', str(restored)]) == 0
    original = restored.read_bytes()
    assert migrate_db.main(['--restore-from', str(backup), '--db', str(restored)]) == 1
    assert restored.read_bytes() == original
    assert history(restored) == history(path)


def test_foreign_key_failure_blocks_migration_before_backup(tmp_path):
    path = legacy_database(tmp_path / 'invalid.sqlite3')
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO camera_users VALUES('orphan-camera','missing-user')")
        conn.commit()
    before = path.read_bytes()
    report = migrate_db.inspect_database(path)
    assert report['foreign_key_check']
    backup = tmp_path / 'never-created.sqlite3'
    assert migrate_db.main(['--db', str(path), '--backup', str(backup)]) == 1
    assert path.read_bytes() == before and not backup.exists()


def test_unknown_future_version_is_not_downgraded(tmp_path):
    path = legacy_database(tmp_path / 'future.sqlite3')
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT,applied_at TEXT)')
        conn.execute("INSERT INTO schema_migrations VALUES(99999,'future','now')")
        conn.commit()
    assert migrate_db.inspect_database(path)['errors'] == ['unknown_schema_version']
    assert migrate_db.main(['--db', str(path)]) == 1
    assert list(tmp_path.glob('*.before-*')) == []


def test_transaction_failure_keeps_history_and_verified_backup(tmp_path, monkeypatch):
    path = legacy_database(tmp_path / 'failure.sqlite3')
    before = history(path)
    migrations = database.MIGRATIONS + [{'version': 1000, 'name': 'injected_failure',
        'up': 'CREATE TABLE should_rollback(id TEXT); SELECT * FROM missing_table;', 'down': None}]
    monkeypatch.setattr(database, 'MIGRATIONS', migrations)
    monkeypatch.setattr(migrate_db, 'MIGRATIONS', migrations)
    backup = tmp_path / 'safe.sqlite3'
    assert migrate_db.main(['--db', str(path), '--backup', str(backup)]) == 1
    assert history(path) == before == history(backup)
    with closing(sqlite3.connect(path)) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        assert [(row[1], row[5]) for row in conn.execute('PRAGMA table_info(camera_users)')] == [
            ('camera_id', 1), ('user_id', 0)]


def test_corrupt_file_and_existing_backup_are_refused(tmp_path):
    corrupt = tmp_path / 'corrupt.sqlite3'
    corrupt.write_bytes(b'not a SQLite file')
    assert migrate_db.main(['--db', str(corrupt), '--check']) == 1
    assert migrate_db.main(['--db', str(corrupt)]) == 1
    valid = legacy_database(tmp_path / 'valid.sqlite3')
    preserved = corrupt.read_bytes()
    assert migrate_db.main(['--db', str(valid), '--backup', str(corrupt)]) == 1
    assert corrupt.read_bytes() == preserved
    assert migrate_db.inspect_database(valid)['status']['applied_count'] == 0
