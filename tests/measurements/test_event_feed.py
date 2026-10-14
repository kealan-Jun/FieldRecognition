import json

import pytest

from tests.support.demo import app_client  # noqa: F401


def write(app, entity, ident, document, status=None):
    with app.db() as conn:
        if entity == "jobs":
            conn.execute("INSERT INTO jobs(id,status,document) VALUES(?,?,?)",
                         (ident, status or document["status"], json.dumps(document)))
        elif entity == "bindings":
            conn.execute("INSERT INTO bindings VALUES(?,?,?,?)",
                         (ident, document["camera_id"], document.get("ended_at"), json.dumps(document)))
        else:
            conn.execute("INSERT INTO scans VALUES(?,?)", (ident, json.dumps(document)))


def read(client, **params):
    return client.get("/api/events", params={"camera_id": "Camera1", **params})


def test_feed_is_installed_and_delivers_transactional_versions_without_archive(app_client):
    app, client = app_client
    initial = read(client).json()
    doc = {"job_id": "ocr-1", "capture_id": "photo-1", "camera_id": "Camera1",
           "status": "queued", "external_photo": {"captured_at": "2026-10-14T09:00:00+08:00"},
           "image_sha256": "a" * 64, "lines": [], "wearer_id": None}
    write(app, "jobs", "ocr-1", doc)
    with app.db() as conn:
        conn.execute("UPDATE jobs SET status='completed',document=? WHERE id='ocr-1'",
                     (json.dumps(doc | {"status": "completed", "lines": [{"text": "12.3 mg"}]}),))
    result = read(client, after_seq=initial["next_cursor"]).json()
    assert result["schema"] == "field-event-feed/1"
    assert len(result["items"]) == 2
    first, second = result["items"]
    assert first["event_id"] == second["event_id"]
    assert first["revision"] < second["revision"]
    assert second["occurred_start"] == "2026-10-14T01:00:00+00:00"
    assert second["payload"]["raw_lines"] == [{"text": "12.3 mg"}]
    assert second["operator_id"] is None and second["wearer_id"] is None
    assert second["associations"]["wearer"]["state"] == "unknown"
    assert read(client, after_seq=initial["next_cursor"]).json() == result
    assert read(client, after_seq=result["next_cursor"]).json()["items"] == []


def test_rollback_does_not_publish_and_camera_filter_cannot_mix_identities(app_client):
    app, client = app_client
    before = read(client).json()["next_cursor"]
    with app.db() as conn:
        conn.execute("INSERT INTO scans VALUES(?,?)", ("rolled-back", json.dumps({"camera_id": "Camera1"})))
        conn.rollback()
    write(app, "scans", "other", {"camera_id": "Camera2", "capture_id": "same-blob",
                                  "source_sha256": "a" * 64})
    write(app, "scans", "own", {"camera_id": "Camera1", "capture_id": "another-instance",
                                "source_sha256": "a" * 64})
    page = read(client, after_seq=before, limit=1).json()
    assert [event["capture_device_id"] for event in page["items"]] == ["Camera1"]
    assert page["items"][0]["capture_id"] == "another-instance"
    assert page["items"][0]["occurred_start"] is None
    assert page["items"][0]["clock"]["basis"] is None


def test_binding_valid_time_and_source_epoch_mismatch_are_explicit(app_client):
    app, client = app_client
    write(app, "bindings", "binding-1", {"camera_id": "Camera1", "binding_id": "binding-1",
          "started_at": "2026-10-14T09:00:00+08:00", "ended_at": "2026-10-14T09:10:00+08:00",
          "wearer_id": "person-a", "operator": "declared-person", "instrument": {"id": "instrument-1"},
          "scan_id": "decoded-qr-evidence"})
    event = read(client).json()["items"][-1]
    assert event["payload"]["valid_interval"] == "[valid_from,valid_to)"
    assert event["associations"]["wearer"]["state"] == "explicit"
    assert event["operator_id"] is None  # A display name is not a stable operator identity.
    assert read(client, source_instance="wrong-instance").status_code == 409


@pytest.mark.parametrize("params", [{"after_seq": -1}, {"limit": 101}, {"limit": 0}])
def test_invalid_feed_page_rejected(app_client, params):
    assert read(app_client[1], **params).status_code == 422
