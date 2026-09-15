"""Database connection management and migration system for production."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator


class Database:
    """Thread-safe database connection manager with transaction support."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _init_db(self):
        """Initialize database with WAL mode and basic tables."""
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.close()

    def get_connection(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self.path), timeout=15)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute('PRAGMA foreign_keys=ON')
        return self._local.conn

    @contextmanager
    def transaction(self, mode: str = 'DEFERRED') -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager for database transactions.

        Args:
            mode: DEFERRED (default), IMMEDIATE, or EXCLUSIVE

        Usage:
            with db.transaction('IMMEDIATE') as conn:
                conn.execute(...)
        """
        conn = self.get_connection()
        conn.execute(f'BEGIN {mode}')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for non-transactional connection (read-only queries)."""
        conn = self.get_connection()
        try:
            yield conn
        finally:
            pass  # Don't close thread-local connection

    def close(self):
        """Close thread-local connection."""
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


MIGRATIONS = [
    {
        'version': 1,
        'name': 'add_user_accounts_and_auth',
        'description': 'Add user accounts, sessions, and authentication support',
        'up': '''
            CREATE TABLE IF NOT EXISTS users(
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'operator', 'reviewer')),
                password_hash TEXT,
                created_at TEXT NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

            CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                metadata TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

            CREATE TABLE IF NOT EXISTS device_credentials(
                device_id TEXT PRIMARY KEY,
                device_type TEXT NOT NULL,
                credential_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0
            );
        ''',
        'down': '''
            DROP TABLE IF EXISTS device_credentials;
            DROP TABLE IF EXISTS sessions;
            DROP TABLE IF EXISTS users;
        '''
    },
    {
        'version': 2,
        'name': 'add_task_queue_leases',
        'description': 'Add worker lease and retry management to jobs table',
        'up': '''
            -- Add columns only if they don't exist
            CREATE TABLE IF NOT EXISTS jobs_new(
                id TEXT PRIMARY KEY,
                status TEXT,
                document TEXT NOT NULL,
                lease_holder TEXT,
                lease_expires_at TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3
            );

            INSERT OR IGNORE INTO jobs_new(id, status, document, lease_holder, lease_expires_at, retry_count, max_retries)
            SELECT id, status, document,
                   COALESCE(lease_holder, NULL),
                   COALESCE(lease_expires_at, NULL),
                   COALESCE(retry_count, 0),
                   COALESCE(max_retries, 3)
            FROM jobs;

            DROP TABLE jobs;
            ALTER TABLE jobs_new RENAME TO jobs;

            CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(status, lease_expires_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_retry ON jobs(status, retry_count);
        ''',
        'down': None
    },
    {
        'version': 3,
        'name': 'add_camera_registry',
        'description': 'Support multiple cameras with independent contexts',
        'up': '''
            CREATE TABLE IF NOT EXISTS camera_registry(
                camera_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                device_credential_id TEXT,
                receiver_url TEXT,
                nas_photo_root TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                registered_at TEXT NOT NULL,
                metadata TEXT,
                FOREIGN KEY(device_credential_id) REFERENCES device_credentials(device_id)
            );
            CREATE INDEX IF NOT EXISTS idx_camera_enabled ON camera_registry(enabled);
        ''',
        'down': 'DROP TABLE IF EXISTS camera_registry;'
    },
    {
        'version': 4,
        'name': 'add_measurement_state_machine',
        'description': 'Add explicit measurement lifecycle states',
        'up': '''
            CREATE TABLE IF NOT EXISTS photo_measurements_new(
                id TEXT PRIMARY KEY,
                camera TEXT NOT NULL,
                burst_key TEXT NOT NULL,
                status TEXT NOT NULL,
                document TEXT NOT NULL,
                measurement_state TEXT NOT NULL DEFAULT 'draft',
                confirmed_at TEXT,
                confirmed_by TEXT,
                updated_at TEXT
            );

            INSERT INTO photo_measurements_new(id, camera, burst_key, status, document, measurement_state)
            SELECT id, camera, burst_key, status, document, 'draft'
            FROM photo_measurements
            WHERE EXISTS(SELECT 1 FROM photo_measurements LIMIT 1);

            DROP TABLE IF EXISTS photo_measurements_old;
            ALTER TABLE photo_measurements RENAME TO photo_measurements_old;
            ALTER TABLE photo_measurements_new RENAME TO photo_measurements;

            CREATE INDEX IF NOT EXISTS idx_measurements_state ON photo_measurements(measurement_state);
            CREATE INDEX IF NOT EXISTS idx_measurements_camera ON photo_measurements(camera);

            CREATE TABLE IF NOT EXISTS measurement_confirmations(
                id TEXT PRIMARY KEY,
                measurement_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('confirm', 'reject', 'revise')),
                reason TEXT,
                confirmed_at TEXT NOT NULL,
                fields_json TEXT,
                FOREIGN KEY(measurement_id) REFERENCES photo_measurements(id)
            );
            CREATE INDEX IF NOT EXISTS idx_confirmations_measurement ON measurement_confirmations(measurement_id);

            CREATE TABLE IF NOT EXISTS experiment_records(
                id TEXT PRIMARY KEY,
                document TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_experiment_records_created ON experiment_records(created_at);
        ''',
        'down': None
    },
    {
        'version': 5,
        'name': 'add_instrument_handoff',
        'description': 'Add explicit instrument handoff workflow',
        'up': '''
            -- Migrate existing binding_handoffs table if needed
            CREATE TABLE IF NOT EXISTS binding_handoffs_new(
                id TEXT PRIMARY KEY,
                binding_id TEXT,
                instrument_id TEXT,
                offered_by TEXT,
                offered_to TEXT,
                offered_at TEXT,
                state TEXT NOT NULL DEFAULT 'pending',
                accepted_at TEXT,
                rejected_at TEXT,
                rejection_reason TEXT,
                status TEXT,
                document TEXT NOT NULL
            );

            -- Copy existing data if table exists
            INSERT OR IGNORE INTO binding_handoffs_new(id, instrument_id, status, document, state, accepted_at, rejected_at, rejection_reason)
            SELECT id, instrument_id, status, document,
                   COALESCE(state, 'pending'), accepted_at, rejected_at, rejection_reason
            FROM binding_handoffs
            WHERE EXISTS(SELECT 1 FROM binding_handoffs LIMIT 1);

            DROP TABLE IF EXISTS binding_handoffs;
            ALTER TABLE binding_handoffs_new RENAME TO binding_handoffs;

            CREATE INDEX IF NOT EXISTS idx_handoffs_state ON binding_handoffs(state);
            CREATE INDEX IF NOT EXISTS idx_handoffs_binding ON binding_handoffs(binding_id);
            CREATE INDEX IF NOT EXISTS idx_handoffs_instrument ON binding_handoffs(instrument_id);
        ''',
        'down': None
    },
    {
        'version': 6,
        'name': 'add_instrument_configuration',
        'description': 'Externalize instrument types and measurement definitions',
        'up': '''
            CREATE TABLE IF NOT EXISTS instrument_types(
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                measurements_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            ALTER TABLE instruments ADD COLUMN type_id TEXT;
            CREATE INDEX IF NOT EXISTS idx_instruments_type ON instruments(type_id);
        ''',
        'down': 'DROP TABLE IF EXISTS instrument_types;'
    },
    {
        'version': 7,
        'name': 'add_audit_trail',
        'description': 'Add comprehensive audit logging',
        'up': '''
            CREATE TABLE IF NOT EXISTS audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_id TEXT,
                device_id TEXT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                changes_json TEXT,
                request_id TEXT,
                ip_address TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
        ''',
        'down': 'DROP TABLE IF EXISTS audit_log;'
    }
]


def apply_migrations(db: Database, dry_run: bool = False) -> list[dict]:
    """
    Apply all pending database migrations.

    Args:
        db: Database instance
        dry_run: If True, only report pending migrations without applying

    Returns:
        List of applied migration info
    """
    applied_migrations = []

    with db.transaction('IMMEDIATE') as conn:
        # Create migrations tracking table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS schema_migrations(
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                duration_ms INTEGER
            )
        ''')

        # Get already applied versions
        applied_versions = {
            row[0] for row in conn.execute('SELECT version FROM schema_migrations')
        }

        # Apply pending migrations
        for migration in MIGRATIONS:
            version = migration['version']

            if version in applied_versions:
                continue

            if dry_run:
                applied_migrations.append({
                    'version': version,
                    'name': migration['name'],
                    'status': 'pending',
                    'description': migration.get('description', '')
                })
                continue

            start = datetime.now()

            try:
                # Execute migration SQL
                conn.executescript(migration['up'])

                # Record migration
                duration_ms = int((datetime.now() - start).total_seconds() * 1000)
                conn.execute(
                    'INSERT INTO schema_migrations VALUES(?,?,?,?)',
                    (version, migration['name'],
                     datetime.now(timezone.utc).isoformat(),
                     duration_ms)
                )

                applied_migrations.append({
                    'version': version,
                    'name': migration['name'],
                    'status': 'applied',
                    'duration_ms': duration_ms
                })

            except Exception as e:
                applied_migrations.append({
                    'version': version,
                    'name': migration['name'],
                    'status': 'failed',
                    'error': str(e)
                })
                raise

    return applied_migrations


def get_migration_status(db: Database) -> dict:
    """Get current migration status."""
    with db.connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS schema_migrations(
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                duration_ms INTEGER
            )
        ''')

        applied = {
            row['version']: dict(row)
            for row in conn.execute('SELECT * FROM schema_migrations ORDER BY version')
        }

    status = {
        'current_version': max(applied.keys()) if applied else 0,
        'latest_version': max(m['version'] for m in MIGRATIONS),
        'applied_count': len(applied),
        'total_count': len(MIGRATIONS),
        'pending': []
    }

    for migration in MIGRATIONS:
        if migration['version'] not in applied:
            status['pending'].append({
                'version': migration['version'],
                'name': migration['name'],
                'description': migration.get('description', '')
            })

    return status
