import json
import shutil
from datetime import datetime, timedelta, timezone

from PIL import Image
import pytest

from field_recognition.capture.photo_watch import SavedPhotoWatcher
from field_recognition.runtime import source_io
from field_recognition.runtime.production_config import ProductionConfig
from field_recognition.capture.photo_sources import photo_location
from tests.support.configuration import refresh_runtime_config
from tests.support.demo import app_client  # noqa: F401


def test_roots_keep_one_capture_identity_and_exclude_generated_outputs(tmp_path):
    new, old = tmp_path / "FieldRecognitionArchive", tmp_path / "voice_photos"
    relative = "Camera/2026-10-14/12-30-00/20261014_123000.jpg"
    for root in [old, new]:
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b"same original bytes")
    derived = new / "2026-10-14_Camera/VoicePhotoReadings/Event/Photos/20261014_123000.jpg"
    derived.parent.mkdir(parents=True)
    derived.write_bytes(b"generated output")
    request = {
        "root": str(new),
        "roots": [str(new), str(old)],
        "camera": "Camera",
        "since": "2000-01-01T00:00:00+00:00",
        "zone": "Asia/Shanghai",
        "recent": False,
    }
    found = source_io.discover(request)
    assert [row[0] for row in found["files"]] == [relative]
    assert len(found["source_roots"]) == 2
    loaded = source_io.read(request | {"path": relative, "max_bytes": 1000})
    assert loaded["relative"] == relative and loaded["path"] == str(new / relative)
    (new / relative).unlink()
    assert source_io.read(request | {"path": relative, "max_bytes": 1000})["path"] == str(
        old / relative
    )
    assert not (new / relative).exists()  # No local/missing-source fallback creation.


def test_uploading_directory_is_not_discovered_or_read(tmp_path):
    path = tmp_path / "Camera/2026-10-14/12-30-00/20261014_123000.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"photo")
    (path.parent / ".uploading").touch()
    request = {
        "root": str(tmp_path),
        "camera": "Camera",
        "zone": "Asia/Shanghai",
        "since": "2000-01-01T00:00:00+00:00",
        "path": str(path),
        "max_bytes": 100,
    }
    assert not source_io.discover(request)["files"]
    with pytest.raises(source_io.SourceError, match="上传"):
        source_io.read(request)


def test_uploader_can_create_metadata_after_watcher_start_and_move_without_reidentification(
    tmp_path,
):
    import os
    import time

    root = tmp_path / "FieldRecognitionArchive"
    root.mkdir()
    request = {
        "root": str(root),
        "camera": "Camera",
        "zone": "Asia/Shanghai",
        "since": "2000-01-01T00:00:00+00:00",
        "max_bytes": 1000,
        "batch_mode": "directory_triplet",
    }
    assert not source_io.discover(request)["files"]
    assert not list(root.iterdir())  # Recognition never pre-creates uploader directories.
    directory = root / "2026-10-14_Camera/MetaData/12-30-00"
    directory.mkdir(parents=True)
    for suffix in ["", "_001", "_002"]:
        path = directory / ("20261014_123000" + suffix + ".jpg")
        path.write_bytes(("photo" + suffix).encode())
        os.utime(path, (time.time() - 2, time.time() - 2))
    found = source_io.discover(request)
    assert len(found["files"]) == 3
    first = directory / "20261014_123000.jpg"
    location = photo_location(first.relative_to(root).as_posix())
    loaded = source_io.read(request | {"path": location.identity})
    assert loaded["relative"] == "Camera/2026-10-14/12-30-00/20261014_123000.jpg"
    assert loaded["path"] == str(first)
    assert {p["capture_id"] for p in loaded["receipt"]["photos"]} == {
        photo_location(row[0]).identity for row in found["files"]
    }
    # A compatibility copy during the handover does not become three new captures.
    old = root / "Camera/2026-10-14/12-30-00"
    old.mkdir(parents=True)
    for path in directory.glob("*.jpg"):
        shutil.copyfile(path, old / path.name)
        os.utime(old / path.name, (time.time() - 2, time.time() - 2))
    legacy = source_io.read(request | {"path": str(old / first.name)})
    assert loaded["receipt"]["measurement"] == legacy["receipt"]["measurement"]
    assert loaded["receipt"]["photos"] == legacy["receipt"]["photos"]
    assert len(source_io.discover(request)["files"]) == 3


def test_unavailable_extra_source_cannot_hide_working_metadata_source(tmp_path):
    root = tmp_path / "available"
    directory = root / "2026-10-14_Camera/MetaData/12-30-00"
    directory.mkdir(parents=True)
    (directory / "20261014_123000.jpg").write_bytes(b"photo")
    found = source_io.discover(
        {
            "root": str(tmp_path / "missing"),
            "roots": [str(tmp_path / "missing"), str(root)],
            "camera": "Camera",
            "zone": "Asia/Shanghai",
            "since": "2000-01-01T00:00:00+00:00",
        }
    )
    assert len(found["files"]) == 1 and found["source_roots"][0]["status"] == "unavailable"


@pytest.mark.parametrize("value", ['"path"', "[null]", '[""]', "invalid-json"])
def test_invalid_additional_roots_fail_at_configuration(monkeypatch, value):
    monkeypatch.setenv("FIELD_SAVED_PHOTO_ADDITIONAL_ROOTS", value)
    with pytest.raises(ValueError):
        ProductionConfig.from_environment()


def test_relocation_reuses_job_and_original_receipt_without_a_second_inference(
    app_client, tmp_path, monkeypatch
):
    app, client = app_client
    new, old = tmp_path / "new", tmp_path / "old"
    new.mkdir()
    old.mkdir()
    stamp = datetime.now(timezone(timedelta(hours=8))) + timedelta(seconds=1)
    relative = "TestCamera/" + stamp.strftime("%Y-%m-%d/%H-%M-%S/%Y%m%d_%H%M%S.jpg")
    original = old / relative
    original.parent.mkdir(parents=True)
    Image.new("RGB", (100, 100), "gray").save(original)
    monkeypatch.setenv("FIELD_SAVED_PHOTO_ROOT", str(new))
    monkeypatch.setenv("FIELD_CAMERA_ID", "TestCamera")
    monkeypatch.setenv("FIELD_SAVED_PHOTO_ADDITIONAL_ROOTS", json.dumps([str(old)]))
    monkeypatch.setenv("FIELD_VOICE_PHOTO_BATCH_MODE", "receipt_only")
    refresh_runtime_config(app, "saved_photo_root", "saved_photo_additional_roots", "camera_id")
    queued = []
    monkeypatch.setattr(app.readout_pool, "submit", lambda fn, job: queued.append(job))
    first = client.post("/api/ocr/photo-result", json={"image_path": relative})
    assert first.status_code == 202, first.text
    relocated = new / relative
    relocated.parent.mkdir(parents=True)
    shutil.copyfile(original, relocated)
    second = client.post("/api/ocr/photo-result", json={"image_path": relative})
    assert second.status_code == 202, second.text
    assert first.json()["job_id"] == second.json()["job_id"] and len(queued) == 1
    assert second.json()["external_photo"]["source_ref"] == str(original)
    assert original.read_bytes() == relocated.read_bytes()
