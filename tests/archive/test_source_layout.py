import base64
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json

from PIL import Image
import pytest

from field_recognition.archive.archive_paths import ArchivePaths
from field_recognition.archive.source_layout import source_relative
from tests.support.archive_store import archive, receipts  # noqa: F401
from tests.support.demo import app_client  # noqa: F401
from tests.support.configuration import refresh_runtime_config


def test_uploaded_original_and_results_share_one_batch_without_a_second_photo(archive, monkeypatch):
    app, client, root = archive
    stamp = datetime.now(ZoneInfo("Asia/Shanghai"))
    path = root / stamp.strftime("%Y-%m-%d_TestCamera/MetaData/%H-%M-%S/%Y%m%d_%H%M%S.jpg")
    path.parent.mkdir(parents=True)
    Image.new("RGB", (160, 100), "gray").save(path)
    original, info = path.read_bytes(), path.stat()
    monkeypatch.setenv("FIELD_ARCHIVE_LAYOUT", "metadata")
    refresh_runtime_config(app, "archive_layout")
    queued = []
    monkeypatch.setattr(app.readout_pool, "submit", lambda fn, job: queued.append(job))
    response = client.post(
        "/api/ocr/photo-result",
        json={
            "photo": {
                "capture_id": path.relative_to(root).as_posix(),
                "camera_id": "TestCamera",
                "source_ref": str(path),
                "captured_at": stamp.isoformat(),
                "image_base64": base64.b64encode(original).decode(),
            }
        },
    )
    assert response.status_code == 202, response.text
    app.run_ocr(queued[0])
    app.archive_store.step(100)
    files = list(root.glob("*/VoicePhotoReadings/*/Evidence.json"))
    assert len(files) == 1
    evidence = json.loads(files[0].read_text())
    assert evidence["sources"][0]["original"]["path"] == path.relative_to(root).as_posix()
    assert evidence["sources"][0]["image"] is None
    assert "已完成识别，未读出" in files[0].with_name("Readme.html").read_text()
    assert files[0].parents[2] == path.parents[2]
    assert len(list(root.glob("**/*.jpg"))) == 1
    assert not list(root.glob("**/*.png"))
    assert path.read_bytes() == original and path.stat().st_mtime_ns == info.st_mtime_ns
    assert app.archive_store.integrity.run_once()["issue_count"] == 0
    saved_receipts = {p: p.read_bytes() for p in root.glob(".System/Receipts/**/*.json")}
    app.archive_store.step(100)
    assert all(p.read_bytes() == raw for p, raw in saved_receipts.items())
    assert all(
        item["path"] == path.relative_to(root).as_posix()
        for receipt in receipts(root)
        for item in receipt["artifacts"].values()
        if isinstance(item, dict) and item.get("source_owned")
    )


def test_adopted_original_cannot_be_moved_or_accepted_after_byte_changes(tmp_path):
    relative = "Camera/2026-10-14/12-30-00/20261014_123000.jpg"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original")
    paths = ArchivePaths(tmp_path, layout="metadata")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    asset = paths.adopt_source(relative, digest)
    assert paths.place(asset, "Other/Photo.jpg")["path"] == relative
    assert path.read_bytes() == b"original" and not (tmp_path / "Other").exists()
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256"):
        paths.adopt_source(relative, digest)


def test_history_moves_outputs_and_reuses_originals_without_rewriting_receipts(
    archive, tmp_path, monkeypatch
):
    from field_recognition.archive.archive_store import ArchiveStore
    from field_recognition.archive.archive_paths import resolve_file

    app, client, root = archive
    legacy = tmp_path / "voice_photos"
    stamp = datetime.now(ZoneInfo("Asia/Shanghai"))
    relative = "TestCamera/" + stamp.strftime("%Y-%m-%d/%H-%M-%S/%Y%m%d_%H%M%S.jpg")
    path = legacy / relative
    path.parent.mkdir(parents=True)
    Image.new("RGB", (160, 100), "gray").save(path)
    original = path.read_bytes()
    queued = []
    monkeypatch.setattr(app.readout_pool, "submit", lambda fn, job: queued.append(job))
    response = client.post(
        "/api/ocr/photo-result",
        json={
            "photo": {
                "capture_id": relative,
                "camera_id": "TestCamera",
                "source_ref": str(path),
                "captured_at": stamp.isoformat(),
                "image_base64": base64.b64encode(original).decode(),
            }
        },
    )
    assert response.status_code == 202
    app.run_ocr(queued[0])
    app.archive_store.step(100)
    old_evidence = next(root.glob("*/VoicePhotoReadings/*/Evidence.json"))
    before = {p: p.read_bytes() for p in root.glob(".System/Receipts/**/*.json")}
    # Also exercise recovery of an earlier missing normalized artifact.
    old_png = next(root.glob("**/*.png"))
    old_png.unlink()
    monkeypatch.setenv("FIELD_ARCHIVE_LAYOUT", "metadata")
    monkeypatch.setenv("FIELD_SAVED_PHOTO_ROOT", str(root))
    monkeypatch.setenv("FIELD_SAVED_PHOTO_ADDITIONAL_ROOTS", json.dumps([str(legacy)]))
    refresh_runtime_config(
        app, "archive_layout", "saved_photo_root", "saved_photo_additional_roots"
    )
    migrated = ArchiveStore(app.runtime)
    migrated.step(100)
    new_evidence = next(root.glob("*/VoicePhotoReadings/*/Evidence.json"))
    assert (
        resolve_file(root, old_evidence.relative_to(root).as_posix())
        == new_evidence
        == old_evidence
    )
    target = root / stamp.strftime("%Y-%m-%d_TestCamera/MetaData/%H-%M-%S/%Y%m%d_%H%M%S.jpg")
    assert target.read_bytes() == path.read_bytes() == original
    assert len(list(root.glob("**/*.jpg"))) == 1
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert list(root.glob(".System/Recovery/*.json"))
    assert migrated.integrity.run_once()["issue_count"] == 0


def test_recovery_never_fabricates_missing_bytes_or_accepts_another_local_image(tmp_path):
    from field_recognition.archive.archive_recovery import recover_missing

    root, data = tmp_path / "Archive", tmp_path / "Data"
    root.mkdir()
    (data / "Images").mkdir(parents=True)
    (data / "Images/photo.png").write_bytes(b"wrong bytes")
    artifact = {
        "path": "Objects/Original.png",
        "sha256": hashlib.sha256(b"correct bytes").hexdigest(),
        "size_bytes": len(b"correct bytes"),
    }
    receipt = {
        "document": {"capture_id": "photo", "image_sha256": artifact["sha256"]},
        "artifacts": {"image": artifact},
    }
    paths = ArchivePaths(root)
    assert recover_missing(paths, [receipt], data) == []
    assert not (root / "Objects/Original.png").exists()
    assert (data / "Images/photo.png").read_bytes() == b"wrong bytes"


@pytest.mark.parametrize(
    "relative",
    [
        "Camera/2026-10-13/12-30-00/20261014_123000.jpg",
        "OtherCamera/2026-10-14/12-30-00/20261014_123000.jpg",
        "2026-10-14_Camera/VoicePhotoReadings/Event/Photos/Original.jpg",
        ".System/Receipts/Any/20261014_123000.jpg",
    ],
)
def test_only_capture_owned_source_grammar_can_choose_result_location(tmp_path, relative):
    assert (
        source_relative(
            tmp_path,
            {
                "camera_id": "Camera",
                "external_photo": {
                    "source_ref": str(tmp_path / relative),
                    "camera_id": "Camera",
                },
            },
        )
        is None
    )
