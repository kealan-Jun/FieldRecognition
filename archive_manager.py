"""Archive management with capacity control and recovery utilities."""
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from database import Database


class ArchiveManager:
    """
    Enhanced archive management with capacity control and recovery.

    Features:
    - Disk capacity monitoring
    - Automatic cleanup of old archives
    - Incremental catalog updates
    - Backup and restore utilities
    - Archive verification
    """

    def __init__(self, db: Database, archive_root: str):
        self.db = db
        self.archive_root = Path(archive_root)
        self.archive_root.mkdir(parents=True, exist_ok=True)

    def check_capacity(self, required_bytes: int = 0) -> dict:
        """
        Check disk capacity and usage.

        Args:
            required_bytes: Optional check if this many bytes can be stored

        Returns:
            Dict with capacity info: total, used, free, percent, can_store
        """
        stat = shutil.disk_usage(self.archive_root)

        result = {
            'total_bytes': stat.total,
            'used_bytes': stat.used,
            'free_bytes': stat.free,
            'used_percent': (stat.used / stat.total * 100) if stat.total > 0 else 0,
            'free_gb': stat.free / (1024**3),
            'can_store': stat.free >= required_bytes
        }

        return result

    def enforce_capacity_limit(self, max_used_percent: float = 90.0,
                              target_percent: float = 80.0) -> dict:
        """
        Enforce capacity limits by removing oldest archives.

        Args:
            max_used_percent: Maximum allowed usage percentage
            target_percent: Target usage after cleanup

        Returns:
            Dict with cleanup stats
        """
        capacity = self.check_capacity()

        if capacity['used_percent'] < max_used_percent:
            return {
                'cleanup_needed': False,
                'current_usage': capacity['used_percent'],
                'removed_count': 0
            }

        # Calculate how much to free
        total = capacity['total_bytes']
        current_used = capacity['used_bytes']
        target_used = total * (target_percent / 100)
        bytes_to_free = current_used - target_used

        if bytes_to_free <= 0:
            return {
                'cleanup_needed': False,
                'current_usage': capacity['used_percent'],
                'removed_count': 0
            }

        # Find oldest archives
        removed_count = 0
        freed_bytes = 0

        with self.db.connection() as conn:
            # Get archived items ordered by age
            rows = conn.execute('''
                SELECT entity, entity_id, archived_at
                FROM archive_outbox
                WHERE archived_at IS NOT NULL
                ORDER BY archived_at ASC
            ''').fetchall()

            for row in rows:
                if freed_bytes >= bytes_to_free:
                    break

                # Get archive path
                entity_type = row['entity']
                entity_id = row['entity_id']
                archive_path = self._get_archive_path(entity_type, entity_id)

                if archive_path.exists():
                    size = self._get_directory_size(archive_path)
                    try:
                        shutil.rmtree(archive_path)
                        freed_bytes += size
                        removed_count += 1

                        # Mark as removed in database
                        conn.execute('''
                            UPDATE archive_outbox
                            SET archived_at = NULL
                            WHERE entity=? AND entity_id=?
                        ''', (entity_type, entity_id))

                    except Exception as e:
                        print(f"Failed to remove archive {archive_path}: {e}")

        final_capacity = self.check_capacity()

        return {
            'cleanup_needed': True,
            'initial_usage': capacity['used_percent'],
            'final_usage': final_capacity['used_percent'],
            'removed_count': removed_count,
            'freed_bytes': freed_bytes,
            'freed_gb': freed_bytes / (1024**3)
        }

    def verify_archive(self, entity_type: str, entity_id: str) -> dict:
        """
        Verify an archive's integrity.

        Args:
            entity_type: Type of entity (e.g., 'binding')
            entity_id: Entity identifier

        Returns:
            Verification result
        """
        archive_path = self._get_archive_path(entity_type, entity_id)

        if not archive_path.exists():
            return {
                'exists': False,
                'valid': False,
                'error': 'Archive not found'
            }

        # Check manifest
        manifest_path = archive_path / 'manifest.json'
        if not manifest_path.exists():
            return {
                'exists': True,
                'valid': False,
                'error': 'Manifest missing'
            }

        try:
            with open(manifest_path) as f:
                manifest = json.load(f)

            # Verify files listed in manifest exist
            missing_files = []
            for file_ref in manifest.get('files', []):
                file_path = archive_path / file_ref['path']
                if not file_path.exists():
                    missing_files.append(file_ref['path'])

            if missing_files:
                return {
                    'exists': True,
                    'valid': False,
                    'error': f'{len(missing_files)} files missing',
                    'missing_files': missing_files
                }

            return {
                'exists': True,
                'valid': True,
                'manifest': manifest,
                'size_bytes': self._get_directory_size(archive_path)
            }

        except Exception as e:
            return {
                'exists': True,
                'valid': False,
                'error': f'Verification failed: {e}'
            }

    def restore_archive(self, entity_type: str, entity_id: str,
                       target_dir: Optional[str] = None) -> dict:
        """
        Restore an archive to a target directory.

        Args:
            entity_type: Type of entity
            entity_id: Entity identifier
            target_dir: Target directory (default: temp directory)

        Returns:
            Restore result with path
        """
        archive_path = self._get_archive_path(entity_type, entity_id)

        if not archive_path.exists():
            raise FileNotFoundError(f'Archive not found: {archive_path}')

        # Verify first
        verification = self.verify_archive(entity_type, entity_id)
        if not verification['valid']:
            raise ValueError(f'Archive invalid: {verification.get("error")}')

        # Create target directory
        if target_dir is None:
            import tempfile
            target_dir = tempfile.mkdtemp(prefix=f'restore_{entity_type}_{entity_id}_')
        else:
            target_dir = Path(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)

        target_path = Path(target_dir)

        # Copy archive contents
        try:
            shutil.copytree(archive_path, target_path / entity_id, dirs_exist_ok=True)

            return {
                'success': True,
                'restored_to': str(target_path),
                'size_bytes': self._get_directory_size(target_path)
            }

        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def backup_database(self, backup_path: str) -> dict:
        """
        Create a backup of the database.

        Args:
            backup_path: Path for backup file

        Returns:
            Backup result
        """
        backup_file = Path(backup_path)
        backup_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Use SQLite backup API for consistent backup
            import sqlite3

            # Get database path
            db_path = self.db.path

            # Create backup connection
            source = sqlite3.connect(str(db_path))
            dest = sqlite3.connect(str(backup_file))

            # Perform backup
            source.backup(dest)

            source.close()
            dest.close()

            size = backup_file.stat().st_size

            return {
                'success': True,
                'backup_path': str(backup_file),
                'size_bytes': size,
                'size_mb': size / (1024**2),
                'created_at': datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def restore_database(self, backup_path: str) -> dict:
        """
        Restore database from backup.

        WARNING: This will replace the current database!

        Args:
            backup_path: Path to backup file

        Returns:
            Restore result
        """
        backup_file = Path(backup_path)

        if not backup_file.exists():
            raise FileNotFoundError(f'Backup not found: {backup_file}')

        try:
            # Close current database connections
            self.db.close()

            # Copy backup over current database
            shutil.copy2(backup_file, self.db.path)

            # Reinitialize database
            self.db._init_db()

            return {
                'success': True,
                'restored_from': str(backup_file),
                'restored_at': datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def update_catalog_incremental(self, entity_type: str, entity_id: str):
        """
        Incrementally update catalog for a single entity.

        Args:
            entity_type: Entity type
            entity_id: Entity identifier
        """
        archive_path = self._get_archive_path(entity_type, entity_id)

        if not archive_path.exists():
            return

        # Update catalog entry
        manifest_path = archive_path / 'manifest.json'
        if manifest_path.exists():
            with open(manifest_path) as f:
                manifest = json.load(f)

            # Store catalog entry in database
            with self.db.transaction() as conn:
                conn.execute('''
                    INSERT OR REPLACE INTO archive_catalog(entity, entity_id, manifest, indexed_at)
                    VALUES(?,?,?,?)
                ''', (entity_type, entity_id, json.dumps(manifest),
                      datetime.now(timezone.utc).isoformat()))

    def get_archive_stats(self) -> dict:
        """Get overall archive statistics."""
        with self.db.connection() as conn:
            total = conn.execute(
                'SELECT COUNT(*) FROM archive_outbox'
            ).fetchone()[0]

            archived = conn.execute(
                'SELECT COUNT(*) FROM archive_outbox WHERE archived_at IS NOT NULL'
            ).fetchone()[0]

            pending = total - archived

        capacity = self.check_capacity()

        return {
            'total_items': total,
            'archived': archived,
            'pending': pending,
            'archive_percent': (archived / total * 100) if total > 0 else 0,
            'disk_usage_percent': capacity['used_percent'],
            'disk_free_gb': capacity['free_gb']
        }

    def _get_archive_path(self, entity_type: str, entity_id: str) -> Path:
        """Get archive path for an entity."""
        return self.archive_root / entity_type / entity_id[:2] / entity_id

    def _get_directory_size(self, path: Path) -> int:
        """Calculate total size of directory."""
        total = 0
        for entry in path.rglob('*'):
            if entry.is_file():
                total += entry.stat().st_size
        return total
