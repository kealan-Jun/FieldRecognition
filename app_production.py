"""Compatibility entry point; the actual app owns all production services."""

def integrate_production(app_globals, app_instance):
    if app_globals.get('app') is not app_instance or not app_globals.get('RUNTIME_ENABLED'):
        raise RuntimeError('Prepare migrations and start a managed API/worker role; runtime patching is not supported')
    return app_globals


def get_production_core():
    import app
    return integrate_production(vars(app),app.app)
