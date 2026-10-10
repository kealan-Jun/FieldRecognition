"""Bounded real GPU validation with a synthetic panel and temporary database."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


@contextmanager
def validation_runtime(data):
    """Own a local-only runtime even when the caller has production variables."""
    from field_recognition.runtime.factory import create_runtime
    from field_recognition.runtime.production_config import ProductionConfig

    config = ProductionConfig(
        service_role="combined",
        data_dir=str(data),
        database_path=str(Path(data) / "Demo.sqlite3"),
        ocr_device="gpu:0",
        production_enabled=False,
        auth_enabled=False,
        archive_enabled=False,
        saved_photo_watch_enabled=False,
        auto_run_enabled=False,
        video_ocr_enabled=False,
        aliyun_fallback_enabled=False,
    )
    # This optional driver still has a legacy environment adapter. The GPU
    # probe measures OCR only, so it must not load an inherited detector model.
    previous = os.environ.get("FIELD_PANEL_DETECTOR_ENABLED")
    os.environ["FIELD_PANEL_DETECTOR_ENABLED"] = "0"
    runtime = None
    try:
        runtime = create_runtime(config)
        yield runtime
    finally:
        try:
            if runtime is not None:
                runtime.close(wait=True)
        finally:
            if previous is None:
                os.environ.pop("FIELD_PANEL_DETECTOR_ENABLED", None)
            else:
                os.environ["FIELD_PANEL_DETECTOR_ENABLED"] = previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("Verification/GpuOcr"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ["FLAGS_allocator_strategy"] = "auto_growth"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with (
        tempfile.TemporaryDirectory(prefix="field-gpu-verify-") as data,
        validation_runtime(data) as runtime,
    ):
        import cv2
        import numpy as np

        panel = np.full((260, 720, 3), 255, dtype=np.uint8)
        cv2.putText(
            panel, "123.45", (70, 170), cv2.FONT_HERSHEY_SIMPLEX, 3.2, (0, 0, 0), 6, cv2.LINE_AA
        )
        cv2.imwrite(str(args.output / "SyntheticPanel.png"), panel)
        started = time.monotonic()
        model = runtime["load_ocr"]()
        load_seconds = round(time.monotonic() - started, 3)
        results = [runtime["predict_panel"](panel) for _ in range(2)]
        import paddle

        paddle.device.synchronize()
        processes = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,gpu_uuid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            text=True,
        ).splitlines()
        own_gpu = [line for line in processes if line.split(",")[0].strip() == str(os.getpid())]
        passed = (
            paddle.is_compiled_with_cuda()
            and paddle.get_device() == "gpu:0"
            and bool(own_gpu)
            and runtime["load_ocr"]() is model
            and runtime["ocr_state"]["load_count"] == 1
            and all(
                r["status"] == "completed"
                and r["device"] == "gpu:0"
                and any("123.45" in line["numeric_candidates"] for line in r["lines"])
                for r in results
            )
        )
        receipt = {
            "status": "PROVEN" if passed else "NOT_PROVEN",
            "scope": "GPU inference and resident reuse on a synthetic panel only",
            "physical_instrument_accuracy": "NOT_PROVEN",
            "paddle_version": paddle.__version__,
            "cuda_version": paddle.version.cuda(),
            "device": paddle.get_device(),
            "gpu_name": paddle.device.cuda.get_device_name(0),
            "process_gpu_memory": own_gpu,
            "load_seconds": load_seconds,
            "ocr_state": dict(runtime["ocr_state"]),
            "results": results,
        }
        (args.output / "GpuReceipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2)
        )
        print(json.dumps(receipt, ensure_ascii=False))
        if not passed:
            raise SystemExit("GPU validation did not satisfy every gate; inspect the receipt")


if __name__ == "__main__":
    main()
