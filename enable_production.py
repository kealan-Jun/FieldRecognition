"""Quick integration script to enable production features in existing app.py."""
import os
import sys

# This script can be imported to enable production features
# without modifying the original app.py

def enable_production_mode():
    """Enable production mode for the current process."""
    os.environ['FIELD_PRODUCTION_ENABLED'] = '1'

def patch_app_at_startup():
    """
    Patch to be called at app startup.

    Add this to app.py lifespan:

    @asynccontextmanager
    async def lifespan(application):
        # Existing startup code...

        # Add production integration
        if os.environ.get('FIELD_PRODUCTION_ENABLED') == '1':
            from app_production import integrate_production
            integrate_production(globals(), application)

        yield
        # Existing cleanup code...
    """
    pass

if __name__ == '__main__':
    print("""
To enable production features:

1. Set environment variable:
   export FIELD_PRODUCTION_ENABLED=1

2. Run migrations:
   python migrate_db.py

3. Create admin user (production mode):
   python production_bootstrap.py --create-admin

4. Start service:
   python -m uvicorn app:app --host 127.0.0.1 --port 8188

Production features will be automatically integrated if FIELD_PRODUCTION_ENABLED=1.

To test without production features:
   unset FIELD_PRODUCTION_ENABLED
   python -m uvicorn app:app --host 127.0.0.1 --port 8188
""")
