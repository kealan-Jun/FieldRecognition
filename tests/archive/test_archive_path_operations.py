"""Temporary-filesystem boundaries for deduplicated path operations."""

import hashlib
import os
from pathlib import Path

import pytest

from field_recognition.archive.archive_paths import ArchivePaths, LEDGER, load, resolve_file


def placed(tmp_path):
    root = tmp_path / "FieldRecognitionArchive"
    root.mkdir()
    paths = ArchivePaths(root)
    raw = b"original immutable evidence"
    artifact = paths.asset("Objects/Original.png", raw)
    target = "Day/Event/Photos/Original.png"
    paths.place(artifact, target)
    return root, paths, raw, artifact, target


@pytest.mark.parametrize("restore_mtime", [False, True])
def test_same_location_place_rejects_same_size_corruption(tmp_path, restore_mtime):
    root, paths, raw, artifact, target = placed(tmp_path)
    path = root / target
    timestamp = path.stat().st_mtime_ns
    damaged = b"x" * len(raw)
    path.write_bytes(damaged)
    if restore_mtime:
        os.utime(path, ns=(timestamp, timestamp))
    with pytest.raises(ValueError, match="corrupt archive evidence"):
        paths.place(artifact, target)
    assert path.read_bytes() == damaged
    assert paths.data["assets"][artifact["sha256"]]["path"] == target


def test_missing_placed_object_is_not_hidden_by_metadata(tmp_path):
    root, paths, _, artifact, target = placed(tmp_path)
    original_ledger = (root / LEDGER).read_bytes()
    (root / target).unlink()
    with pytest.raises(FileNotFoundError):
        paths.place(artifact, target)
    assert (root / LEDGER).read_bytes() == original_ledger
    assert paths.data["assets"][artifact["sha256"]]["path"] == target


def test_same_instance_resolves_new_moves_and_authoritative_returning_source(tmp_path):
    root, paths, raw, artifact, target = placed(tmp_path)
    assert paths.name(artifact["path"]) == target
    second = "Day/Other/Photos/Original.png"
    paths.move(target, second)
    assert paths.name(artifact["path"]) == second
    assert resolve_file(root, artifact["path"]).read_bytes() == raw
    # A positive alias/existence cache would keep pointing at the removed file.
    (root / second).unlink()
    returning = root / artifact["path"]
    returning.parent.mkdir(parents=True, exist_ok=True)
    returning.write_bytes(raw)
    assert paths.name(artifact["path"]) == artifact["path"]
    assert resolve_file(root, artifact["path"]).read_bytes() == raw


def test_interrupted_place_preserves_old_object_and_resumes_from_journal(tmp_path, monkeypatch):
    root = tmp_path / "FieldRecognitionArchive"
    root.mkdir()
    paths = ArchivePaths(root)
    raw = b"photo awaiting migration"
    artifact = paths.asset("Objects/Original.png", raw)
    target = "Day/Event/Photos/Original.png"
    rename = Path.rename
    with monkeypatch.context() as patch:
        patch.setattr(
            Path, "rename", lambda *_: (_ for _ in ()).throw(OSError("interrupted rename"))
        )
        with pytest.raises(OSError, match="interrupted rename"):
            paths.place(artifact, target)
    assert resolve_file(root, artifact["path"]).read_bytes() == raw
    assert load(root)["aliases"][artifact["path"]] == target
    assert not (root / target).exists()
    assert Path.rename is rename
    ArchivePaths(root).place(artifact, target)
    assert not (root / artifact["path"]).exists()
    assert resolve_file(root, artifact["path"]).read_bytes() == raw


def test_destination_created_after_journal_never_deletes_original(tmp_path, monkeypatch):
    root = tmp_path / "FieldRecognitionArchive"
    root.mkdir()
    old = root / "Original.png"
    old.write_bytes(b"original")
    target = root / "New.png"
    paths = ArchivePaths(root)
    save = paths.save

    def conflicting_writer():
        save()
        target.write_bytes(b"other bytes")

    monkeypatch.setattr(paths, "save", conflicting_writer)
    with pytest.raises(ValueError, match="destination conflict"):
        paths.move("Original.png", "New.png")
    assert old.read_bytes() == b"original"
    assert target.read_bytes() == b"other bytes"
    assert resolve_file(root, "Original.png").read_bytes() == b"original"


def test_each_target_is_resolved_again_after_external_symlink_change(tmp_path):
    root, paths, raw, artifact, target = placed(tmp_path)
    outside = tmp_path / "Outside"
    outside.mkdir()
    folder = root / "Destination"
    folder.mkdir()
    paths.alias("Unused.png", "Destination/Other.png")
    folder.rmdir()
    folder.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        paths.place(artifact, "Destination/Other.png")
    assert (root / target).read_bytes() == raw
    assert not list(outside.iterdir())


def test_symlinked_existing_source_cannot_escape_cached_root(tmp_path):
    root, paths, raw, artifact, target = placed(tmp_path)
    outside = tmp_path / "Private.png"
    outside.write_bytes(raw)
    (root / target).unlink()
    (root / target).symlink_to(outside)
    with pytest.raises(ValueError):
        paths.place(artifact, target)
    assert outside.read_bytes() == raw


def test_unchanged_save_recreates_externally_deleted_ledger(tmp_path):
    root, paths, raw, artifact, _ = placed(tmp_path)
    ledger = root / LEDGER
    previous = ledger.read_bytes()
    ledger.unlink()
    paths.save()
    assert ledger.read_bytes() == previous
    assert (
        hashlib.sha256(resolve_file(root, artifact["path"]).read_bytes()).hexdigest()
        == artifact["sha256"]
    )
    assert resolve_file(root, artifact["path"]).read_bytes() == raw


def test_deferred_assets_are_immutable_before_one_explicit_ledger_save(tmp_path):
    root = tmp_path / "FieldRecognitionArchive"
    root.mkdir()
    paths = ArchivePaths(root)
    images = {"Objects/First.png": b"first photo", "Objects/Second.png": b"second photo"}
    staged = {logical: paths.asset(logical, raw, save=False) for logical, raw in images.items()}
    assert not (root / LEDGER).exists()
    for logical, item in staged.items():
        assert (root / item["path"]).read_bytes() == images[logical]
        assert paths.name(logical) == item["path"]
    paths.save()
    reopened = ArchivePaths(root)
    for logical, item in staged.items():
        assert reopened.name(logical) == item["path"]
        assert resolve_file(root, logical).read_bytes() == images[logical]


def test_unsaved_pending_assets_can_be_retried_after_instance_loss(tmp_path):
    root = tmp_path / "FieldRecognitionArchive"
    root.mkdir()
    logical, raw = "Objects/Original.png", b"photo awaiting receipt"
    staged = ArchivePaths(root).asset(logical, raw, save=False)
    assert not (root / LEDGER).exists()
    retry = ArchivePaths(root)
    assert retry.asset(logical, raw, save=False) == staged
    retry.save()
    assert resolve_file(root, logical).read_bytes() == raw


def test_deferred_asset_conflict_preserves_existing_bytes_and_saved_ledger(tmp_path):
    root = tmp_path / "FieldRecognitionArchive"
    root.mkdir()
    logical, raw = "Objects/Original.png", b"photo awaiting receipt"
    staged = ArchivePaths(root).asset(logical, raw, save=False)
    damaged = b"conflicting pending object"
    (root / staged["path"]).write_bytes(damaged)
    retry = ArchivePaths(root)
    with pytest.raises(ValueError, match="Archive content conflict"):
        retry.asset(logical, raw, save=False)
    assert (root / staged["path"]).read_bytes() == damaged
    assert not (root / LEDGER).exists()
    assert retry.data["assets"] == {} and retry.data["aliases"] == {}
