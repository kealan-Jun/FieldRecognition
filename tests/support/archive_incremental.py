"""Reusable archive_incremental fixtures and doubles."""

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

STAMP = "2026-10-08T01:00:00+00:00"


@pytest.fixture
def snapshots(tmp_path):
    database = tmp_path / "Archive.sqlite3"
    root = tmp_path / "FieldRecognitionArchive"
    root.mkdir()

    @contextmanager
    def db():
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    with db() as conn:
        conn.executescript("""
            CREATE TABLE archive_outbox(seq INTEGER PRIMARY KEY,entity TEXT,entity_id TEXT,
                document TEXT,recorded_at TEXT,archived_at TEXT,receipt_path TEXT,
                published_at TEXT,readable_at TEXT,readability_basis TEXT);
            CREATE INDEX archive_entity_version ON archive_outbox(entity,entity_id,seq);
            CREATE TABLE archive_meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE jobs(id TEXT PRIMARY KEY,document TEXT);
            CREATE INDEX jobs_capture_lookup ON jobs(json_extract(document,'$.capture_id'));
            CREATE TABLE experiment_records(id TEXT PRIMARY KEY,document TEXT);
            CREATE TABLE bindings(id TEXT PRIMARY KEY,document TEXT);
        """)

    def add(entity, ident, doc, *, pending=False, current=True, receipt=True):
        raw_document = json.dumps(doc)
        with db() as conn:
            cursor = conn.execute(
                "INSERT INTO archive_outbox(entity,entity_id,document,recorded_at,archived_at,"
                "published_at,readable_at) VALUES(?,?,?,?,?,?,?)",
                (entity, ident, raw_document, STAMP, STAMP, STAMP, None if pending else STAMP),
            )
            seq = cursor.lastrowid
            payload = {
                "sequence": seq,
                "entity": entity,
                "entity_id": ident,
                "document": doc,
                "artifacts": {},
            }
            raw = json.dumps(payload).encode()
            relative = f".System/Receipts/{seq:012d}-{hashlib.sha256(raw).hexdigest()}.json"
            conn.execute("UPDATE archive_outbox SET receipt_path=? WHERE seq=?", (relative, seq))
            if current and entity in {"jobs", "experiment_records", "bindings"}:
                conn.execute(
                    f"INSERT OR REPLACE INTO {entity}(id,document) VALUES(?,?)",
                    (ident, raw_document),
                )
        if receipt:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        return seq

    archive = SimpleNamespace(
        core={"db": db, "now": lambda: STAMP}, _root=lambda: root, lock=threading.RLock(), status={}
    )
    return archive, add, root


def job(ident, capture, **fields):
    return {
        "job_id": ident,
        "capture_id": capture,
        "camera_id": "Camera",
        "status": "completed",
        "submitted_at": STAMP,
        "finished_at": STAMP,
        "lines": [],
        "readings": [],
        "request_trigger": "voice_photo_directory",
        "external_photo": {"captured_at": STAMP},
        **fields,
    }


def scan(ident):
    return {
        "scan_id": ident,
        "capture_id": ident,
        "camera_id": "Camera",
        "received_at": STAMP,
        "matches": [],
        "external_photo": {"captured_at": STAMP},
    }


def group(ident, jobs, **fields):
    return {
        "measurement_id": ident,
        "camera_id": "Camera",
        "status": "draft",
        "job_ids": jobs,
        "sources": [],
        "context": {"burst_id": ident, "expected_photos": len(jobs)},
        **fields,
    }
