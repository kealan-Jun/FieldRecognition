"""Compatibility entry point for field_recognition.runtime.prepare_runtime."""

import importlib
import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("field_recognition.runtime.prepare_runtime", run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module("field_recognition.runtime.prepare_runtime")
