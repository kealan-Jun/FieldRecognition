"""Explicit component settings survive ambient environment changes."""

import base64

import pytest

from field_recognition.capture.saved_photo import load_saved_bundle
from field_recognition.runtime.factory import create_runtime
from field_recognition.runtime.production_config import ProductionConfig


def set_conflicting_environment(monkeypatch, tmp_path):
    for key in (
        "FIELD_ARCHIVE_ENABLED",
        "FIELD_SAVED_PHOTO_WATCH_ENABLED",
        "FIELD_AUTO_RUN_ENABLED",
        "FIELD_VIDEO_OCR_ENABLED",
    ):
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("FIELD_SERVICE_ROLE", "api")
    monkeypatch.setenv("FIELD_PRODUCTION_ENABLED", "1")
    monkeypatch.setenv("FIELD_ARCHIVE_ROOT", str(tmp_path / "wrong-archive"))
    monkeypatch.setenv("FIELD_ARCHIVE_MOUNT", str(tmp_path / "wrong-mount"))
    monkeypatch.setenv("FIELD_ARCHIVE_CHECK_SECONDS", "3600")
    monkeypatch.setenv("FIELD_SAVED_PHOTO_ROOT", str(tmp_path / "wrong-photos"))
    monkeypatch.setenv("FIELD_SAVED_PHOTO_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("FIELD_CAMERA_ID", "wrong-camera")
    monkeypatch.setenv("FIELD_VOICE_PHOTO_BATCH_MODE", "receipt_only")
    monkeypatch.setenv("FIELD_PHOTO_RECEIPT_REQUIRED", "0")
    monkeypatch.delenv("FIELD_RECEIVER_URL", raising=False)
    monkeypatch.delenv("FIELD_CAMERA_SNAPSHOT_URL", raising=False)


def test_explicit_component_values_survive_environment_drift(tmp_path, monkeypatch):
    from field_recognition.runtime import source_io
    from field_recognition.archive import archive_store

    mount = tmp_path / "snapshot-mount"
    root = mount / "FieldRecognitionArchive"
    root.mkdir(parents=True)
    photo_root = tmp_path / "snapshot-photos"
    config = ProductionConfig(
        database_path=str(tmp_path / "local.sqlite3"),
        service_role="ocr",
        camera_id="snapshot-camera",
        archive_enabled=True,
        archive_root=str(root),
        archive_mount=str(mount),
        archive_check_seconds=7200,
        saved_photo_watch_enabled=True,
        saved_photo_root=str(photo_root),
        saved_photo_timezone="UTC",
        auto_run_enabled=True,
        video_ocr_enabled=True,
    )
    set_conflicting_environment(monkeypatch, tmp_path)
    runtime = create_runtime(config)
    calls = []
    monkeypatch.setattr(source_io, "call", lambda payload: calls.append(payload) or {"files": []})
    monkeypatch.setattr(archive_store.os.path, "ismount", lambda path: path == mount)
    try:
        store = runtime["archive_store"]
        assert store.enabled() is True
        assert store.service_role() == "ocr"
        assert store._root(create=False) == root
        assert store.integrity.interval() == 7200
        assert store.snapshot()["root"] == str(root)
        # A request/OCR role cannot accidentally start another archive writer.
        store.start()
        assert store.worker is None
        watcher = runtime["saved_photo_watcher"]
        assert watcher.enabled() is True
        assert watcher.snapshot()["configured"] is True
        watcher.discover()
        assert calls[0]["root"] == str(photo_root)
        assert calls[0]["camera"] == "snapshot-camera"
        assert calls[0]["zone"] == "UTC"
        runner = runtime["automatic_runner"]
        assert runner.target() == "snapshot-camera"
        assert runner.settings()["enabled"] is True
        assert runtime["video_ocr"].enabled() is True
        # Saved user settings retain precedence over deployment defaults.
        runner._save(dict(runner.settings(), enabled=False, video_ocr_enabled=False))
        assert runner.settings()["enabled"] is False
        assert runtime["video_ocr"].enabled() is False
    finally:
        runtime.close(wait=True)


def test_explicit_disabled_components_do_not_inherit_environment_enablement(tmp_path, monkeypatch):
    config = ProductionConfig(database_path=str(tmp_path / "disabled.sqlite3"), service_role="ocr")
    for key in (
        "FIELD_ARCHIVE_ENABLED",
        "FIELD_SAVED_PHOTO_WATCH_ENABLED",
        "FIELD_AUTO_RUN_ENABLED",
        "FIELD_VIDEO_OCR_ENABLED",
    ):
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("FIELD_ARCHIVE_ROOT", str(tmp_path / "ambient" / "FieldRecognitionArchive"))
    monkeypatch.setenv("FIELD_SAVED_PHOTO_ROOT", str(tmp_path / "ambient-photos"))
    monkeypatch.setenv("FIELD_CAMERA_ID", "ambient-camera")
    runtime = create_runtime(config)
    try:
        assert not runtime["archive_store"].enabled()
        assert runtime["archive_store"].snapshot()["root"] is None
        assert not runtime["saved_photo_watcher"].enabled()
        assert not runtime["saved_photo_watcher"].snapshot()["configured"]
        assert not runtime["automatic_runner"].settings()["enabled"]
        assert not runtime["video_ocr"].enabled()
        assert runtime["automatic_runner"].target() == "UnconfiguredNeckCamera"
    finally:
        runtime.close(wait=True)


def test_saved_photo_import_uses_the_same_root_and_timezone_snapshot(tmp_path, monkeypatch):
    from field_recognition.runtime import source_io

    config = ProductionConfig(
        saved_photo_root=str(tmp_path / "snapshot-photos"), saved_photo_timezone="UTC"
    )
    set_conflicting_environment(monkeypatch, tmp_path)
    observed = []
    relative = "snapshot-camera/2026-10-09/12-30-00/20261009_123000.jpg"

    def read(payload):
        observed.append(payload)
        return {
            "relative": relative,
            "path": "synthetic-source",
            "mtime": 1,
            "image_base64": base64.b64encode(b"synthetic-photo").decode(),
            "receipt": None,
        }

    monkeypatch.setattr(source_io, "call", read)
    photo, context, evidence = load_saved_bundle(
        relative, expected_camera="snapshot-camera", max_bytes=1000, config=config
    )
    assert observed[0]["root"] == config.saved_photo_root
    assert observed[0]["zone"] == "UTC"
    assert photo.captured_at.isoformat() == "2026-10-09T12:30:00+00:00"
    assert photo.camera_id == "snapshot-camera"
    assert context is evidence is None


def test_plain_legacy_components_keep_environment_defaults(tmp_path, monkeypatch):
    from field_recognition.archive.archive_store import ArchiveStore
    from field_recognition.capture.automation import AutomaticRunner
    from field_recognition.capture.photo_watch import SavedPhotoWatcher
    from field_recognition.capture.video_ocr import VideoOcr

    config = ProductionConfig(database_path=str(tmp_path / "legacy.sqlite3"), service_role="ocr")
    runtime = create_runtime(config)
    for key in (
        "FIELD_ARCHIVE_ENABLED",
        "FIELD_SAVED_PHOTO_WATCH_ENABLED",
        "FIELD_AUTO_RUN_ENABLED",
        "FIELD_VIDEO_OCR_ENABLED",
    ):
        monkeypatch.setenv(key, "yes")
    root = tmp_path / "FieldRecognitionArchive"
    root.mkdir()
    monkeypatch.setenv("FIELD_ARCHIVE_ROOT", str(root))
    monkeypatch.delenv("FIELD_ARCHIVE_MOUNT", raising=False)
    monkeypatch.setenv("FIELD_ARCHIVE_CHECK_SECONDS", "7200")
    monkeypatch.setenv("FIELD_SAVED_PHOTO_ROOT", str(tmp_path / "legacy-photos"))
    monkeypatch.setenv("FIELD_SAVED_PHOTO_TIMEZONE", "UTC")
    monkeypatch.setenv("FIELD_CAMERA_ID", "legacy-camera")
    legacy = dict(runtime)
    del legacy["config"]
    try:
        store = ArchiveStore(legacy)
        watcher = SavedPhotoWatcher(legacy)
        runner = AutomaticRunner(legacy)
        legacy["automatic_runner"] = runner
        video = VideoOcr(legacy)
        assert store.enabled() and store._root(create=False) == root
        assert store.integrity.interval() == 7200
        assert watcher.enabled() and watcher.target() == "legacy-camera"
        assert watcher.configured_root() == str(tmp_path / "legacy-photos")
        assert watcher.timezone() == "UTC"
        assert runner.target() == "legacy-camera" and runner.settings()["enabled"]
        assert video.enabled()
    finally:
        runtime.close(wait=True)


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_component_environment_truth_values_preserve_legacy_semantics(monkeypatch, value):
    for key in (
        "FIELD_ARCHIVE_ENABLED",
        "FIELD_SAVED_PHOTO_WATCH_ENABLED",
        "FIELD_AUTO_RUN_ENABLED",
        "FIELD_VIDEO_OCR_ENABLED",
    ):
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FIELD_ARCHIVE_ROOT", "/synthetic/FieldRecognitionArchive")
    config = ProductionConfig.from_environment()
    assert config.archive_enabled and config.saved_photo_watch_enabled
    assert config.auto_run_enabled and config.video_ocr_enabled
