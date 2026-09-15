"""Production-ready app.py integration patch.

This module patches the existing app.py to integrate production components
while maintaining backward compatibility.
"""
import os
import sys
from pathlib import Path

# Initialize production core if enabled
PRODUCTION_ENABLED = os.environ.get('FIELD_PRODUCTION_ENABLED', '0') == '1'

if PRODUCTION_ENABLED:
    from production_bootstrap import create_production_core
    from legacy_adapter import integrate_with_legacy_app
    from monitoring import setup_monitoring_routes

    # Create production core with validation
    production_core = create_production_core(validate=True)

    def integrate_production(app_globals, app_instance):
        """
        Integrate production components into app globals.

        Args:
            app_globals: Dictionary of app global variables (globals())
            app_instance: FastAPI app instance
        """
        # Integrate with legacy app
        adapter = integrate_with_legacy_app(app_globals, production_core)

        # Initialize monitoring with core dict
        core_dict = {
            'db': app_globals.get('db'),
            'ocr_state': app_globals.get('ocr_state', {}),
            'archive_store': app_globals.get('archive_store'),
            'receiver_camera': app_globals.get('receiver_camera')
        }
        production_core.init_monitoring(core_dict)

        # Setup monitoring routes
        setup_monitoring_routes(app_instance, production_core.monitoring)

        return adapter

else:
    # Production not enabled, provide no-op functions
    production_core = None

    def integrate_production(app_globals, app_instance):
        """No-op when production not enabled."""
        return None


def get_production_core():
    """Get production core instance (None if not enabled)."""
    return production_core
