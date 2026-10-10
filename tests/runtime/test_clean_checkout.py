"""Exercise the CPU upload profile from a source checkout without host state."""

import json
import os
import shutil
import subprocess
import sys

from field_recognition.runtime.paths import PROJECT_ROOT


def test_sample_registry_references_shipped_labels_with_the_declared_qr_identity():
    import cv2

    from field_recognition.capture.qr_decode import decode_qr

    registry = json.loads((PROJECT_ROOT / "InstrumentRegistry.json").read_text())
    for item in registry["instruments"]:
        assert item["qr_payload"]["id"] == item["instrument_id"]
        for name in item["files"]:
            label = PROJECT_ROOT / "static/labels" / name
            assert label.is_file(), name
            decoded = decode_qr(cv2.imread(str(label)))
            assert item["encoded_text"] in decoded[0]


def test_new_checkout_with_spaces_runs_without_device_models_or_host_configuration(tmp_path):
    checkout = tmp_path / "checkout with spaces"
    checkout.mkdir()
    for name in ("field_recognition", "static"):
        shutil.copytree(
            PROJECT_ROOT / name,
            checkout / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for source in PROJECT_ROOT.glob("*.json"):
        shutil.copy2(source, checkout / source.name)
    home = tmp_path / "new user"
    home.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FIELD_") and key not in {"PYTHONPATH", "DASHSCOPE_API_KEY"}
    }
    environment.update(HOME=str(home), FIELD_OCR_DEVICE="cpu", FIELD_REGISTRY_POLICY="sample")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from field_recognition.runtime.server import app_factory

app = app_factory()
with TestClient(app) as client:
    assert client.get('/').status_code == 200
    assert client.get('/static/app.js').status_code == 200
    state = client.get('/api/state').json()
    assert state['camera']['configured'] is False
    assert state['ocr']['device'] == 'cpu'
    assert state['ocr']['status'] == 'not_loaded'
    assert state['archive']['status'] == 'disabled'
    label = Path('static/labels/InstrumentA.png')
    scan = client.post('/api/scans',
        files={'file': (label.name, label.read_bytes(), 'image/png')},
        data={'camera_id': 'SampleUploadCamera'})
    assert scan.status_code == 200, scan.text
    assert scan.json()['matches'][0]['id'] == json.loads(
        Path('InstrumentRegistry.json').read_text())['instruments'][0]['instrument_id']
    assert 'paddle' not in sys.modules and 'paddleocr' not in sys.modules
    assert app.state.runtime.data_dir == Path('Data').resolve()
print(json.dumps({'upload': 'decoded', 'ocr': 'not_loaded', 'camera': 'unconfigured'}))
""",
        ],
        cwd=checkout,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["upload"] == "decoded"
    assert not list(home.iterdir())
    assert (checkout / "Data/Demo.sqlite3").exists()
