import hashlib
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PIL import Image

from tests.support.demo import app_client  # noqa: F401
from tests.support.photo_measurements import setup, complete  # noqa: F401
from tests.support.archive_store import archive  # noqa: F401
from tests.support.configuration import refresh_runtime_config


def source(setup, tmp_path, monkeypatch, count=3):
    app, _, _, _ = setup
    root = tmp_path / "VoicePhotos"
    moment = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(seconds=2)
    folder = root / "TestCamera" / moment.strftime("%Y-%m-%d/%H-%M-%S")
    folder.mkdir(parents=True)
    paths = []
    for i in range(count):
        path = folder / (moment.strftime("%Y%m%d_%H%M%S") + (f"_{i:03}" if i else "") + ".jpg")
        Image.new("RGB", (80, 60), (60 * i, 100, 70)).save(path)
        os.utime(path, (0, 0))
        paths.append(path)
    monkeypatch.setenv("FIELD_SAVED_PHOTO_ROOT", str(root))
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    refresh_runtime_config(app, "saved_photo_root", "camera_id")
    monkeypatch.setenv("FIELD_VOICE_PHOTO_BATCH_MODE", "directory_triplet")
    return root, paths


def test_three_nas_photos_one_measurement_without_modifying_source(
    setup, tmp_path, monkeypatch, archive
):
    app, client, _, queued = setup
    root, paths = source(setup, tmp_path, monkeypatch)
    before = {str(p): p.read_bytes() for p in paths}
    replies = []
    for p in paths:
        result = client.post("/api/ocr/photo-result", json={"image_path": str(p)})
        assert result.status_code == 202, result.text
        replies.append(result.json())
    assert len({r["measurement_id"] for r in replies}) == 1 and len(queued) == 3
    assert all(r["measurement_context"]["expected_photos"] == 3 for r in replies)
    assert all(
        r["external_photo"]["timestamp_basis"] == "nas_filename_local_time_not_hardware_verified"
        for r in replies
    )
    assert all(
        r["external_photo"]["batch_basis"] == "configured_directory_triplet" for r in replies
    )
    assert (
        client.post("/api/ocr/photo-result", json={"image_path": str(paths[0])}).json()["job_id"]
        == replies[0]["job_id"]
    )
    assert len(queued) == 3 and {str(p): p.read_bytes() for p in paths} == before
    assert not list(root.glob("**/PhotoReceipt.json"))
    with app.db() as conn:
        capture = json.loads(
            conn.execute(
                "SELECT document FROM scans WHERE id=?", (replies[0]["capture_id"],)
            ).fetchone()[0]
        )
    assert len(capture["photo_observation"]["source_receipt"]["photos"]) == 3
    assert capture["source_sha256"] == hashlib.sha256(before[str(paths[0])]).hexdigest()
    for i in range(3):
        complete(setup, i)
    app.archive_store.step(100)
    files = list(archive[2].glob("*/VoicePhotoReadings/*/Evidence.json"))
    assert len(files) == 1
    evidence = json.loads(files[0].read_text())
    assert len(evidence["sources"]) == 3
    assert all(a["batch_basis"] == "configured_directory_triplet" for a in evidence["acquisition"])
    assert all(a["time"]["quality"] == "filename_time" for a in evidence["acquisition"])
    assert evidence["record_scope"] == "draft"
    assert app.archive_store.integrity.run_once()["issue_count"] == 0


def test_incomplete_directory_processes_available_photos_and_rejects_over_three(
    setup, tmp_path, monkeypatch
):
    app, client, _, queued = setup
    _, paths = source(setup, tmp_path, monkeypatch, count=2)
    first = client.post("/api/ocr/photo-result", json={"image_path": str(paths[0])})
    assert first.status_code == 202 and len(queued) == 1
    for n in (2, 3):
        path = paths[0].with_name(paths[0].stem + f"_{n:03}.jpg")
        Image.new("RGB", (80, 60), "red").save(path)
        os.utime(path, (0, 0))
    result = client.post("/api/ocr/photo-result", json={"image_path": str(paths[0])})
    assert result.status_code == 409 and len(queued) == 1


def test_late_triplet_members_join_one_result_and_retries_never_reinfer(
    setup, tmp_path, monkeypatch, archive
):
    app, client, _, queued = setup
    _, paths = source(setup, tmp_path, monkeypatch, count=1)
    first_bytes = paths[0].read_bytes()
    responses = []
    original_receipt = None
    for index in range(3):
        if index:
            path = paths[0].with_name(paths[0].stem + f"_{index:03}.jpg")
            Image.new("RGB", (80, 60), (50 * index, 100, 70)).save(path)
            os.utime(path, (0, 0))
            paths.append(path)
        response = client.post("/api/ocr/photo-result", json={"image_path": str(paths[index])})
        assert response.status_code == 202, response.text
        responses.append(response.json())
        complete(setup, index, text="51")
        app.archive_store.step(100)
        evidence_files = list(archive[2].glob("*/VoicePhotoReadings/*/Evidence.json"))
        assert len(evidence_files) == 1
        evidence = json.loads(evidence_files[0].read_text())
        assert evidence["processing_terminal"] is True
        assert evidence["processing_outcome"] == "succeeded"
        assert evidence["photo_collection"] == {
            "arrived_photos": index + 1,
            "expected_photos": 3,
            "complete": index == 2,
            "processing_requires_all_expected_photos": False,
        }
        readings = json.loads(evidence_files[0].with_name("Readings.json").read_text())
        assert len(readings["readings"]) == index + 1
        # Same numeric value keeps each photograph as evidence in this ONE batch.
        assert {r["value"] for r in readings["readings"]} == {51.0}
        with app.db() as conn:
            capture = json.loads(conn.execute(
                "SELECT document FROM scans WHERE id=?", (responses[0]["capture_id"],)
            ).fetchone()[0])
            group = json.loads(conn.execute(
                "SELECT document FROM photo_measurements WHERE id=?",
                (responses[0]["measurement_id"],),
            ).fetchone()[0])
        if original_receipt is None:
            original_receipt = capture["photo_observation"]["source_receipt"]
            assert original_receipt["schema_version"] == "field-photo-directory-snapshot/1"
            assert len(original_receipt["photos"]) == 1
        assert capture["photo_observation"]["source_receipt"] == original_receipt
        assert group["fields"] and len(group["sources"]) == index + 1
        for existing, saved in zip(paths, responses):
            retry = client.post("/api/ocr/photo-result", json={"image_path": str(existing)})
            assert retry.status_code == 202, retry.text
            assert retry.json()["job_id"] == saved["job_id"]
        assert len(queued) == index + 1
    assert len({r["measurement_id"] for r in responses}) == 1
    assert len({r["measurement_context"]["burst_id"] for r in responses}) == 1
    assert paths[0].read_bytes() == first_bytes
    assert app.archive_store.integrity.run_once()["issue_count"] == 0


def test_default_does_not_infer_burst_and_existing_photo_is_not_regrouped(
    setup, tmp_path, monkeypatch
):
    _, client, _, queued = setup
    _, paths = source(setup, tmp_path, monkeypatch)
    monkeypatch.delenv("FIELD_VOICE_PHOTO_BATCH_MODE")
    first = client.post("/api/ocr/photo-result", json={"image_path": str(paths[0])}).json()
    assert first["measurement_context"]["burst_id"] is None
    monkeypatch.setenv("FIELD_VOICE_PHOTO_BATCH_MODE", "directory_triplet")
    assert (
        client.post("/api/ocr/photo-result", json={"image_path": str(paths[0])}).status_code == 409
    )
    assert len(queued) == 1


def test_uploaded_receipt_cannot_claim_an_internal_partial_snapshot(
    setup, tmp_path, monkeypatch
):
    _, client, _, queued = setup
    root, paths = source(setup, tmp_path, monkeypatch, count=1)
    from field_recognition.runtime.source_io import read

    snapshot = read({"root": str(root), "camera": "TestCamera", "zone": "Asia/Shanghai",
                     "path": str(paths[0]), "max_bytes": 100000,
                     "batch_mode": "directory_triplet"})["receipt"]
    (paths[0].parent / "PhotoReceipt.json").write_text(json.dumps(snapshot))
    response = client.post("/api/ocr/photo-result", json={"image_path": str(paths[0])})
    assert response.status_code == 422 and not queued


def test_triplet_cannot_read_a_symlink_as_a_member(setup, tmp_path, monkeypatch):
    _, client, _, queued = setup
    _, paths = source(setup, tmp_path, monkeypatch)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(paths[2].read_bytes())
    paths[2].unlink()
    paths[2].symlink_to(outside)
    assert (
        client.post("/api/ocr/photo-result", json={"image_path": str(paths[0])}).status_code == 422
    )
    assert not queued
