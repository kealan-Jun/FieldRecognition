"""Controlled stage gates; no physical camera, NAS or model invocation."""

import copy
from concurrent.futures import Future
from types import SimpleNamespace

import numpy as np
import pytest

import field_recognition.recognition.panel_readout as panel_readout
import field_recognition.recognition.panel_regions as panel_regions
from tests.support.demo import app_client  # noqa: F401
from tests.support.panel_readout import job_context, Clock, local_result  # noqa: F401


@pytest.mark.parametrize("remote", [False, True])
def test_model_timing_separates_lock_preparation_and_lazy_prediction(
    app_client, monkeypatch, remote
):
    app, _ = app_client
    clock = Clock()

    class Lock:
        def __enter__(self):
            clock.value += 0.2

        def __exit__(self, *_):
            pass

    class Model:
        def predict(self, _):
            clock.value += 0.4
            yield {"rec_texts": [], "rec_scores": [], "rec_polys": []}

    model = Model()
    if remote:
        model.last_receipt = {"execution": {"actual_model_invocation": True}}

    def load():
        clock.value += 0.3
        return model

    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(app, "ocr_lock", Lock())
    monkeypatch.setattr(app, "load_ocr", load)
    result = app.predict_panel(np.zeros((20, 30, 3), np.uint8))
    assert result["status"] == "completed"
    assert result["ocr_model_lock_wait_ms"] == 200
    assert result["ocr_model_prepare_ms"] == 300
    assert result["ocr_model_call_ms"] == 400
    assert result["ocr_model_call_count"] == 1
    assert result["wall_seconds"] == 0.9
    assert result["ocr_model_call_scope"] == (
        "adapter_call_including_remote_queue_and_transport"
        if remote
        else "local_model_predict_iteration"
    )


def test_refinement_calls_are_summed_once_without_counting_nested_receipt_copies(monkeypatch):
    instrument = "test-instrument"
    detector = SimpleNamespace(
        enabled=lambda: True,
        predict=lambda *args, **kwargs: {
            "status": "completed",
            "boxes": [
                {
                    "class_id": 0,
                    "instrument_id": instrument,
                    "confidence": 0.9,
                    "xyxy": [10, 10, 50, 40],
                }
            ],
        },
    )

    def predict(*args):
        return {
            "status": "completed",
            "lines": [],
            "actual_model_invocation": True,
            "ocr_model_lock_wait_ms": 10,
            "ocr_model_prepare_ms": 20,
            "ocr_model_call_ms": 30,
            "ocr_model_call_count": 1,
            "ocr_model_call_scope": "local_model_predict_iteration",
        }

    def refine(predictor, image, initial, *args, **kwargs):
        second, third = predictor(image), predictor(image)
        return dict(
            initial, panel_ocr=copy.deepcopy(initial), digit_ocr=second, digit_ocr_check=third
        )

    monkeypatch.setattr(panel_regions, "refine_digits", refine)
    import field_recognition.recognition.led_digits as led_digits

    monkeypatch.setattr(led_digits, "verify_digits", lambda panel, local, *args: local)
    core = {
        "panel_detector": detector,
        "predict_panel": predict,
        "now": lambda: "2026-10-08T01:00:00+00:00",
        "OCR_DEVICE": "cpu",
        "ocr_state": {"engine": "test"},
    }
    result = panel_regions.predict(
        core, np.zeros((60, 80, 3), np.uint8), allowed_instrument_ids=[instrument]
    )
    assert result["ocr_model_call_count"] == 3
    assert result["ocr_model_lock_wait_ms"] == 30
    assert result["ocr_model_prepare_ms"] == 60
    assert result["ocr_model_call_ms"] == 90
    assert result["ocr_model_call_scopes"] == ["local_model_predict_iteration"]


def test_single_worker_queue_wait_is_distinct_from_predictor_execution(job_context, monkeypatch):
    app, _, job, _ = job_context
    clock = Clock()
    future = Future()
    queued = []

    class Executor:
        def submit(self, function, *args):
            queued.append((function, args))
            return future

    def predict(*args, **kwargs):
        clock.value += 0.25
        return local_result("12.34 g")

    def release(value):
        if value >= 2 and not future.done():
            function, args = queued.pop()
            future.set_result(function(*args))

    clock.on_pause = release
    monkeypatch.setattr(app, "predict_readout", predict)
    panel_readout.run(vars(app) | {"ocr_pool": Executor()}, job, clock=clock, pause=clock.pause)
    result = app.get_job(job["job_id"])
    assert result["status"] == "completed" and result["lines"][0]["text"] == "12.34 g"
    assert result["timing"]["durations_ms"]["ocr_scheduler_wait_ms"] == 2000
    assert result["timing"]["durations_ms"]["ocr_execution_ms"] == 250
    assert result["timing"]["duration_clock_basis"]["ocr_scheduler_wait_ms"] == "process_monotonic"
    assert "fallback" not in result


@pytest.mark.parametrize("actual", [False, True])
def test_remote_execution_receipt_is_authoritative(app_client, monkeypatch, actual):
    app, _ = app_client

    class ReceiptModel:
        last_receipt = {"execution": {"actual_model_invocation": actual}}

        def predict(self, panel):
            return []

    app.ocr_model = None
    monkeypatch.setattr(app, "create_ocr_model", ReceiptModel)
    result = app.predict_panel(np.zeros((32, 32, 3), dtype=np.uint8))
    assert result["status"] == "completed"
    assert result["actual_model_invocation"] is actual
    assert result["remote_inference"]["execution"]["actual_model_invocation"] is actual
