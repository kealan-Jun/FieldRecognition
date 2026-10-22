import copy
import hashlib
import json

import cv2
import numpy as np
import pytest

from field_recognition.recognition import panel_regions, aliyun_vision
from field_recognition.recognition.photo_orientation import Rotation, recover, source_panel
from tests.support.photo_orientation import scene, core, Detector, Ocr
from tests.support.panel_regions import A, B, configure
from tests.support.parallel_photo import parallel_context, enqueue, running  # noqa: F401
from tests.support.demo import app_client  # noqa: F401
from tests.support.archive_store import archive  # noqa: F401
from tests.support.multi_readout import two_bindings
from tests.support.unbound_readout import photo
from tests.support.panel_readout import cloud_result


@pytest.mark.parametrize("degrees", [90, 180, 270])
@pytest.mark.parametrize("offset", [(0, 0), (13, 17)])
def test_pixel_centers_and_half_open_boxes_map_to_the_original_without_off_by_one(degrees, offset):
    source = np.arange(7 * 11 * 3, dtype=np.uint8).reshape(7, 11, 3)
    rotation = Rotation(degrees, 11, 7, *offset)
    working = rotation.image(source)
    assert rotation.box([0, 0, working.shape[1], working.shape[0]]) == [
        offset[0],
        offset[1],
        11 + offset[0],
        7 + offset[1],
    ]
    for u, v in ((0, 0), (working.shape[1] - 1, working.shape[0] - 1), (2, 3)):
        x, y = rotation.polygon([[u, v]])[0]
        assert np.array_equal(working[v, u], source[int(y) - offset[1], int(x) - offset[0]])
    crop = [1, 2, working.shape[1] - 2, working.shape[0] - 3]
    region = {"crop": rotation.crop(crop), "photo_orientation": rotation.metadata()}
    canvas = np.zeros((7 + offset[1], 11 + offset[0], 3), np.uint8)
    canvas[offset[1] :, offset[0] :] = source
    expected = working[crop[1] : crop[1] + crop[3], crop[0] : crop[0] + crop[2]]
    assert np.array_equal(source_panel(canvas, region), expected)


def test_direction_uses_spatial_lettering_and_ignores_higher_inverted_detector_score():
    source, _ = scene()
    context, detector, ocr = core(source)
    rotation, detection, proof = recover(context, source, {B})
    assert rotation.degrees == 90 and proof["basis"] == "source_lettering_multi_view"
    assert (
        detection["boxes"][0]["confidence"]
        < proof["attempts"][2]["detection"]["boxes"][0]["confidence"]
    )
    assert detector.calls == [90, 180, 270] and len(ocr.calls) == 4
    assert proof["readings_used_for_orientation"] is False
    assert any(item["support"] for item in proof["attempts"][0]["lettering"])


@pytest.mark.parametrize("lettering,expected", [("numbers", "unconfirmed"), ("both", "ambiguous")])
def test_numeric_confidence_and_competing_lettering_cannot_choose_direction(lettering, expected):
    source, _ = scene()
    context, _, _ = core(source, lettering=lettering)
    rotation, detection, proof = recover(context, source, {B})
    assert rotation is detection is None and proof["status"] == expected


def test_generic_type_and_other_binding_never_gain_identity_from_rotation():
    source, _ = scene()
    for generic, allowed in ((True, {B}), (False, {A})):
        context, _, ocr = core(source, generic=generic)
        rotation, _, proof = recover(context, source, allowed)
        assert rotation is None and proof["reason"] == "no_authorized_rotated_panel"
        assert not ocr.calls


@pytest.mark.parametrize("native_rows,competing", [(True, False), (False, False), (True, True)])
def test_horizontal_pair_needs_native_rows_and_a_unique_direction(native_rows, competing):
    upright = np.full((120, 100, 3), 90, np.uint8)
    bounds = [[12, 42, 42, 65], [53, 42, 83, 65]]
    if native_rows:
        for left, top, right, bottom in bounds:
            upright[top:bottom, left:right] = (3, 3, 12)
            cv2.rectangle(upright, (left + 3, top + 3), (right - 3, bottom - 3), (25, 70, 220), 2)
    source = np.ascontiguousarray(np.rot90(upright, 3))
    context, _, _ = core(source, lettering="numbers")

    def detect(pixels, **kwargs):
        degree = next(
            d for d in (0, 90, 180, 270) if np.array_equal(pixels, np.rot90(source, d // 90))
        )
        boxes = []
        if degree == 90 or (degree == 270 and competing):
            coords = bounds if degree == 90 else [[17, 55, 47, 78], [58, 55, 88, 78]]
            boxes = [
                {"class_id": 0, "instrument_id": A, "confidence": 0.8, "xyxy": bound}
                for bound in coords
            ]
            # Another authorized asset cannot inherit A's directional evidence.
            boxes.append(
                {"class_id": 1, "instrument_id": B, "confidence": 0.9, "xyxy": [5, 85, 90, 105]}
            )
        return {"status": "completed", "boxes": boxes}

    context["panel_detector"].predict = detect
    rotation, detection, proof = recover(context, source, {A, B})
    if native_rows and not competing:
        assert rotation.degrees == 90 and len(detection["boxes"]) == 2
        assert proof["basis"] == "unique_horizontal_pair_and_native_windows"
        assert proof["supported_instrument_ids"] == [A]
        assert not proof["ocr_calls"]
        assert detection["diagnostics"]["excluded_candidates"] == [
            {
                "class_id": 1,
                "instrument_id": B,
                "confidence": 0.9,
                "xyxy": [5, 85, 90, 105],
                "reason": "panel_orientation_unconfirmed",
            }
        ]
    else:
        assert rotation is detection is None
        assert proof["status"] == ("ambiguous" if competing else "unconfirmed")


def test_matching_letters_at_different_positions_do_not_prove_direction():
    source, _ = scene()
    context, _, ocr = core(source)

    def moved(pixels, x=0, y=0):
        result = ocr(pixels, x, y)
        if pixels.shape[1] == 184:
            for line in result["lines"]:
                line["polygon"] = (np.asarray(line["polygon"]) + [70, 70]).tolist()
        return result

    context["predict_panel"] = moved
    rotation, _, proof = recover(context, source, {B})
    assert rotation is None and proof["reason"] == "source_direction_unconfirmed"


@pytest.mark.parametrize("allowed,video,detector_calls", [([], False, 0), ([B], True, 1)])
def test_unbound_photo_and_live_video_do_not_run_rotation_recovery(allowed, video, detector_calls):
    source, _ = scene()
    context, detector, _ = core(source)
    result = panel_regions.predict(context, source, allowed_instrument_ids=allowed, video=video)
    assert len(detector.calls) == detector_calls
    assert not result.get("photo_orientation")


def test_prepared_rotation_reuses_direction_once_and_rejects_changed_source(monkeypatch):
    from field_recognition.recognition import lcd_digits

    source, upright = scene()
    context, detector, ocr = core(source)

    def retain_geometry(predict, image, initial, *args, **kwargs):
        return dict(
            initial, red_display_geometry={"polygon": copy.deepcopy(initial["lines"][0]["polygon"])}
        )

    monkeypatch.setattr(lcd_digits, "refine_lcd", retain_geometry)
    template = panel_regions.predict(
        context, source, allowed_instrument_ids=[B], localization_only=True
    )
    assert template["photo_orientation"]["status"] == "recovered"
    assert template["panel_regions"][0]["bbox"] == [40, 20, 70, 80]
    assert template["ocr_model_call_count"] == 4
    result = panel_regions.predict(
        context, source, allowed_instrument_ids=[B], detection=template["panel_detection"]
    )
    assert detector.calls == [0, 90, 180, 270]
    assert len(ocr.calls) == result["ocr_model_call_count"] == 5
    region = result["panel_regions"][0]
    assert region["measurement_name"] == "质量"
    assert region["local_ocr"]["red_display_geometry"]["polygon"] == [
        [68, 21],
        [68, 43],
        [59, 43],
        [59, 21],
    ]
    assert np.array_equal(source_panel(source, region), upright[48:82, 18:82])
    changed = source.copy()
    changed[0, 0] ^= 255
    with pytest.raises(ValueError, match="does not match"):
        panel_regions.predict(
            context, changed, allowed_instrument_ids=[B], detection=template["panel_detection"]
        )
    with pytest.raises(ValueError, match="does not match"):
        panel_regions.predict(
            context, source, allowed_instrument_ids=[A], detection=template["panel_detection"]
        )


def test_unconfirmed_orientation_still_reports_real_ocr_calls():
    source, _ = scene()
    context, _, ocr = core(source, lettering="numbers")
    result = panel_regions.predict(context, source, allowed_instrument_ids=[B])
    assert (
        result["recognition_skipped"]
        and result["failure_reason"] == "panel_orientation_unconfirmed"
    )
    assert result["actual_model_invocation"]
    assert result["ocr_model_call_count"] == len(ocr.calls) == 4


def test_parallel_crops_archive_and_formal_builder_keep_the_original_coordinate_contract(
    parallel_context, archive, monkeypatch
):
    app, client = parallel_context
    two_bindings(app, client)
    configure(app, monkeypatch, [])
    source, upright = scene()
    detector, ocr = Detector(source), Ocr()
    monkeypatch.setattr(app.panel_detector, "predict", detector.predict)
    monkeypatch.setattr(app, "predict_panel", ocr)
    clouds = []

    def cloud(pixels, **kwargs):
        clouds.append(pixels.copy())
        result = cloud_result()
        result["answer"]["readings"] = [{"text": "12.3456 g", "value": "12.3456", "unit": "g"}]
        return result

    monkeypatch.setattr(aliyun_vision, "read_panel", cloud)
    raw = cv2.imencode(".png", source)[1].tobytes()
    job = app.read_saved_panel(
        app.SavedPhotoRequest(photo=photo(app, raw=raw)), trigger="voice_photo_directory"
    )
    path = app.DATA / "Images" / (job["capture_id"] + ".png")
    original = path.read_bytes()
    with running(app, copy.deepcopy(job)) as future:
        future.result(timeout=4)
    final = app.get_job(job["job_id"])
    assert final["status"] == "completed" and final["photo_orientation"]["degrees_ccw"] == 90
    assert path.read_bytes() == original
    assert detector.calls == [0, 90, 180, 270]
    assert len(clouds) == 1 and np.array_equal(clouds[0], upright[48:82, 18:82])
    assert {r["instrument"]["id"] for r in final["readings"]} == {B}
    assert final["measurement_records"][0]["instrument_id"] == B
    assert final["measurement_records"][0]["record"]["values"][0]["value"] == 12.3456
    region = final["panel_regions"][0]
    saved = app.DATA / "Images" / (region["evidence_id"] + ".png")
    assert region["image_sha256"] == hashlib.sha256(saved.read_bytes()).hexdigest()
    assert np.array_equal(cv2.imread(str(saved)), clouds[0])
    app.archive_store.step(100)
    evidence_path = next(archive[2].glob("*/VoicePhotoReadings/*/Evidence.json"))
    evidence = json.loads(evidence_path.read_text())
    observation = evidence["observations"][0]
    assert observation["photo_orientation"]["degrees_ccw"] == 90
    assert observation["panel_regions"][0]["bbox"] == [40, 20, 70, 80]
    assert evidence["processing_outcome"] == "succeeded"
