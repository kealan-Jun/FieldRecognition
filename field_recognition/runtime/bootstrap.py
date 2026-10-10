"""Database checks and the legacy demonstration schema, executed by a factory."""

import json

from field_recognition.runtime.database import get_migration_status


def initialize_database(runtime):
    database = runtime.database
    if runtime["RUNTIME_ENABLED"] or runtime["AUTH_ENABLED"]:
        if get_migration_status(database)["pending"]:
            raise RuntimeError("Pending database migrations; run migrate_db.py before starting")
    if runtime["AUTH_ENABLED"]:
        with database.connection() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE role='admin' AND disabled=0").fetchone():
                raise RuntimeError("Create the initial administrator before starting")

    with database.connection() as conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS instruments(id TEXT PRIMARY KEY, name TEXT, scene TEXT, model TEXT);
        CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY, document TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bindings(id TEXT PRIMARY KEY, camera TEXT, ended TEXT, document TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, status TEXT, document TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS camera_resets(camera TEXT PRIMARY KEY, confirmed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS camera_service_state(camera TEXT PRIMARY KEY, document TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fallback_attempts(binding_id TEXT PRIMARY KEY, attempted_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS qr_device_registry(
            qr_hash TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL);
        """)
        if not runtime["RUNTIME_ENABLED"] and not runtime["AUTH_ENABLED"]:
            from field_recognition.runtime.database import SESSION_SCHEMA, execute_script

            execute_script(conn, SESSION_SCHEMA)
        conn.execute("""INSERT OR IGNORE INTO qr_device_registry(qr_hash,device_id,first_seen_at,last_seen_at)
            SELECT json_extract(document,'$.qr_hash'), json_extract(document,'$.instrument.id'),
                   json_extract(document,'$.started_at'), json_extract(document,'$.started_at')
              FROM bindings
             WHERE json_extract(document,'$.qr_hash') IS NOT NULL
               AND json_extract(document,'$.instrument.id') IS NOT NULL""")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(instruments)")}
        for column, declaration in [
            ("device_no", "TEXT"),
            ("measurement_ranges", "TEXT NOT NULL DEFAULT '{}'"),
            ("type_id", "TEXT"),
            ("device_category", "TEXT NOT NULL DEFAULT 'instrument'"),
        ]:
            if column not in columns:
                conn.execute(f"ALTER TABLE instruments ADD COLUMN {column} {declaration}")
        registry = json.loads((runtime.project_root / "InstrumentRegistry.json").read_text())
        for item in registry["instruments"]:
            conn.execute(
                """INSERT OR IGNORE INTO instruments
                (id,name,scene,model,measurement_ranges,type_id,device_category) VALUES(?,?,?,?,?,?,?)""",
                (
                    item["instrument_id"],
                    item["label"],
                    "",
                    "",
                    json.dumps(item.get("measurement_ranges") or {}, ensure_ascii=False),
                    item.get("type_id"),
                    item.get("device_category") or "instrument",
                ),
            )
