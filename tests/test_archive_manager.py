"""Archive recovery checks use the actual immutable receipt format."""
import hashlib
import json
import sqlite3
from pathlib import Path
import pytest
from database import Database,apply_migrations
from archive_manager import ArchiveManager
from archive_store import canonical,immutable_write

@pytest.fixture
def manager(tmp_path):
    db=Database(tmp_path/'state.sqlite3');apply_migrations(db)
    root=tmp_path/'FieldRecognitionArchive';root.mkdir()
    return ArchiveManager(db,root)


def add_receipt(manager):
    payload=b'original';sha=hashlib.sha256(payload).hexdigest()
    obj='Objects/'+sha[:2]+'/'+sha+'.png'
    immutable_write(manager.archive_root/obj,payload)
    document={'camera_id':'cam','binding_id':'id'}
    receipt={'schema':'field-recognition-receipt/1','sequence':1,'entity':'bindings','entity_id':'id','document':document,
        'artifacts':{'image':{'path':obj,'sha256':sha,'size_bytes':len(payload)}}}
    raw=canonical(receipt);path='Receipts/cam/000001-'+hashlib.sha256(raw).hexdigest()+'.json'
    immutable_write(manager.archive_root/path,raw)
    with manager.db.transaction() as c:
        c.execute('INSERT INTO archive_outbox(seq,entity,entity_id,document,recorded_at,archived_at,receipt_path) VALUES(?,?,?,?,?,?,?)',
            (1,'bindings','id',json.dumps(document),'2026-09-15T01:00:00Z','2026-09-15T01:00:01Z',path))
    return obj,path


def test_construction_does_not_create_mountpoint(tmp_path):
    ArchiveManager(Database(tmp_path/'db'),tmp_path/'Offline'/'Archive')
    assert not (tmp_path/'Offline').exists()


def test_missing_receipt_is_not_valid(manager):
    assert manager.verify_archive('bindings','id')['valid'] is False


def test_checksum_and_snapshot_are_required(manager,tmp_path):
    obj,path=add_receipt(manager)
    assert manager.verify_archive('bindings','id')['valid']
    result=manager.restore_archive('bindings','id',tmp_path/'Recovered')
    assert (Path(result['restored_to'])/obj).read_bytes()==b'original'
    (manager.archive_root/obj).write_bytes(b'corrupt!')
    assert not manager.verify_archive('bindings','id')['valid']
    with pytest.raises(ValueError):manager.restore_archive('bindings','id',tmp_path/'BadRecovery')
    assert not (tmp_path/'BadRecovery').exists()


def test_capacity_never_deletes_evidence(manager,monkeypatch):
    obj,path=add_receipt(manager)
    monkeypatch.setattr(manager,'check_capacity',lambda **kw:{'used_percent':99})
    assert manager.enforce_capacity_limit()['action']=='pause_writes'
    assert (manager.archive_root/obj).exists() and (manager.archive_root/path).exists()
    with manager.db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM archive_outbox WHERE archived_at IS NOT NULL').fetchone()[0]==1


def test_verified_catalog_and_pending_counts(manager):
    add_receipt(manager);manager.update_catalog_incremental('bindings','id')
    assert manager.get_archive_stats()['pending']==0
    with manager.db.connection() as conn:
        assert json.loads(conn.execute('SELECT manifest FROM archive_catalog').fetchone()[0])['valid']


def test_database_backup_and_restore_do_not_overwrite_live_wal(manager,tmp_path):
    with manager.db.connection() as conn:
        conn.execute('CREATE TABLE example(value TEXT)');conn.execute("INSERT INTO example VALUES('old')")
    backup=tmp_path/'Backup.sqlite3';manager.backup_database(backup)
    with pytest.raises(FileExistsError):manager.backup_database(backup)
    with manager.db.connection() as conn:conn.execute("UPDATE example SET value='new'")
    with pytest.raises(ValueError):manager.restore_database(backup)
    target=tmp_path/'Restored.sqlite3';manager.restore_database(backup,target_path=target)
    restored=sqlite3.connect(target)
    try:assert restored.execute('SELECT value FROM example').fetchone()[0]=='old'
    finally:restored.close()
    with manager.db.connection() as conn:assert conn.execute('SELECT value FROM example').fetchone()[0]=='new'
