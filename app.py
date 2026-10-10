"""Legacy ASGI entry point: importing this module deliberately creates a runtime.

Use ``field_recognition.api.application.create_app`` or
``field_recognition.runtime.factory.create_runtime`` for explicit construction.
"""

import sys

from field_recognition.api.application import create_app
from field_recognition.runtime.factory import create_runtime

runtime = create_runtime()
app = create_app(runtime)
runtime.attach_compatibility_module(sys.modules[__name__])
