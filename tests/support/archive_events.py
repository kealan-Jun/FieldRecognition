"""Reusable archive_events fixtures and doubles."""

import json

from field_recognition.archive.archive_paths import ArchivePaths
from tests.support.archive_store import archive  # noqa: F401
from tests.support.demo import app_client, register, scan  # noqa: F401
from tests.support.photo_measurements import complete, enqueue, get_draft, setup  # noqa: F401


def publish_document(root, document):
    from field_recognition.archive.archive_events import make_views, prepare

    paths = ArchivePaths(root)
    rows = [
        {
            "entity": "jobs",
            "entity_id": document["job_id"],
            "seq": 1,
            "document": json.dumps(document),
            "recorded_at": "2026-09-14T05:43:10Z",
            "receipt_path": ".System/Receipts/Original.json",
        }
    ]
    bundles, _ = prepare(rows, paths)
    views = make_views(bundles, paths, {1: {"artifacts": {}}})
    paths.retire_views(views)
    return next(root.glob("*/VoicePhotoReadings/*/Evidence.json"))
