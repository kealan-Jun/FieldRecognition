"""Reusable archive_store fixtures and doubles."""

import json

import pytest

from tests.support.demo import app_client, register, scan  # noqa: F401
from tests.support.configuration import refresh_runtime_config


@pytest.fixture
def archive(app_client, monkeypatch, tmp_path):
    app, client = app_client
    root = tmp_path / "NasMount" / "FieldRecognitionArchive"
    monkeypatch.setenv("FIELD_ARCHIVE_ENABLED", "1")
    monkeypatch.setenv("FIELD_ARCHIVE_ROOT", str(root))
    monkeypatch.delenv("FIELD_ARCHIVE_MOUNT", raising=False)
    refresh_runtime_config(app, "archive_enabled", "archive_root", "archive_mount")
    return app, client, root


def receipts(root):
    return [json.loads(p.read_text()) for p in root.glob(".System/Receipts/**/*.json")]
