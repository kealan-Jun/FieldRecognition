from tests.support.archive_events import publish_document  # noqa: F401
import hashlib
import json
from pathlib import Path

import pytest

from field_recognition.archive.archive_paths import ArchivePaths, checked, load, resolve_file
from field_recognition.archive.archive_store import canonical
from tests.support.archive_store import archive  # noqa: F401
from tests.support.demo import app_client, scan, register  # noqa: F401
from tests.support.photo_measurements import setup, enqueue, complete, get_draft  # noqa: F401


def test_burst_conflict_correction_confirmation_and_retry_share_one_folder(setup, archive):
    app, client, binding, _ = setup
    root = archive[2]
    response, request = enqueue(setup, 3)
    mid = response["measurement_id"]
    complete(setup, 0, "51")
    app.archive_store.step(100)
    file = next(root.glob("*/VoicePhotoReadings/*/Evidence.json"))
    complete(setup, 1, "52")
    complete(setup, 2, "51")
    app.archive_store.step(100)
    result = json.loads(file.read_text())
    assert len(list(root.glob("*/VoicePhotoReadings/*/Evidence.json"))) == 1
    assert len(result["sources"]) == 3
    assert (
        json.loads((root / result["measurement_files"][0]["result"]).read_text())["values"][0][
            "value"
        ]
        is None
    )
    readme = (file.parent / "Readme.html").read_text()
    assert "连拍存在冲突" in readme and "51" in readme and "52" in readme
    assert "逐张原始识别文字" in readme and "合并值保持 null" in readme
    assert not (file.parent / "UnboundReadings.json").exists()
    draft = get_draft(client, mid)
    body = {
        "actor": "Reader",
        "revision": draft["revision"],
        "reason": "核对三张照片",
        "fields": [
            {
                "field_id": draft["fields"][0]["field_id"],
                "instrument_id": binding["instrument"]["id"],
                "name": "温度",
                "value": 51,
                "unit": "°C",
            }
        ],
    }
    revised = client.post(f"/api/photo-measurements/{mid}/revisions", json=body).json()
    confirmation = {"actor": "Reader", "revision": revised["revision"]}
    assert (
        client.post(f"/api/photo-measurements/{mid}/confirm", json=confirmation).status_code == 200
    )
    app.archive_store.step(100)
    result = json.loads(file.read_text())
    assert result["record_scope"] == "production_confirmed"
    assert (
        json.loads((root / result["measurement_files"][0]["result"]).read_text())["values"][0][
            "value"
        ]
        == 51
    )
    assert len(result["decision"]["corrections"]) == 1
    assert result["measurement_files"][0]["evidence"]["human_verified"]
    assert result["observations"][1]["readings"][0]["value"] == "52"
    before = {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.glob("**/*")
        if p.is_file() and p.suffix in {".png", ".jpg"}
    }
    count = app.archive_store.snapshot()["archived_receipts"]
    assert (
        client.post("/api/ocr/photo-burst", json=request).json()["job_ids"] == response["job_ids"]
    )
    assert (
        client.post(f"/api/photo-measurements/{mid}/confirm", json=confirmation).status_code == 200
    )
    app.archive_store.step(100)
    assert app.archive_store.snapshot()["archived_receipts"] == count
    assert all((root / p).read_bytes() == raw for p, raw in before.items())
    assert len({hashlib.sha256(raw).hexdigest() for raw in before.values()}) == len(before)
    assert len(list(root.glob("*/VoicePhotoReadings/*/Evidence.json"))) == 1
    assert app.archive_store.integrity.run_once()["issue_count"] == 0


def test_interrupted_move_keeps_old_bytes_readable_then_resumes(tmp_path, monkeypatch):
    root = tmp_path / "FieldRecognitionArchive"
    root.mkdir()
    old = root / "Objects/Photo.png"
    old.parent.mkdir()
    old.write_bytes(b"original")
    paths = ArchivePaths(root)
    original = Path.rename
    monkeypatch.setattr(
        Path, "rename", lambda *args: (_ for _ in ()).throw(OSError("NAS disconnected"))
    )
    target = "2026-09-14_Cam/Bindings/Event/Photos/Original.png"
    with pytest.raises(OSError):
        paths.move("Objects/Photo.png", target)
    assert load(root)["aliases"]["Objects/Photo.png"] == target
    assert resolve_file(root, "Objects/Photo.png").read_bytes() == b"original"
    monkeypatch.setattr(Path, "rename", original)
    ArchivePaths(root).move("Objects/Photo.png", target)
    assert resolve_file(root, "Objects/Photo.png").read_bytes() == b"original"
    assert not old.exists()
    with pytest.raises(ValueError):
        ArchivePaths(root).move(target, "../Outside.png")


def test_migration_conflicting_destination_never_overwrites_or_repoints(tmp_path):
    old, target = tmp_path / "Original.png", tmp_path / "Other.png"
    old.write_bytes(b"original")
    target.write_bytes(b"other")
    with pytest.raises(ValueError):
        ArchivePaths(tmp_path).move("Original.png", "Other.png")
    assert old.read_bytes() == b"original" and target.read_bytes() == b"other"
    assert not load(tmp_path)["aliases"]


def test_group_export_never_refills_a_conflicting_unit_from_registration():
    from field_recognition.archive.archive_events import standard_records
    from field_recognition.measurements.photo_measurements import candidates
    from tests.support.measurement_records import document

    doc = document()
    doc["readings"][0]["unit"] = "rpm"
    group = {"fields": candidates([doc]), "status": "draft"}
    entry = standard_records([{"seq": 1, "entity_id": "job", "doc": doc}], group)[0]
    assert entry["record"]["values"][0] == {
        "name": "温度",
        "value": None,
        "unit": None,
        "range": [None, None],
    }


def test_public_results_are_exact_six_fields_one_per_instrument_with_stable_paths(tmp_path):
    import copy
    from tests.support.measurement_records import document

    doc = document() | {"camera_id": "Cam", "submitted_at": "2026-09-14T05:43:10Z"}
    first = copy.deepcopy(doc)
    first["panel_regions"] = [r for r in first["panel_regions"] if r["instrument_id"] == "b"]
    file = publish_document(tmp_path, first)
    before = json.loads(file.read_text())["measurement_files"][0]
    assert Path(before["result"]).name == "Result.json"
    # A later burst member reveals A: B keeps its original filename.
    publish_document(tmp_path, doc)
    evidence = json.loads(file.read_text())
    assert "instrument_measurements" not in evidence
    assert len(evidence["measurement_files"]) == 2
    for entry in evidence["measurement_files"]:
        raw = (tmp_path / entry["result"]).read_bytes()
        record = json.loads(raw)
        assert set(record) == {
            "wearer_id",
            "device_model",
            "device_no",
            "qr_hash",
            "photo_time",
            "values",
        }
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        if entry["instrument_id"] == "b":
            assert entry["result"] == before["result"]
            assert record["values"][0]["name"] == "质量"
        else:
            assert [v["name"] for v in record["values"]] == ["温度", "转速"]
        assert "record" not in entry  # A reference, not another copy of the result.
    assert len(list(file.parent.glob("Result*.json"))) == 2
    publish_document(tmp_path, doc)
    assert json.loads(file.read_text()) == evidence


def test_skipped_unbound_photo_has_evidence_but_no_fake_measurement(tmp_path):
    from tests.support.measurement_records import document

    doc = document() | {
        "camera_id": "Cam",
        "panel_regions": [],
        "readings": [],
        "lines": [],
        "recognition_skipped": True,
        "skip_reason": "no_active_instrument_binding",
        "actual_model_invocation": False,
    }
    file = publish_document(tmp_path, doc)
    evidence = json.loads(file.read_text())
    assert evidence["status"] == "skipped" and evidence["processing_status"] == "completed"
    assert evidence["skip_reasons"] == ["no_active_instrument_binding"]
    assert evidence["measurement_files"] == []
    assert not list(file.parent.glob("Result*.json"))
    assert resolve_file(tmp_path, str(file.relative_to(tmp_path).with_name("Result.json"))) == file


def test_unbound_candidates_visible_without_formal_result_or_identity(tmp_path):
    from tests.support.measurement_records import document

    doc = document() | {
        "camera_id": "Cam",
        "panel_regions": [],
        "instrument": None,
        "readings": [
            {
                "reading_id": "candidate",
                "text": "144062",
                "raw_value": "144062",
                "value": "14.4062",
                "unit": "g",
                "instrument": None,
                "binding_id": None,
                "normalization": {
                    "method": "registered_fixed_decimal_v2",
                    "raw_text": "144062",
                    "instrument_id": None,
                    "value": "14.4062",
                    "decimal_places": 4,
                },
                "display_rule": {"basis": "user_confirmed_display_type"},
            }
        ],
    }
    file = publish_document(tmp_path, doc)
    evidence = json.loads(file.read_text())
    candidate_path = tmp_path / evidence["unbound_readings"]["path"]
    content = json.loads(candidate_path.read_text())
    reading = content["readings"][0]
    assert reading["job_id"] == doc["job_id"]
    assert reading["captured_at"] == doc["external_photo"]["captured_at"]
    assert reading["raw_text"] == reading["raw_value"] == "144062"
    assert reading["value"] == 14.4062 and reading["unit"] == "g"
    assert reading["normalization"]["decimal_places"] == 4
    assert reading["display_rule"]["basis"] == "user_confirmed_display_type"
    assert (
        content["instrument_id"]
        is content["qr_hash"]
        is reading["instrument_id"]
        is reading["qr_hash"]
        is None
    )
    assert not content["human_verified"] and not reading["human_verified"]
    assert content["record_scope"] == "unbound_candidates"
    assert evidence["measurement_files"] == [] and not list(file.parent.glob("Result*.json"))
    assert evidence["observations"][0]["readings"] == doc["readings"]
    assert content["receipt_versions"] == evidence["receipt_versions"]
    assert (
        hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        == evidence["unbound_readings"]["sha256"]
    )
    assert "14.4062" in (file.parent / "Readme.html").read_text()
    assert "非正式测量结果" in candidate_path.read_text()
    assert "未绑定读数候选" in next(tmp_path.glob("*/DailyReport/DailyReport.html")).read_text()
    doc["readings"][0].update(text="144000", raw_value="144000", value="14.4000")
    publish_document(tmp_path, doc)
    assert "14.4000" in (file.parent / "Readme.html").read_text()


@pytest.mark.parametrize("issue", ["unit_conflict", "display_precision_mismatch"])
def test_unbound_conflicts_retain_raw_evidence_without_effective_value(tmp_path, issue):
    from tests.support.measurement_records import document

    doc = document() | {
        "camera_id": "Cam",
        "panel_regions": [],
        "instrument": None,
        "readings": [
            {
                "text": "<script>144.062</script>",
                "value": "144.062",
                "unit": "g",
                "instrument": None,
                "quality_issue": issue,
                "normalized_value": {
                    "name": "质量",
                    "value": None,
                    "unit": "g",
                    "range": [None, None],
                },
            }
        ],
    }
    file = publish_document(tmp_path, doc)
    content = json.loads((file.parent / "UnboundReadings.json").read_text())
    reading = content["readings"][0]
    assert reading["value"] is None and reading["raw_value"] == "144.062"
    assert reading["quality_issue"] == issue
    if issue == "unit_conflict":
        assert reading["unit"] is None
    assert "不可作为有效值" in (file.parent / "Readme.html").read_text()
    assert "<script>" not in (file.parent / "Readme.html").read_text()
    assert not list(file.parent.glob("Result*.json"))


def test_photo_readme_uses_normalized_unit_and_preserves_missing_raw_unit():
    from field_recognition.archive.archive_events import photo_candidates, photo_readme

    doc = {
        "job_id": "unit-test",
        "capture_id": "photo",
        "readings": [
            {
                "text": "143976",
                "value": "14.3976",
                "unit": None,
                "instrument": {"id": "registered-balance"},
                "binding_id": "binding",
                "normalized_value": {
                    "name": "质量",
                    "value": 14.3976,
                    "unit": "g",
                    "range": [None, None],
                },
            }
        ],
    }
    readings = photo_candidates([doc], [], unbound_only=False)
    assert readings[0]["raw_unit"] is None and readings[0]["unit"] == "g"
    assert "14.3976 g" in photo_readme({"readings": readings}).decode()
    doc["readings"][0]["quality_issue"] = "unit_conflict"
    readings = photo_candidates([doc], [], unbound_only=False)
    assert readings[0]["raw_unit"] is None and readings[0]["unit"] is None
    assert readings[0]["value"] is None


def test_upgrade_replaces_generated_envelope_without_duplicate_results(tmp_path):
    from tests.support.measurement_records import document

    doc = document() | {"camera_id": "Cam"}
    file = publish_document(tmp_path, doc)
    evidence = json.loads(file.read_text())
    original = file.parent / "Result.json"
    # Simulate the old generated Result.json envelope tracked by the migration ledger.
    raw = canonical(
        {"schema": "field-recognition-event/1", "derived_view": True, "observations": [doc]}
    )
    original.write_bytes(raw)
    paths = ArchivePaths(tmp_path)
    paths.data["views"][original.relative_to(tmp_path).as_posix()] = hashlib.sha256(raw).hexdigest()
    paths.save()
    note = file.parent / "PersonalNote.txt"
    note.write_text("keep")
    publish_document(tmp_path, doc)
    assert "observations" not in json.loads(original.read_text())
    assert json.loads(file.read_text())["observations"] == evidence["observations"]
    assert note.read_text() == "keep"


def test_migration_cli_loads_only_literal_archive_settings(tmp_path, monkeypatch):
    from scripts.migrate_archive import load_settings

    monkeypatch.delenv("FIELD_ARCHIVE_ROOT", raising=False)
    monkeypatch.delenv("FIELD_ARCHIVE_ENABLED", raising=False)
    monkeypatch.delenv("NOT_AN_ARCHIVE_KEY", raising=False)
    path = tmp_path / "Settings.env"
    path.write_text(
        'FIELD_ARCHIVE_ENABLED=1\nFIELD_ARCHIVE_ROOT="/path with spaces/$(touch Nope)"\nNOT_AN_ARCHIVE_KEY=private\n'
    )
    load_settings(path)
    import os

    assert os.environ["FIELD_ARCHIVE_ROOT"] == "/path with spaces/$(touch Nope)"
    assert os.environ["FIELD_ARCHIVE_ENABLED"] == "1" and "NOT_AN_ARCHIVE_KEY" not in os.environ


def test_legacy_receipts_objects_and_urls_survive_migration_without_duplicates(archive):
    app, client, root = archive
    capture = scan(client)
    iid = capture["matches"][0]["id"]
    register(client, iid)
    binding = client.post(
        "/api/bindings",
        json={"scan_id": capture["scan_id"], "instrument_id": iid, "operator": "甲"},
    ).json()
    app.archive_store.step(100)
    paths = ArchivePaths(root)
    # Construct a legacy archive from the same fixture's real decoded QR evidence.
    for receipt in list(root.glob(".System/Receipts/**/*.json")):
        doc = json.loads(receipt.read_text())
        for artifact in doc["artifacts"].values():
            if not isinstance(artifact, dict):
                continue
            source = resolve_file(root, artifact["path"])
            digest = artifact["sha256"]
            logical = f"Objects/{digest[:2]}/{digest}{source.suffix}"
            target = root / logical
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            artifact["path"] = logical
        raw = canonical(doc)
        legacy = f"Receipts/Cam/2026-09-14/Instance/{doc['sequence']:012d}-{hashlib.sha256(raw).hexdigest()}.json"
        file = root / legacy
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(raw)
        with app.db() as conn:
            conn.execute(
                "UPDATE archive_outbox SET receipt_path=? WHERE seq=?", (legacy, doc["sequence"])
            )
        receipt.unlink()
    for item in paths.data["assets"].values():
        checked(root, item["path"]).unlink(missing_ok=True)
    for view in paths.data["views"]:
        checked(root, view).unlink(missing_ok=True)
    (root / ".System/MigrationMap.json").unlink()
    with app.db() as conn:
        rows = conn.execute("SELECT * FROM archive_outbox ORDER BY seq").fetchall()
    from field_recognition.archive.archive_browse import build_views

    for name, raw in build_views(rows).items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(raw)
    old = f"Records/Bindings/{binding['binding_id']}/Record.json"
    note = (root / old).with_name("PersonalNote.txt")
    note.write_text("保留手工备注")
    before = {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.glob("Receipts/**/*.json")
    }
    legacy_links = {"01_业务数据/绑定/Record.json": old}
    (root / "LegacyLinks.json").write_text(json.dumps(legacy_links))
    app.archive_store.receipt_cache = {}
    app.archive_store.write_index()
    assert note.read_text() == "保留手工备注"
    assert not (root / old).exists()
    assert client.get("/api/archive/files/" + old).json()["binding"]["operator"] == "甲"
    assert (
        client.get("/api/archive/files/01_业务数据/绑定/Record.json").json()["binding"][
            "binding_id"
        ]
        == binding["binding_id"]
    )
    for relative, raw in before.items():
        assert resolve_file(root, relative).read_bytes() == raw
    images = [p for p in root.rglob("*") if p.is_file() and p.suffix == ".png"]
    assert len(images) == len({hashlib.sha256(p.read_bytes()).hexdigest() for p in images})
    assert not (root / "Objects").exists() and not (root / "Receipts").exists()
    assert not (root / "Browse").exists() and not (root / "LegacyLinks.json").exists()
    assert app.archive_store.integrity.run_once()["issue_count"] == 0
    app.archive_store.write_index()
    assert all(resolve_file(root, p).read_bytes() == raw for p, raw in before.items())
