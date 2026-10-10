import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from field_recognition.measurements.photo_experiment_context import resolve_context
from field_recognition.runtime.source_io import read, call


class ExperimentWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"FIELD_DEMO_DATA": str(self.root / "Data")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = self.root / "Data/Runtime/PhotoExperimentContexts.json"
        self.config.parent.mkdir(parents=True)
        self.binding = {
            "camera_id": "cam01",
            "start_at": "2026-09-25T17:20:00+08:00",
            "end_at": "2026-09-26T00:00:00+08:00",
            "experiment_context_ref": "experiment://demo/today",
        }
        self.write([self.binding])

    def write(self, bindings):
        self.config.write_text(json.dumps({"version": 1, "bindings": bindings}))

    def photos(self, moment):
        return [{"captured_at": moment}] * 3

    def test_scoped_camera_and_capture_window(self):
        self.assertEqual(
            resolve_context("cam01", self.photos("2026-09-25T17:20:00+08:00")),
            "experiment://demo/today",
        )
        self.assertIsNone(
            resolve_context("another-camera", self.photos("2026-09-25T17:20:00+08:00"))
        )
        self.assertIsNone(resolve_context("cam01", self.photos("2026-09-25T17:19:59+08:00")))
        self.assertIsNone(resolve_context("cam01", self.photos("2026-09-26T00:00:00+08:00")))
        self.assertEqual(
            resolve_context("cam01", self.photos("2026-09-25T09:20:00+00:00")),
            "experiment://demo/today",
        )

    def test_late_retry_uses_capture_time_and_whole_burst_must_fit(self):
        a = self.photos("2026-09-25T17:20:00+08:00")
        self.assertEqual(resolve_context("cam01", a), resolve_context("cam01", a))
        self.assertIsNone(resolve_context("cam01", a + self.photos("2026-09-26T00:00:00+08:00")))

    def test_no_setting_preserves_legacy(self):
        self.config.unlink()
        self.assertIsNone(resolve_context("cam01", self.photos("2026-09-25T17:20:00+08:00")))

    def test_overlap_and_unzoned_settings_rejected(self):
        self.write([self.binding, self.binding | {"experiment_context_ref": "experiment://other"}])
        with self.assertRaises(ValueError):
            resolve_context("cam01", self.photos("2026-09-25T17:20:00+08:00"))
        self.write([self.binding | {"start_at": "2026-09-25T17:20:00"}])
        with self.assertRaises(ValueError):
            resolve_context("cam01", self.photos("2026-09-25T17:20:00+08:00"))

    def test_read_child_process_and_existing_receipt_unchanged(self):
        folder = self.root / "nas/cam01/2026-09-25/17-20-00"
        folder.mkdir(parents=True)
        for suffix in ["", "_001", "_002"]:
            p = folder / ("20260925_172000" + suffix + ".jpg")
            p.write_bytes(b"unchanged photo bytes" + suffix.encode())
            os.utime(p, (1, 1))
        request = {
            "action": "read",
            "root": str(self.root / "nas"),
            "path": str(folder / "20260925_172000.jpg"),
            "camera": "cam01",
            "max_bytes": 4096,
            "zone": "Asia/Shanghai",
            "batch_mode": "directory_triplet",
        }
        result = call(request)
        self.assertEqual(
            result["receipt"]["measurement"]["experiment_context_ref"], "experiment://demo/today"
        )
        self.assertEqual(len(result["receipt"]["photos"]), 3)
        self.assertEqual(result, call(request))
        original = result["receipt"] | {
            "measurement": {
                "burst_id": "upstream",
                "expected_photos": 3,
                "experiment_context_ref": "experiment://upstream",
            }
        }
        (folder / "PhotoReceipt.json").write_text(json.dumps(original))
        self.assertEqual(read(request)["receipt"], original)


if __name__ == "__main__":
    unittest.main()
