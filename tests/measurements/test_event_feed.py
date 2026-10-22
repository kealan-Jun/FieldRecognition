import asyncio
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
    return client.get("/api/events/feed", params={"camera_id": "Camera1", **params})


def test_composed_app_has_distinct_sse_and_bounded_json_routes(app_client):
    app, client = app_client
    paths = {"/api/events", "/api/events/feed"}

    def entries(router):
        for route in router.routes:
            if hasattr(route, "original_router"):  # FastAPI lazily included routers.
                yield from entries(route.original_router)
            else:
                yield route

    routes = [route.path for route in entries(app.app)
              if getattr(route, "path", None) in paths and "GET" in route.methods]
    assert sorted(routes) == sorted(paths)
    before = read(client).json()["next_cursor"]
    for index in range(101):
        write(app, "scans", f"page-{index}", {"camera_id": "Camera1"})
    response = read(client, after_seq=before, limit=100)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    page = response.json()
    assert len(page["items"]) == 100
    assert page["has_more"] is True
    assert len(read(client, after_seq=page["next_cursor"]).json()["items"]) == 1


def test_composed_app_sse_subscribes_without_camera_query(app_client):
    app, _ = app_client

    async def subscribe():
        disconnected = asyncio.Event()
        sent = []
        initial_request = True

        async def receive():
            nonlocal initial_request
            if initial_request:
                initial_request = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                disconnected.set()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
                 "http_version": "1.1", "scheme": "http", "method": "GET",
                 "path": "/api/events", "raw_path": b"/api/events", "root_path": "",
                 "query_string": b"", "headers": [],
                 "client": ("127.0.0.1", 12345), "server": ("testserver", 80)}
        await asyncio.wait_for(app.app(scope, receive, send), timeout=5)
        return sent

    messages = asyncio.run(subscribe())
    start = next(message for message in messages if message["type"] == "http.response.start")
    assert start["status"] == 200
    headers = dict(start["headers"])
    assert headers[b"content-type"].startswith(b"text/event-stream")
    assert headers[b"cache-control"] == b"no-store"
    body = b"".join(message.get("body", b"") for message in messages)
    assert b"event: changed\ndata: {\"refresh\":true}" in body


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
