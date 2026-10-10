"""Compatibility entry point for field_recognition.runtime.migrate_db."""

import importlib
import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("field_recognition.runtime.migrate_db", run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module("field_recognition.runtime.migrate_db")
