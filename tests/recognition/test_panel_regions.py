from tests.support.configuration import refresh_runtime_config
from tests.support.panel_regions import A, B, BOXES
from tests.support.panel_regions import configure  # noqa: F401
import copy
import hashlib
import json

import numpy as np
import pytest

import field_recognition.recognition.panel_readout as panel_readout
import field_recognition.recognition.panel_regions as panel_regions
from tests.support.demo import app_client, wait_for_job  # noqa: F401
from tests.support.multi_readout import two_bindings
from tests.support.unbound_readout import photo
from tests.support.panel_readout import Clock, cloud_result, refresh_recognition_config
from tests.support.archive_store import archive  # noqa: F401
from field_recognition.archive.archive_paths import resolve_file


def test_off_display_is_a_state_not_zero_or_a_fallback_miss(app_client, monkeypatch):
    app, _ = app_client
    boxes = [dict(class_id=0, instrument_id=A, confidence=0.8, xyxy=[20, 30, 80, 50])]
    configure(app, monkeypatch, boxes)
    monkeypatch.setattr(
        panel_regions,
        "refine_digits",
        lambda predict, image, initial, *args, **kwargs: dict(
            initial, lines=[], panel_ocr=copy.deepcopy(initial)
        ),
    )
    monkeypatch.setattr(
        app,
        "predict_panel",
        lambda *args: {"status": "completed", "lines": [{"text": "OFF", "confidence": 0.9}]},
    )
    result = panel_regions.predict(
        vars(app), np.zeros((100, 150, 3), np.uint8), allowed_instrument_ids=[A]
    )
    assert result["lines"] == []
    assert result["panel_regions"][0]["display_state"]["text"] == "OFF"
    assert panel_regions.needs_fallback(result) is None


def test_small_detector_border_overlap_keeps_temperature_and_speed_roles():
    from field_recognition.recognition.panel_layout import roles_for_boxes

    boxes = [
        dict(class_id=0, instrument_id=A, xyxy=box)
        for box in ([354, 315, 398, 347], [395, 313, 438, 342])
    ]
    assert list(roles_for_boxes(boxes).values()) == ["温度", "转速"]
    boxes[1]["xyxy"] = [360, 313, 401, 342]
    assert roles_for_boxes(boxes) == {}  # Overlapping instances cannot be guessed apart.


def orientation_pair(degrees=(180, 180), statuses=("consistent", "consistent")):
    return [
        {
            "class_id": 0,
            "instrument_id": A,
            "panel_id": f"panel-0-{i}",
            "measurement_name": name,
            "local_ocr": {
                "red_display_orientation": {"status": status, "orientation_degrees": degree},
                "red_display_geometry": {
                    "roll_degrees": 0,
                    "native_size": [40, 20],
                    "polygon": [[x, 10], [x + 40, 10], [x + 40, 30], [x, 30]],
                },
            },
        }
        for i, (x, name, degree, status) in enumerate(
            zip((10, 70), ("温度", "转速"), degrees, statuses)
        )
    ]


def test_physical_pair_roles_reverse_only_with_both_native_off_direction_evidences():
    pair = orientation_pair()
    proof = panel_regions._orient_pair_roles(pair)
    assert [region["measurement_name"] for region in pair] == ["转速", "温度"]
    assert proof[0]["orientation_degrees"] == 180 and proof[0]["status"] == "consistent"
    assert proof[0]["supporting_views"] == ["panel-0-0", "panel-0-1"]
    assert all(region["source_orientation"] == proof[0] for region in pair)


@pytest.mark.parametrize(
    "degrees,statuses",
    [
        ((180, None), ("consistent", "unconfirmed")),
        ((180, 0), ("consistent", "consistent")),
        ((180, 180), ("ocr_only", "ocr_only")),
    ],
)
def test_one_panel_or_ocr_only_rotation_does_not_guess_pair_roles(degrees, statuses):
    pair = orientation_pair(degrees, statuses)
    proof = panel_regions._orient_pair_roles(pair)
    assert [region["measurement_name"] for region in pair] == [None, None]
    assert proof[0]["status"] == "unconfirmed"


def test_off_direction_from_separate_rows_cannot_establish_one_physical_pair():
    pair = orientation_pair()
    pair[1]["local_ocr"]["red_display_geometry"]["polygon"] = [
        [70, 60],
        [110, 60],
        [110, 80],
        [70, 80],
    ]
    proof = panel_regions._orient_pair_roles(pair)
    assert proof[0]["status"] == "unconfirmed"
    assert all(region["measurement_name"] is None for region in pair)


def test_upright_or_unconfirmed_geometry_preserves_existing_pair_roles():
    for degrees, statuses in (
        ((0, 0), ("consistent", "consistent")),
        ((None, None), ("unconfirmed", "unconfirmed")),
    ):
        pair = orientation_pair(degrees, statuses)
        assert panel_regions._orient_pair_roles(pair) == []
        assert [region["measurement_name"] for region in pair] == ["温度", "转速"]


def test_two_panels_keep_separate_bindings_polygons_and_archived_crops(archive, monkeypatch):
    app, client, root = archive
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "camera_id")
    bindings = two_bindings(app, client)
    configure(app, monkeypatch)
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app)), trigger="voice_photo_directory"
    )
    done = wait_for_job(client, job["job_id"])
    assert done["status"] == "completed" and done["association_status"] == "localized_panels"
    assert [r["instrument"]["id"] for r in done["readings"]] == [A, B]
    assert [r["binding_id"] for r in done["readings"]] == [b["binding_id"] for b in bindings]
    assert done["readings"][0]["polygon"][0] == [18, 28]
    assert done["readings"][1]["polygon"][0] == [207, 97]
    assert all(r["association_basis"] == "panel_detector_session_binding" for r in done["readings"])
    assert done["instrument"] is None and len(done["binding_ids"]) == 2
    for r in done["readings"]:
        raw = client.get(r["panel_image_url"]).content
        assert hashlib.sha256(raw).hexdigest() == r["panel_image_sha256"]
    app.archive_store.step()
    documents = [json.loads(p.read_text()) for p in root.glob(".System/Receipts/**/*.json")]
    receipt = next(
        r for r in documents if r["entity_id"] == job["job_id"] and r["document"].get("readings")
    )
    crops = [a for k, a in receipt["artifacts"].items() if k.startswith("panel_")]
    assert len(crops) == 2
    assert all(
        hashlib.sha256(resolve_file(root, c["path"]).read_bytes()).hexdigest() == c["sha256"]
        for c in crops
    )
    for iid, text in ((A, "12.3 g"), (B, "200 g")):
        records = list(root.glob("*/VoicePhotoReadings/*/Evidence.json"))
        assert len(records) == 1
        r = json.loads(records[0].read_text())
        assert [
            v["text"] for v in r["observations"][0]["readings"] if v["instrument"]["id"] == iid
        ] == [text]
        entry = next(v for v in r["measurement_files"] if v["instrument_id"] == iid)
        standard = json.loads((root / entry["result"]).read_text())
        assert set(standard) == {
            "wearer_id",
            "device_model",
            "device_no",
            "qr_hash",
            "photo_time",
            "values",
        }
        assert standard["qr_hash"] == next(
            b["qr_hash"] for b in bindings if b["instrument"]["id"] == iid
        )
    exported = client.get("/api/jobs/" + job["job_id"] + "/measurements").json()
    assert exported["format_available"] and len(exported["records"]) == 2
    assert "面板分别定位" in client.get("/api/readouts").json()["items"][0]["target"]
    related = client.get("/api/readouts?related_only=true").json()["items"][0]
    assert related["job_id"] == done["job_id"]
    assert [r["instrument"]["id"] for r in related["readings"]] == [A, B]


def test_unlocalized_ocr_does_not_borrow_the_only_bound_instrument(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "camera_id")
    bindings = two_bindings(app, client)
    client.post("/api/bindings/" + bindings[1]["binding_id"] + "/end")
    configure(app, monkeypatch, [])
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app)), trigger="voice_photo_directory"
    )
    done = wait_for_job(client, job["job_id"])
    assert done["readings"] == [] and done["recognition_skipped"]
    assert done["skip_reason"] == "no_visible_bound_panel"
    assert not done["actual_model_invocation"]
    assert done["instrument"] is done["binding_id"] is None


def test_unbound_photo_reads_text_without_inventing_instrument(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "camera_id")
    configure(app, monkeypatch)
    monkeypatch.setattr(
        app.panel_detector, "predict", lambda *a: pytest.fail("No identity from detector")
    )
    calls = []

    def predict(*args):
        calls.append(args)
        return {
            "status": "completed",
            "lines": [{"text": "12.34", "confidence": 0.9}],
            "actual_model_invocation": True,
            "device": "cpu",
        }

    monkeypatch.setattr(app, "predict_panel", predict)
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app)), trigger="voice_photo_directory"
    )
    done = wait_for_job(client, job["job_id"])
    assert calls and done["actual_model_invocation"]
    assert done["lines"][0]["text"] == "12.34"
    assert not done.get("recognition_skipped")
    assert done["instrument"] is done["binding_id"] is None
    assert done["panel_regions"] == []
    assert all(r.get("instrument") is None for r in done["readings"])
    assert not client.get("/api/state").json()["bindings"]


def test_unbound_video_still_skips_inference(app_client, monkeypatch):
    app, _ = app_client
    configure(app, monkeypatch)
    monkeypatch.setattr(app.panel_detector, "predict", lambda *a: pytest.fail("No video binding"))
    monkeypatch.setattr(app, "predict_panel", lambda *a: pytest.fail("No video binding"))
    result = panel_regions.predict(vars(app), np.zeros((100, 150, 3), np.uint8), video=True)
    assert result["recognition_skipped"]
    assert result["skip_reason"] == "no_active_instrument_binding"


def test_missing_a_uses_its_crop_for_fallback_even_when_b_has_digits(app_client, monkeypatch):
    import field_recognition.recognition.aliyun_vision as vision

    app, client = app_client
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "camera_id")
    bindings = two_bindings(app, client)
    configure(app, monkeypatch)
    monkeypatch.setattr(app.readout_pool, "submit", lambda *a: None)
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app)), trigger="voice_photo_directory"
    )
    local = app.predict_readout(np.zeros((300, 400, 3), np.uint8), binding_snapshots=bindings)
    local["panel_regions"][0]["local_ocr"]["lines"] = []
    local["lines"] = local["panel_regions"][1]["local_ocr"]["lines"]
    job["precomputed_local"] = local
    monkeypatch.setenv("FIELD_ALIYUN_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.setenv("FIELD_ALIYUN_NO_DIGITS_SECONDS", "5")
    refresh_recognition_config(app)
    clock = Clock()
    calls = []

    def cloud(crop, *, config):
        assert clock.value >= 5
        calls.append(crop.shape[:2])
        return cloud_result()

    monkeypatch.setattr(vision, "read_panel", cloud)
    panel_readout.run(vars(app), job, clock=clock, pause=clock.pause)
    done = app.get_job(job["job_id"])
    assert calls == [(24, 64)]
    assert [r["value"] for r in done["readings"]] == ["12.34", "200"]
    assert [r["instrument"]["id"] for r in done["readings"]] == [A, B]


def test_managed_fallback_does_not_complete_or_release_lease_before_result(app_client, monkeypatch):
    import field_recognition.recognition.aliyun_vision as vision
    from field_recognition.runtime.task_queue import TaskQueue

    app, client = app_client
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "camera_id")
    bindings = two_bindings(app, client)
    configure(app, monkeypatch, BOXES[1:])
    monkeypatch.setattr(app.readout_pool, "submit", lambda *a: None)
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app)), trigger="voice_photo_directory"
    )
    local = app.predict_readout(np.zeros((300, 400, 3), np.uint8), binding_snapshots=bindings)
    local["panel_regions"][0]["local_ocr"]["lines"] = []
    local["lines"] = []
    from field_recognition.runtime.database import apply_migrations

    apply_migrations(app.database)
    queue = TaskQueue(app.database, "regression")
    task = queue.claim_task()
    task["precomputed_local"] = local
    monkeypatch.setenv("FIELD_ALIYUN_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    refresh_recognition_config(app)

    def cloud(crop, *, config):
        with app.db() as c:
            row = c.execute(
                "select status,lease_holder from jobs where id=?", (job["job_id"],)
            ).fetchone()
            assert row["status"] == "running" and row["lease_holder"] == task["lease_holder"]
        assert queue.renew_lease(task["job_id"], task["lease_holder"])
        return cloud_result()

    monkeypatch.setattr(vision, "read_panel", cloud)
    clock = Clock()
    panel_readout.run(dict(vars(app), task_queue=queue), task, clock=clock, pause=clock.pause)
    done = app.get_job(job["job_id"])
    assert done["status"] == done["phase"] == "completed" and done["finished_at"]
    assert done["readings"][0]["value"] == "12.34"
    assert done["readings"][0]["instrument"]["id"] == B
    assert done["fallback"]["status"] == "completed"
    with app.db() as c:
        assert (
            c.execute("select lease_holder from jobs where id=?", (job["job_id"],)).fetchone()[0]
            is None
        )


def test_no_panel_in_video_skips_ocr_and_does_not_use_background_numbers(app_client, monkeypatch):
    app, _ = app_client
    configure(app, monkeypatch, [])
    monkeypatch.setattr(
        app, "predict_panel", lambda *a: pytest.fail("Background video must not invoke OCR")
    )
    local = app.predict_readout(np.zeros((300, 400, 3), np.uint8), video=True)
    assert local["lines"] == local["panel_regions"] == []
    assert not local["actual_model_invocation"]


def test_crop_offsets_are_in_full_photo_coordinates(app_client, monkeypatch):
    app, _ = app_client
    configure(app, monkeypatch, BOXES[:1])
    local = app.predict_readout(
        np.zeros((150, 150, 3), np.uint8), 100, 200, binding_snapshots=[{"instrument": {"id": A}}]
    )
    region = local["panel_regions"][0]
    assert region["bbox"] == [120, 230, 180, 250]
    assert region["crop"] == [118, 228, 64, 24]
    assert local["lines"][0]["polygon"][0] == [118, 228]


def test_only_b_bound_skips_a_panels_before_ocr(app_client, monkeypatch):
    app, _ = app_client
    configure(app, monkeypatch)
    calls = []
    monkeypatch.setattr(
        app, "predict_panel", lambda image, x=0, y=0: calls.append((x, y)) or {"lines": []}
    )
    local = app.predict_readout(
        np.zeros((300, 400, 3), np.uint8), binding_snapshots=[{"instrument": {"id": B}}]
    )
    assert calls == [(207, 97)]
    assert [r["instrument_id"] for r in local["panel_regions"]] == [B]
    assert [r["instrument_id"] for r in local["panel_detection"]["skipped_unbound_panels"]] == [A]


def test_localization_timeout_never_sends_whole_image_to_cloud(app_client, monkeypatch):
    from concurrent.futures import Future
    import field_recognition.recognition.aliyun_vision as vision

    app, client = app_client
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "camera_id")
    two_bindings(app, client)
    configure(app, monkeypatch)
    monkeypatch.setattr(app.readout_pool, "submit", lambda *a: None)
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app)), trigger="voice_photo_directory"
    )
    future = Future()
    monkeypatch.setattr(app.ocr_pool, "submit", lambda *a: future)
    monkeypatch.setenv("FIELD_ALIYUN_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.setattr(vision, "read_panel", lambda *a: pytest.fail("Unlocalized cloud call"))
    clock = Clock()
    panel_readout.run(vars(app), job, clock=clock, pause=clock.pause)
    done = app.get_job(job["job_id"])
    assert done["error"] == "bound_panel_localization_unavailable"
    assert done["readings"] == [] and done["instrument"] is None


def test_stirrer_values_use_aligned_left_right_windows_before_partial_frame_selection(
    app_client, monkeypatch
):
    app, _ = app_client
    boxes = [BOXES[0], dict(BOXES[0], xyxy=[90, 30, 150, 50]), BOXES[1]]
    configure(app, monkeypatch, boxes)
    local = app.predict_readout(
        np.zeros((300, 400, 3), np.uint8),
        binding_snapshots=[{"instrument": {"id": A}}, {"instrument": {"id": B}}],
    )
    assert [r["measurement_name"] for r in local["panel_regions"]] == ["温度", "转速", "质量"]
    # Only one changed window can subsequently be retained, with its role intact.
    assert local["panel_regions"][1]["measurement_name"] == "转速"
    configure(app, monkeypatch, boxes[1:])
    local = app.predict_readout(
        np.zeros((300, 400, 3), np.uint8), binding_snapshots=[{"instrument": {"id": A}}]
    )
    assert local["panel_regions"][0]["measurement_name"] is None
