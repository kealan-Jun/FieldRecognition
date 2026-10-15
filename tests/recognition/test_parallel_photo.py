import json
from threading import Event

import pytest

from field_recognition.recognition import aliyun_vision
from field_recognition.recognition import panel_readout
from field_recognition.runtime.task_queue import TaskQueue, LeaseLost
from field_recognition.runtime.database import apply_migrations
from tests.support.parallel_photo import parallel_context, enqueue, running, observe  # noqa: F401
from tests.support.demo import app_client  # noqa: F401
from tests.support.panel_readout import local_result, cloud_result
from tests.support.multi_readout import two_bindings
from tests.support.panel_regions import configure, A, B
from tests.support.archive_store import archive  # noqa: F401


def test_local_is_published_and_archived_while_cloud_is_still_reading(
    parallel_context, archive, monkeypatch
):
    app, client = parallel_context
    root = archive[2]
    local_started, cloud_started, release = Event(), Event(), Event()

    def local(*args, **kwargs):
        local_started.set()
        assert cloud_started.wait(3), "local OCR was allowed to finish before cloud even started"
        return local_result("12.34 g")

    def cloud(*args, **kwargs):
        cloud_started.set()
        assert local_started.wait(3)
        assert release.wait(4)
        result = cloud_result()
        result["answer"]["readings"][0].update(text="12.35 g", value="12.35")
        return result

    monkeypatch.setattr(app, "predict_panel", local)
    monkeypatch.setattr(aliyun_vision, "read_panel", cloud)
    job = enqueue(parallel_context)
    with running(app, job, release) as future:
        first = observe(client, job["job_id"], lambda r: r.get("result_available"))
        assert not future.done() and first["status"] == "running"
        assert first["result_stage"] == "preliminary"
        assert first["readings"][0]["value"] == "12.34"
        app.archive_store.step(100)
        file = next(root.glob("*/VoicePhotoReadings/*/Readings.json"))
        initial = json.loads(file.read_text())
        assert len(initial["readings"]) == 1
        assert initial["readings"][0]["value"] == 12.34
        assert initial["readings"][0]["result_stage"] == "preliminary"
        evidence = json.loads((file.parent / "Evidence.json").read_text())
        assert not evidence["processing_terminal"]
        assert "本地初读" in (file.parent / "Readme.html").read_text()
        release.set()
        future.result(timeout=4)
    final = app.get_job(job["job_id"])
    assert final["status"] == "completed"
    assert final["result_stage"] == "reviewed" and final["result_revision"] == 2
    assert final["readings"][0]["value"] == "12.35"
    assert final["local_ocr"]["lines"][0]["text"] == "12.34 g"
    assert final["first_result_at"] == first["first_result_at"]
    assert final["timing"]["durations_ms"]["run_to_first_result_ms"] is not None
    assert final["readings"][0]["reading_id"] == first["readings"][0]["reading_id"]
    app.archive_store.step(100)
    assert len(list(root.glob("*/VoicePhotoReadings/*/Readings.json"))) == 1
    assert json.loads(file.read_text())["readings"][0]["value"] == 12.35
    with app.db() as conn:
        snapshots = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT document FROM archive_outbox WHERE entity='jobs' AND entity_id=?",
                (job["job_id"],),
            )
        ]
    assert any(r.get("result_stage") == "preliminary" for r in snapshots)
    assert any(r.get("result_stage") == "reviewed" for r in snapshots)


def test_cloud_can_finish_first_and_late_local_only_appends_raw_evidence(
    parallel_context, monkeypatch
):
    app, client = parallel_context
    started, release = Event(), Event()

    def local(*args, **kwargs):
        started.set()
        assert release.wait(4)
        return local_result("99.99 g")

    def cloud(*args, **kwargs):
        assert started.wait(3)
        return cloud_result()

    monkeypatch.setattr(app, "predict_panel", local)
    monkeypatch.setattr(aliyun_vision, "read_panel", cloud)
    job = enqueue(parallel_context)
    with running(app, job, release) as future:
        future.result(timeout=3)
        result = app.get_job(job["job_id"])
        assert result["status"] == "completed" and result["readings"][0]["value"] == "12.34"
        assert result["local_ocr"]["status"] == "running"
        release.set()
        late = observe(client, job["job_id"], lambda r: r.get("local_ocr_finished_late"))
        assert late["local_ocr"]["lines"][0]["text"] == "99.99 g"
        assert late["readings"][0]["value"] == "12.34" and late["result_revision"] == 1


@pytest.mark.parametrize("cloud_first", [False, True])
def test_cloud_failure_keeps_local_answer_and_releases_queue_lease(
    parallel_context, monkeypatch, cloud_first
):
    app, client = parallel_context
    release, started = Event(), Event()

    def local(*args, **kwargs):
        if cloud_first:
            assert release.wait(4)
        started.set()
        return local_result("12.34 g")

    def cloud(*args, **kwargs):
        if not cloud_first:
            assert started.wait(3)
            assert release.wait(4)
        return {"status": "failed", "error": "timeout", "attempted": True}

    monkeypatch.setattr(app, "predict_panel", local)
    monkeypatch.setattr(aliyun_vision, "read_panel", cloud)
    apply_migrations(app.database)
    queued = enqueue(parallel_context)
    queue = TaskQueue(app.database, "test-parallel-owner")
    monkeypatch.setattr(app, "task_queue", queue)
    job = queue.claim_task("ocr")
    assert job["job_id"] == queued["job_id"]
    with running(app, job, release) as future:
        first = observe(
            client,
            job["job_id"],
            lambda r: (
                r.get("parallel_review", {}).get("status") == "failed"
                if cloud_first
                else r.get("result_available")
            ),
        )
        with app.db() as conn:
            assert conn.execute(
                "SELECT lease_holder FROM jobs WHERE id=?", (job["job_id"],)
            ).fetchone()[0]
        release.set()
        future.result(timeout=4)
    final = app.get_job(job["job_id"])
    assert final["readings"][0]["value"] == "12.34"
    assert final["parallel_review"]["status"] == "failed"
    assert final["result_stage"] == "preliminary" and final["status"] == "completed"
    with app.db() as conn:
        assert (
            conn.execute("SELECT lease_holder FROM jobs WHERE id=?", (job["job_id"],)).fetchone()[0]
            is None
        )


@pytest.mark.parametrize("answer", ["valid", "invalid_unit", "unreadable"])
def test_bound_readers_share_one_localization_and_update_same_result_files(
    parallel_context, archive, monkeypatch, answer
):
    app, client = parallel_context
    root = archive[2]
    two_bindings(app, client)
    configure(
        app,
        monkeypatch,
        [
            dict(class_id=0, instrument_id=A, confidence=0.8, xyxy=[20, 30, 80, 50]),
            dict(class_id=0, instrument_id=A, confidence=0.8, xyxy=[110, 30, 180, 50]),
            dict(class_id=1, instrument_id=B, confidence=0.9, xyxy=[210, 100, 300, 140]),
        ],
    )
    calls, detection_count = [], []
    detector = app.panel_detector.predict

    def detect(*args, **kwargs):
        detection_count.append(True)
        return detector(*args, **kwargs)

    release, cloud_started = Event(), Event()

    def local(pixels, x=0, y=0):
        assert cloud_started.wait(3)
        return local_result("12.3 °C" if x < 100 else "1200 rpm" if x < 200 else "200 g")

    def cloud(pixels, **kwargs):
        cloud_started.set()
        assert release.wait(4)
        calls.append(pixels.shape[:2])
        result = cloud_result()
        mass = pixels.shape[1] > 80
        speed = 70 < pixels.shape[1] <= 80
        result["answer"]["readings"] = (
            []
            if answer == "unreadable"
            else [
                {
                    "text": "201 g" if mass else "1201 rpm" if speed else "12.4 °C",
                    "value": "201" if mass else "1201" if speed else "12.4",
                    "unit": ("g" if speed else "rpm")
                    if answer == "invalid_unit"
                    else "g"
                    if mass
                    else "rpm"
                    if speed
                    else "°C",
                    "label": "质量" if mass else "转速" if speed else "温度",
                }
            ]
        )
        return result

    monkeypatch.setattr(app.panel_detector, "predict", detect)
    monkeypatch.setattr(app, "predict_panel", local)
    monkeypatch.setattr(aliyun_vision, "read_panel", cloud)
    job = enqueue(parallel_context)
    with running(app, job, release) as future:
        first = observe(client, job["job_id"], lambda r: r.get("result_available"))
        assert first["display_fields"][0]["value"] == 12.3
        app.archive_store.step(100)
        result_paths = sorted(root.glob("*/VoicePhotoReadings/*/Result*.json"))
        assert len(result_paths) == 2
        old = {p: json.loads(p.read_text()) for p in result_paths}
        assert any(r["values"][0]["value"] == 200 for r in old.values())
        release.set()
        future.result(timeout=4)
    final = app.get_job(job["job_id"])
    assert len(detection_count) == 1 and len(calls) == 3
    assert final["job_id"] == first["job_id"] and final["capture_id"] == first["capture_id"]
    assert final["all_binding_snapshots"] == first["all_binding_snapshots"]
    assert len(final["readings"]) == (0 if answer == "unreadable" else 3)
    if answer == "valid":
        assert {r["value"] for r in final["readings"]} == {"12.4", "1201", "201"}
    elif answer == "invalid_unit":
        assert final["parallel_review"]["status"] == "conflict"
        assert {r["value"] for r in final["readings"]} == {"12.3", "1200", "200"}
    else:
        assert final["parallel_review"]["status"] == "unreadable"
        assert all(f["value"] is None for f in final["display_fields"])
    app.archive_store.step(100)
    assert sorted(root.glob("*/VoicePhotoReadings/*/Result*.json")) == result_paths
    for p in result_paths:
        current = json.loads(p.read_text())
        assert current["photo_time"] == old[p]["photo_time"]
        assert current["qr_hash"] == old[p]["qr_hash"]
    assert len(final["local_ocr"]["panel_regions"]) == 3


def test_a_superseded_lease_cannot_publish_the_cloud_response(parallel_context, monkeypatch):
    app, client = parallel_context
    release = Event()
    monkeypatch.setattr(app, "predict_panel", lambda *a, **k: local_result("12.34 g"))

    def cloud(*args, **kwargs):
        assert release.wait(4)
        return cloud_result()

    monkeypatch.setattr(aliyun_vision, "read_panel", cloud)
    apply_migrations(app.database)
    queued = enqueue(parallel_context)
    queue = TaskQueue(app.database, "old-owner")
    monkeypatch.setattr(app, "task_queue", queue)
    job = queue.claim_task("ocr")
    with pytest.raises(LeaseLost):
        with running(app, job, release) as future:
            first = observe(client, job["job_id"], lambda r: r.get("result_available"))
            newer = dict(first, lease_holder="replacement-owner", replacement_marker=True)
            with app.db() as conn:
                conn.execute(
                    "UPDATE jobs SET lease_holder=?,document=? WHERE id=?",
                    ("replacement-owner", json.dumps(newer), job["job_id"]),
                )
            release.set()
            future.result(timeout=4)
    final = app.get_job(job["job_id"])
    assert final["replacement_marker"] and final["parallel_review"]["status"] == "running"
    assert final["result_revision"] == 1


def test_local_failure_and_cloud_failure_are_not_reported_as_success(parallel_context, monkeypatch):
    app, _ = parallel_context
    monkeypatch.setattr(
        app,
        "predict_panel",
        lambda *a, **k: {
            "status": "failed",
            "lines": [],
            "error": "model_failed",
        },
    )
    monkeypatch.setattr(
        aliyun_vision,
        "read_panel",
        lambda *a, **k: {
            "status": "failed",
            "error": "timeout",
        },
    )
    job = enqueue(parallel_context)
    with running(app, job) as future:
        future.result(timeout=4)
    final = app.get_job(job["job_id"])
    assert final["status"] == "failed" and not final["result_available"]


def test_video_keeps_its_existing_local_first_fallback_policy(parallel_context, monkeypatch):
    app, _ = parallel_context
    monkeypatch.setattr(app, "predict_panel", lambda *a, **k: local_result("12.34 g"))
    monkeypatch.setattr(app, "current_readout_binding", lambda _: True)
    monkeypatch.setattr(
        app.vision_pool, "submit", lambda *a, **k: pytest.fail("Video used photo review")
    )
    job = enqueue(parallel_context)
    job["request_trigger"] = "video_stream"
    panel_readout.run(vars(app), job)
    final = app.get_job(job["job_id"])
    assert final["status"] == "completed" and final["reading_mode"] == "one_requested_photo"
    assert "parallel_review" not in final


@pytest.mark.parametrize(
    "initial,reviewed,reason",
    [
        ("1500", "15.0", "decimal_scale_conflict"),
        ("1.234", "12.34", "decimal_scale_conflict"),
        ("12.34", "-12.34", "sign_conflict"),
    ],
)
def test_numeric_scale_and_sign_disagreements_keep_initial_and_record_both_answers(
    parallel_context, monkeypatch, initial, reviewed, reason
):
    app, client = parallel_context
    release = Event()
    monkeypatch.setattr(app, "predict_panel", lambda *a, **k: local_result(initial + " g"))

    def cloud(*args, **kwargs):
        assert release.wait(4)
        result = cloud_result()
        result["answer"]["readings"][0].update(text=reviewed + " g", value=reviewed)
        return result

    monkeypatch.setattr(aliyun_vision, "read_panel", cloud)
    job = enqueue(parallel_context)
    with running(app, job, release) as future:
        first = observe(client, job["job_id"], lambda r: r.get("result_available"))
        release.set()
        future.result(timeout=4)
    final = app.get_job(job["job_id"])
    assert final["readings"][0]["value"] == first["readings"][0]["value"] == initial
    assert final["parallel_review"]["status"] == "conflict"
    assert final["parallel_review"]["conflicts"][0]["reason"] == reason
    assert final["fallback"]["answer"]["readings"][0]["value"] == reviewed
