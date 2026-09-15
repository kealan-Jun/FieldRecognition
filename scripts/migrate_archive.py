"""Upgrade this project's archive without importing camera or OCR services."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import shlex
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from archive_manager import ArchiveManager
from archive_paths import resolve_file
from archive_store import ArchiveStore
from database import Database
from worker_support import process_lock


def load_settings(path):
    """Read only archive/path settings; never evaluate a shell or load credentials."""
    allowed = {'FIELD_DEMO_DATA', 'FIELD_DATABASE_PATH', 'FIELD_ARCHIVE_ENABLED', 'FIELD_ARCHIVE_ROOT',
               'FIELD_ARCHIVE_MOUNT', 'FIELD_ARCHIVE_MIN_FREE_BYTES', 'FIELD_ARCHIVE_CHECK_SECONDS'}
    for line in path.read_text().splitlines():
        name, separator, value = line.strip().partition('=')
        if separator and name in allowed and name not in os.environ:
            parts = shlex.split(value, comments=True)
            if len(parts) != 1:
                raise ValueError('Archive setting requires one quoted or unquoted value: ' + name)
            os.environ[name] = parts[0]


def main():
    parser = argparse.ArgumentParser(description='验证并迁移 FieldRecognition NAS 归档；须先停止归档 worker')
    parser.add_argument('--apply', action='store_true', help='备份、迁移并完整校验；默认仅检查')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    load_settings(BASE/'.env')
    data = Path(os.environ.get('FIELD_DEMO_DATA', BASE/'Data'))
    database = Database(os.environ.get('FIELD_DATABASE_PATH') or data/'Demo.sqlite3')
    if not database.path.is_file():
        raise ValueError('Existing project database required')
    core = {'DATA':data, 'db':database.connection, 'now':lambda:datetime.now(timezone.utc).isoformat()}
    with process_lock(data, 'archive'):
        archive = ArchiveStore(core)
        if not archive.enabled():
            raise ValueError('Archive must be enabled')
        root = archive._root(create=False)
        report = archive.integrity.run_once()
        print(json.dumps({'phase':'BeforeCheck', 'status':report['status'], 'receipts':report['verified_receipts'],
                          'objects':report['verified_objects'], 'issues':report['issue_count']}), flush=True)
        if report['status'] != 'completed':
            raise ValueError('Archive integrity check must pass before migration')
        if not args.apply:
            return
        backup = (args.backup_dir or BASE/'Verification'/('ArchiveMigration'+datetime.now().strftime('%Y%m%d-%H%M%S'))).resolve()
        if backup.is_relative_to(root):
            raise ValueError('Backup must be outside the archive')
        backup.mkdir(parents=True, exist_ok=False, mode=0o700)
        ArchiveManager(database, root).backup_database(backup/'Database.sqlite3')
        os.chmod(backup/'Database.sqlite3', 0o600)
        inventory = {}
        with database.connection() as conn:
            rows = conn.execute('SELECT * FROM archive_outbox WHERE archived_at IS NOT NULL ORDER BY seq').fetchall()
        for row in rows:
            relative = row['receipt_path']; raw = resolve_file(root, relative).read_bytes()
            inventory[relative] = hashlib.sha256(raw).hexdigest()
            for item in json.loads(raw).get('artifacts', {}).values():
                if isinstance(item, dict):
                    inventory[item['path']] = item['sha256']
        (backup/'Inventory.json').write_text(json.dumps(inventory, indent=2))
        print(json.dumps({'phase':'Backup', 'directory':str(backup), 'files':len(inventory)}), flush=True)
        result = archive.write_index()
        print(json.dumps({'phase':'Migrated', **result}), flush=True)
        # Read the real bytes again through every historical alias.
        for relative, digest in inventory.items():
            if hashlib.sha256(resolve_file(root, relative).read_bytes()).hexdigest() != digest:
                raise ValueError('Migration changed historical bytes: ' + relative)
        report = archive.integrity.run_once()
        archive.integrity.publish_pending()
        archive.write_index()
        summary = {'status':report['status'], 'issues':report['issue_count'], 'receipts':report['verified_receipts'],
                   'objects':report['verified_objects'], 'inventory_verified':len(inventory), **result}
        (backup/'Verification.json').write_text(json.dumps(summary, indent=2))
        print(json.dumps({'phase':'AfterCheck', **summary}), flush=True)
        if report['status'] != 'completed':
            raise ValueError('Post-migration verification failed')


if __name__ == '__main__':
    main()
