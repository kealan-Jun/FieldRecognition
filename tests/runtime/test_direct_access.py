from tests.support.direct_access import direct  # noqa: F401

"""The managed workbench must work without a login or an administrator account."""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient

from field_recognition.runtime.database import Database, apply_migrations
from field_recognition.runtime.prepare_runtime import prepare
from field_recognition.runtime.production_config import ProductionConfig
from field_recognition.runtime.task_queue import TaskQueue


def test_default_direct_access_without_admin_or_cookie(direct):
    app, client = direct
    assert app.RUNTIME_ENABLED and not app.AUTH_ENABLED
    assert not ProductionConfig.from_environment().auth_enabled
    assert not any(
        not e.startswith("Warning:")
        for e in ProductionConfig.from_environment().validate_production_requirements()
    )
    with app.db() as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0
    assert client.get("/").status_code == 200
    old_login = client.get("/login", follow_redirects=False)
    assert old_login.status_code == 303 and old_login.headers["location"] == "/"
    for path in (
        "/api/state",
        "/api/v1/state",
        "/api/tools",
        "/api/admin/status",
        "/api/photo-measurements",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
    assert client.get("/api/auth/me").json() == {
        "auth_enabled": False,
        "user_id": None,
        "csrf_token": None,
    }
    # A stale cookie left by the removed login flow must not block browser tools.
    client.cookies.set("field_session", "expired-session")
    for prefix in ("/api", "/api/v1"):
        assert client.post(prefix + "/tools/get_field_state", json={}).status_code == 200
    assert client.post("/api/auth/logout", json={}).status_code == 200
    assert app.ocr_model is None  # The API still does not own the GPU model.


def test_camera_header_routes_state_to_the_requested_camera(direct):
    app, client = direct
    with app.db() as conn:
        for camera_id in ("cam-a", "cam-b"):
            conn.execute(
                """INSERT INTO camera_registry
                            (camera_id,display_name,enabled,registered_at)
                            VALUES(?,?,1,?)""",
                (camera_id, camera_id, app.now()),
            )
    cameras = client.get("/api/cameras").json()["items"]
    assert {item["camera_id"] for item in cameras} == {"cam-a", "cam-b"}
    first = client.get("/api/state", headers={"X-Camera-Id": "cam-a"}).json()
    second = client.get("/api/state", headers={"X-Camera-Id": "cam-b"}).json()
    assert first["camera"]["id"] == "cam-a"
    assert second["camera"]["id"] == "cam-b"
    assert first["automation"]["camera_id"] == "cam-a"
    assert second["automation"]["camera_id"] == "cam-b"


def test_disabled_login_and_account_endpoints_do_not_create_accounts(direct):
    app, client = direct
    assert (
        client.post("/api/auth/login", json={"username": "a", "password": "unused"}).status_code
        == 404
    )
    assert (
        client.post(
            "/api/admin/users",
            json={
                "username": "new-user",
                "display_name": "人员",
                "role": "operator",
                "password": "unused-password",
            },
        ).status_code
        == 404
    )
    assert client.put("/api/admin/users/absent/disabled?disabled=true").status_code == 404
    assert client.post("/api/admin/cameras/cam-a/credential").status_code == 404
    with app.db() as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0


def test_direct_queue_replay_records_declared_actor(direct):
    app, client = direct
    queue = TaskQueue(app.database)
    queue.enqueue({"job_id": "failed-job", "camera_id": "cam-a"})
    with app.db() as conn:
        conn.execute("UPDATE jobs SET status='failed' WHERE id='failed-job'")
    url = "/api/admin/jobs/failed-job/replay"
    assert client.post(url, json={"reason": "恢复后重试"}).status_code == 422
    response = client.post(url, json={"actor": "登记操作人", "reason": "恢复后重试"})
    assert response.status_code == 200, response.text
    assert response.json()["replays"][-1]["actor"] == "登记操作人"


def test_prepare_registers_multiple_personnel_and_switches_active_user(tmp_path):
    first = prepare(tmp_path, "wearer-one", "甲", "cam-a")
    assert prepare(tmp_path, "wearer-one", "甲", "cam-a")["user_id"] == first["user_id"]
    assert not (tmp_path / "Access").exists()
    with Database(first["database"]).connection() as conn:
        user = conn.execute("SELECT * FROM users").fetchone()
        assert user["password_hash"] is None and user["role"] == "operator"
    second = prepare(tmp_path, "wearer-two", "乙", "cam-a")
    with Database(first["database"]).connection() as conn:
        members = {
            row["user_id"]
            for row in conn.execute(
                "SELECT user_id FROM camera_users WHERE camera_id=?", ("cam-a",)
            )
        }
        assert members == {first["user_id"], second["user_id"]}
        assert (
            conn.execute(
                "SELECT user_id FROM camera_active_users WHERE camera_id=?", ("cam-a",)
            ).fetchone()[0]
            == second["user_id"]
        )
