"""Bounded transport recovery retains ownership without inventing gap evidence."""

import json
import io

import pytest

from field_recognition.bindings.binding_policy import contains, sweep
from field_recognition.capture.live_scan import LiveScanner
from field_recognition.measurements.readout_context import at_capture
from tests.support.demo import app_client, enter_scene, scan  # noqa: F401
from tests.support.live_scan import live, reconnecting  # noqa: F401
from tests.support.instrument_ownership import own
from tests.support.archive_store import archive  # noqa: F401
from tests.support.live_scan import Camera


def saved(app, binding):
    with app.db() as conn:
        return json.loads(
            conn.execute(
                "SELECT document FROM bindings WHERE id=?", (binding["binding_id"],)
            ).fetchone()[0]
        )


@pytest.mark.parametrize(
    "failure",
    [
        "camera_stream_error",
        "camera_decode_error",
        "receiver_status_unavailable",
        "camera_not_found",
        "camera_service_offline",
        "camera_frames_stale",
    ],
)
def test_short_disconnect_retains_ids_occupancy_and_photo_history(reconnecting, failure):
    app, client, receiver, advance = reconnecting
    enter_scene(client)
    first = own(client, "InstrumentA", receiver.target, "甲")
    second = own(client, "InstrumentB", receiver.target, "甲")
    visit = client.get("/api/state").json()["scene_visits"][0]
    before = app.now()
    advance(1, online=False, failure_reason=failure)
    during = app.now()
    held = saved(app, first)
    assert held["connection_status"] == "recovering" and not held["ended_at"]
    assert not app.current_readout_binding(held)
    other_scan = scan(client, "InstrumentA", "OtherCamera")
    assert (
        client.post(
            "/api/bindings",
            json={
                "scan_id": other_scan["scan_id"],
                "instrument_id": first["instrument"]["id"],
                "operator": "乙",
            },
        ).status_code
        == 409
    )
    # A heartbeat without frames leaves the original deadline intact.
    advance(1, online=True, media_session_id=10)
    assert saved(app, first)["reconnect_deadline"] == held["reconnect_deadline"]
    assert receiver.snapshot()["service_status"]["online"] is False
    # The state is persisted, independent of the in-memory scanner instance.
    app.live_scanner = LiveScanner(vars(app))
    advance(1, fresh=True, online=True, media_session_id=10)
    for binding in (first, second):
        resumed = saved(app, binding)
        assert resumed["binding_id"] == binding["binding_id"] and not resumed["ended_at"]
        assert resumed["connection_status"] == "connected"
        assert app.current_readout_binding(resumed)
        assert contains(resumed, before)
        assert not contains(resumed, during)
        assert contains(resumed, app.now())
        assert client.get(binding["image_url"]).status_code == 200
    assert client.get("/api/state").json()["scene_visits"][0]["visit_id"] == visit["visit_id"]
    with app.db() as conn:
        for captured, expected in ((before, True), (during, False), (app.now(), True)):
            context = at_capture(
                vars(app),
                conn,
                {
                    "camera_id": receiver.target,
                    "received_at": app.now(),
                    "external_photo": {"captured_at": captured},
                },
                automatic=True,
            )
            assert bool(context["binding_ids"]) is expected
            if not expected:
                assert context["attribution_reason"] == "camera_connection_interrupted"
        assert conn.execute("SELECT count(*) FROM bindings").fetchone()[0] == 2


@pytest.mark.parametrize("stream_error_first", [True, False])
def test_ingress_change_waits_for_new_frame_then_keeps_binding(reconnecting, stream_error_first):
    app, client, receiver, advance = reconnecting
    first = own(client, "InstrumentA", receiver.target, "甲")
    if stream_error_first:
        advance(1, online=False, failure_reason="camera_stream_error")
    advance(1, fresh=True, online=True, media_session_id=5)
    assert receiver.latest is None  # Pre-change images cannot authorize recovery.
    assert receiver.info["service_status"]["online"] is False
    assert saved(app, first)["connection_status"] == "recovering"
    advance(1, fresh=True, online=True, media_session_id=5)
    assert app.current_readout_binding(first)
    assert saved(app, first)["service_interruptions"][-1]["recovered_media_session_id"] == 5


@pytest.mark.parametrize("seconds", [30, 31])
def test_deadline_releases_without_poll_and_never_resurrects(reconnecting, seconds):
    app, client, receiver, advance = reconnecting
    first = own(client, "InstrumentA", receiver.target, "甲")
    other = own(client, "InstrumentB", "OtherCamera", "乙")
    advance(1, online=False, failure_reason="camera_stream_error")
    advance(seconds)
    sweep(vars(app))
    ended = saved(app, first)
    assert ended["ended_at"] and ended["end_reason"] == "camera_reconnect_timeout"
    assert not saved(app, other)["ended_at"]
    advance(1, fresh=True, online=True, media_session_id=10)
    assert not app.current_readout_binding(first)
    assert saved(app, first)["ended_at"] == ended["ended_at"]
    # Another camera can claim the released instrument through a real QR decode.
    own(client, "InstrumentA", "OtherCamera", "乙")


def test_repeated_errors_do_not_extend_window_and_old_scans_stay_invalid(reconnecting):
    app, client, receiver, advance = reconnecting
    first = own(client, "InstrumentA", receiver.target, "甲")
    advance(1, online=False, failure_reason="camera_stream_error")
    deadline = saved(app, first)["reconnect_deadline"]
    during = scan(client)
    for _ in range(3):
        advance(5, online=False, failure_reason="receiver_status_unavailable")
        assert saved(app, first)["reconnect_deadline"] == deadline
    advance(1, fresh=True, online=True, media_session_id=10)
    for scan_id in (first["scan_id"], during["scan_id"]):
        assert (
            client.post(
                "/api/bindings",
                json={
                    "scan_id": scan_id,
                    "instrument_id": first["instrument"]["id"],
                    "operator": "甲",
                },
            ).status_code
            == 409
        )
    assert not saved(app, first)["ended_at"]


@pytest.mark.parametrize("during_grace", [True, False])
def test_explicit_boot_change_releases_immediately(reconnecting, during_grace):
    app, client, receiver, advance = reconnecting
    advance(1, fresh=True, online=True, media_session_id=10, device_boot_id="boot-a")
    first = own(client, "InstrumentA", receiver.target, "甲")
    if during_grace:
        advance(1, online=False, failure_reason="camera_stream_error")
    advance(1, fresh=True, online=True, media_session_id=10, device_boot_id="boot-b")
    assert saved(app, first)["end_reason"] == "device_boot_changed"
    assert not app.current_readout_binding(first)


def test_hard_process_failure_bypasses_grace(reconnecting):
    app, client, receiver, advance = reconnecting
    first = own(client, "InstrumentA", receiver.target, "甲")
    advance(1, online=False, failure_reason="camera_stream_error")
    advance(1)
    app.live_scanner.observe_service({"online": False, "failure_reason": "capture_service_exited"})
    assert saved(app, first)["end_reason"] == "capture_service_exited"
    advance(1, fresh=True, online=True, media_session_id=10)
    assert saved(app, first)["ended_at"]


def test_manual_end_during_grace_is_never_restored(reconnecting):
    app, client, receiver, advance = reconnecting
    first = own(client, "InstrumentA", receiver.target, "甲")
    advance(1, online=False, failure_reason="camera_stream_error")
    assert client.post(f"/api/bindings/{first['binding_id']}/end").status_code == 200
    advance(1, fresh=True, online=True, media_session_id=10)
    assert saved(app, first)["ended_at"]
    assert not app.current_readout_binding(first)


def test_midnight_still_expires_at_midnight_while_waiting(reconnecting):
    app, client, receiver, advance = reconnecting
    advance(4 * 3600 + 19 * 60 + 50)
    first = own(client, "InstrumentA", receiver.target, "甲")
    advance(1, online=False, failure_reason="camera_stream_error")
    advance(9)
    sweep(vars(app))
    ended = saved(app, first)
    assert ended["end_reason"] == "daily_qr_expired"
    assert ended["ended_at"] == "2026-10-14T16:00:00+00:00"
    advance(1, fresh=True, online=True, media_session_id=10)
    assert saved(app, first)["ended_at"]


def test_a_different_camera_identity_cannot_resume_or_inherit_binding(reconnecting, monkeypatch):
    import urllib.request

    app, client, receiver, advance = reconnecting
    first = own(client, "InstrumentA", receiver.target, "甲")
    advance(1, online=False, failure_reason="camera_stream_error")
    advance(1, fresh=True)

    class Opener:
        def open(self, *args, **kwargs):
            receiver.stop.set()
            return io.BytesIO(
                json.dumps(
                    {
                        "cameras": [
                            {
                                "camera_key": "ReplacementCamera",
                                "status_live": True,
                                "media_live": True,
                                "rgb_ingress_session_id": 10,
                            }
                        ]
                    }
                ).encode()
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    receiver._status_loop()
    assert receiver.info["service_status"]["online"] is False
    assert saved(app, first)["connection_status"] == "recovering"
    with app.db() as conn:
        context = at_capture(
            vars(app),
            conn,
            {
                "camera_id": "ReplacementCamera",
                "received_at": app.now(),
                "external_photo": {"captured_at": app.now()},
            },
            automatic=True,
        )
        assert not context["binding_ids"]
    advance(30)
    sweep(vars(app))
    assert saved(app, first)["end_reason"] == "camera_reconnect_timeout"


def test_reconnect_edges_archive_without_replacing_qr_receipts(archive, monkeypatch):
    app, client, root = archive
    camera = Camera()
    monkeypatch.setattr(app, "receiver_camera", camera)
    clock = ["2026-10-14T11:40:00+00:00"]
    monkeypatch.setattr(app, "now", lambda: clock[0])
    scanner = app.live_scanner
    scanner.observe_service({"online": True, "media_session_id": 10})
    first = own(client, "InstrumentA", camera.target, "甲")
    app.archive_store.step(100)
    path = next(
        p
        for p in root.glob("*/Bindings/*/Binding.json")
        if json.loads(p.read_text())["binding"].get("binding_id") == first["binding_id"]
    )
    original = json.loads(path.read_text())
    clock[0] = "2026-10-14T11:40:01+00:00"
    scanner.observe_service(
        {"online": False, "reconnectable": True, "failure_reason": "camera_stream_error"}
    )
    app.archive_store.step(100)
    waiting = json.loads(path.read_text())
    assert waiting["binding"]["connection_status"] == "recovering"
    assert waiting["status"] == "active"
    clock[0] = "2026-10-14T11:40:03+00:00"
    scanner.observe_service(
        {"online": True, "reconnectable": True, "frame_verified": True, "media_session_id": 5}
    )
    app.archive_store.step(100)
    recovered = json.loads(path.read_text())
    assert recovered["binding"]["scan_id"] == original["binding"]["scan_id"]
    assert recovered["binding"]["connection_status"] == "connected"
    assert recovered["binding"]["service_interruptions"][0]["ended_at"] == clock[0]
    assert (
        len(recovered["receipt_versions"])
        > len(waiting["receipt_versions"])
        > len(original["receipt_versions"])
    )
