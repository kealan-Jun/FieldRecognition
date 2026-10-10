"""Legacy access to the existing initialized managed ASGI runtime."""

from field_recognition.runtime.app_production import integrate_production
from field_recognition.runtime.app_production import get_production_core as _get_explicit_core


def get_production_core(runtime=None, **factory_settings):
    if runtime is None and not factory_settings:
        import app

        return integrate_production(app.runtime, app.app)
    return _get_explicit_core(runtime, **factory_settings)
