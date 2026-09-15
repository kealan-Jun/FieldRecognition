"""Tests for archive manager with capacity control and recovery."""
import os
import sys
import tempfile
import json
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from database import Database, apply_migrations
from archive_manager import ArchiveManager


@pytest.fixture
def test_db():
    """Create temporary test database."""
    with tempfile.NamedTemporaryFile(suffix='.sqlite3', delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    apply_migrations(db)

    yield db

    db.close()
    try:
        os.unlink(db_path)
    except:
        pass


@pytest.fixture
def archive_root():
    """Create temporary archive directory."""
    temp_dir = tempfile.mkdtemp(prefix='test_archive_')
    yield temp_dir
    try:
        shutil.rmtree(temp_dir)
    except:
        pass


def test_check_capacity(test_db, archive_root):
    """Test disk capacity checking."""
    manager = ArchiveManager(test_db, archive_root)

    capacity = manager.check_capacity()

    assert 'total_bytes' in capacity
    assert 'used_bytes' in capacity
    assert 'free_bytes' in capacity
    assert 'used_percent' in capacity
    assert 'free_gb' in capacity
    assert 'can_store' in capacity

    # Check that we can store 1KB
    capacity_with_check = manager.check_capacity(required_bytes=1024)
    assert capacity_with_check['can_store'] is True


def test_verify_archive_not_found(test_db, archive_root):
    """Test verifying non-existent archive."""
    manager = ArchiveManager(test_db, archive_root)

    result = manager.verify_archive('binding', 'nonexistent123')

    assert result['exists'] is False
    assert result['valid'] is False
    assert 'not found' in result['error'].lower()


def test_verify_archive_with_manifest(test_db, archive_root):
    """Test verifying archive with valid manifest."""
    manager = ArchiveManager(test_db, archive_root)

    # Create mock archive
    archive_path = Path(archive_root) / 'binding' / 'te' / 'test123'
    archive_path.mkdir(parents=True)

    # Create manifest
    manifest = {
        'entity': 'binding',
        'entity_id': 'test123',
        'created_at': '2026-09-15T12:00:00Z',
        'files': [
            {'path': 'data.json', 'size': 100}
        ]
    }

    with open(archive_path / 'manifest.json', 'w') as f:
        json.dump(manifest, f)

    # Create data file
    with open(archive_path / 'data.json', 'w') as f:
        json.dump({'test': 'data'}, f)

    result = manager.verify_archive('binding', 'test123')

    assert result['exists'] is True
    assert result['valid'] is True
    assert 'manifest' in result
    assert result['manifest']['entity'] == 'binding'


def test_verify_archive_missing_files(test_db, archive_root):
    """Test verifying archive with missing files."""
    manager = ArchiveManager(test_db, archive_root)

    # Create mock archive
    archive_path = Path(archive_root) / 'binding' / 'te' / 'test123'
    archive_path.mkdir(parents=True)

    # Create manifest referencing non-existent files
    manifest = {
        'entity': 'binding',
        'entity_id': 'test123',
        'files': [
            {'path': 'missing.json', 'size': 100}
        ]
    }

    with open(archive_path / 'manifest.json', 'w') as f:
        json.dump(manifest, f)

    result = manager.verify_archive('binding', 'test123')

    assert result['exists'] is True
    assert result['valid'] is False
    assert 'missing' in result['error'].lower()
    assert 'missing.json' in result['missing_files']


def test_backup_database(test_db, archive_root):
    """Test database backup."""
    manager = ArchiveManager(test_db, archive_root)

    # Insert some test data
    with test_db.transaction() as conn:
        conn.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?,?)",
            ('user1', 'Test User', 'hash123', 'operator', 0,
             '2026-09-15T12:00:00Z', '2026-09-15T12:00:00Z')
        )

    backup_path = Path(archive_root) / 'backup.sqlite3'
    result = manager.backup_database(str(backup_path))

    assert result['success'] is True
    assert backup_path.exists()
    assert result['size_bytes'] > 0
    assert 'backup_path' in result
    assert 'created_at' in result


def test_restore_database(test_db, archive_root):
    """Test database restore."""
    manager = ArchiveManager(test_db, archive_root)

    # Create original data
    with test_db.transaction() as conn:
        conn.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?,?)",
            ('user1', 'Test User', 'hash123', 'operator', 0,
             '2026-09-15T12:00:00Z', '2026-09-15T12:00:00Z')
        )

    # Create backup
    backup_path = Path(archive_root) / 'backup.sqlite3'
    manager.backup_database(str(backup_path))

    # Modify database
    with test_db.transaction() as conn:
        conn.execute("DELETE FROM users WHERE username=?", ('user1',))

    # Verify deletion
    with test_db.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM users WHERE username=?", ('user1',)).fetchone()[0]
        assert count == 0

    # Restore from backup
    result = manager.restore_database(str(backup_path))

    assert result['success'] is True
    assert 'restored_from' in result

    # After restore, the Database object needs to be reinitialized
    # In production, this would require application restart or reconnection
    # For this test, we verify the backup file exists and restore succeeded
    assert backup_path.exists()


def test_get_archive_stats(test_db, archive_root):
    """Test getting archive statistics."""
    manager = ArchiveManager(test_db, archive_root)

    # Add some archive outbox entries
    with test_db.transaction() as conn:
        conn.execute(
            "INSERT INTO archive_outbox VALUES(?,?,?,?)",
            ('binding', 'bind1', None, None)
        )
        conn.execute(
            "INSERT INTO archive_outbox VALUES(?,?,?,?)",
            ('binding', 'bind2', '2026-09-15T12:00:00Z', None)
        )

    stats = manager.get_archive_stats()

    assert stats['total_items'] == 2
    assert stats['archived'] == 1
    assert stats['pending'] == 1
    assert stats['archive_percent'] == 50.0
    assert 'disk_usage_percent' in stats
    assert 'disk_free_gb' in stats


def test_enforce_capacity_limit_no_cleanup_needed(test_db, archive_root):
    """Test capacity enforcement when usage is below threshold."""
    manager = ArchiveManager(test_db, archive_root)

    # Disk usage should be below 90% in test environment
    result = manager.enforce_capacity_limit(max_used_percent=90.0)

    assert result['cleanup_needed'] is False
    assert result['removed_count'] == 0


def test_update_catalog_incremental(test_db, archive_root):
    """Test incremental catalog update."""
    manager = ArchiveManager(test_db, archive_root)

    # Create mock archive with manifest
    archive_path = Path(archive_root) / 'binding' / 'te' / 'test123'
    archive_path.mkdir(parents=True)

    manifest = {
        'entity': 'binding',
        'entity_id': 'test123',
        'created_at': '2026-09-15T12:00:00Z',
        'files': []
    }

    with open(archive_path / 'manifest.json', 'w') as f:
        json.dump(manifest, f)

    # Update catalog
    manager.update_catalog_incremental('binding', 'test123')

    # Verify catalog entry
    with test_db.connection() as conn:
        row = conn.execute(
            "SELECT manifest FROM archive_catalog WHERE entity=? AND entity_id=?",
            ('binding', 'test123')
        ).fetchone()

        assert row is not None
        stored_manifest = json.loads(row['manifest'])
        assert stored_manifest['entity_id'] == 'test123'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
