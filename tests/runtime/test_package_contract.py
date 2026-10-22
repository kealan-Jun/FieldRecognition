"""Architecture checks target runtime/resource behavior rather than file counts."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from field_recognition.runtime.paths import PROJECT_ROOT, source_path


def test_canonical_imports_do_not_create_data_or_start_services(tmp_path):
    unused = tmp_path / "MustNotBeCreated"
    env = {key: value for key, value in os.environ.items() if not key.startswith("FIELD_")}
    env.update(PYTHONPATH=str(PROJECT_ROOT), FIELD_DEMO_DATA=str(unused))
    script = """
import importlib, json, pkgutil, threading
import field_recognition
before = {thread.ident for thread in threading.enumerate()}
modules = [item.name for item in pkgutil.walk_packages(field_recognition.__path__, field_recognition.__name__ + '.')]
for module in modules:
    importlib.import_module(module)
after = [thread.name for thread in threading.enumerate() if thread.ident not in before]
print(json.dumps({'modules': len(modules), 'threads': after}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    report = json.loads(result.stdout)
    assert report["modules"] >= 80
    assert report["threads"] == []
    assert not unused.exists()
    assert not (tmp_path / "Data").exists()


def test_new_fingerprints_use_actual_implementation_and_keep_old_receipt_keys():
    from field_recognition.measurements.photo_measurements import versions

    result = versions()["code_sha256"]
    for legacy in ("photo_measurements.py", "instrument_ownership.py", "reading_results.py"):
        assert result[legacy] == hashlib.sha256(source_path(legacy).read_bytes()).hexdigest()
        assert "field_recognition" in source_path(legacy).parts
    for actual in (
        "field_recognition/measurements/readout_rules.py",
        "field_recognition/recognition/config.py",
        "field_recognition/recognition/panel_readout.py",
        "field_recognition/recognition/parallel_photo.py",
        "field_recognition/recognition/photo_orientation.py",
        "field_recognition/recognition/red_geometry.py",
        "field_recognition/recognition/pipeline_context.py",
        "field_recognition/measurements/measurement_actions.py",
        "field_recognition/runtime/context.py",
        "field_recognition/runtime/runtime_rpc.py",
    ):
        assert result[actual] == hashlib.sha256((PROJECT_ROOT / actual).read_bytes()).hexdigest()


def test_archived_page_has_complete_inline_scripts_and_no_server_dependency():
    from field_recognition.archive.archive_catalog import render_index

    rendered = render_index({"schema": "field-recognition-index/2", "items": []}).decode()
    assert "/*VIEW_UTILS*/" not in rendered
    assert "/*ARCHIVE_SCRIPT*/" not in rendered
    assert "/*ARCHIVE_DATA*/" not in rendered
    assert "ViewUtils" in rendered
    assert '<script src="' not in rendered
