from field_recognition.runtime.paths import PROJECT_ROOT

"""Simplified integration tests for production components."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(PROJECT_ROOT))


def test_imports():
    """Test that all production modules can be imported."""
    try:
        import field_recognition.runtime.database as database
        import field_recognition.runtime.auth as auth
        import field_recognition.runtime.task_queue as task_queue
        import field_recognition.bindings.camera_registry as camera_registry
        import field_recognition.measurements.measurement_state as measurement_state
        import field_recognition.bindings.instrument_config as instrument_config
        import field_recognition.runtime.monitoring as monitoring
        import field_recognition.runtime.production_config as production_config
        import field_recognition.runtime.production_bootstrap as production_bootstrap
        import field_recognition.runtime.legacy_adapter as legacy_adapter

        print("✓ All production modules imported successfully")
        return None
    except ImportError as e:
        print(f"✗ Import failed: {e}")
        raise


def test_database_creation():
    """Test database creation and migration tracking."""
    from field_recognition.runtime.database import Database

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        db = Database(db_path)
        from field_recognition.runtime.database import apply_migrations

        apply_migrations(db)

        with db.connection() as conn:
            # Check WAL mode
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal", f"Expected WAL mode, got {mode}"

        db.close()
        os.unlink(db_path)
        print("✓ Database creation works")
        return None
    except Exception as e:
        print(f"✗ Database creation failed: {e}")
        raise


def test_config_validation():
    """Test production configuration validation."""
    from field_recognition.runtime.production_config import ProductionConfig

    try:
        config = ProductionConfig.from_environment()
        errors = config.validate_production_requirements()

        # In test mode, should not have critical errors
        if config.record_mode == "test":
            print(f"✓ Config validation works (test mode)")
        else:
            print(f"⚠ Config validation found {len(errors)} issues in production mode")

        return None
    except Exception as e:
        print(f"✗ Config validation failed: {e}")
        raise


def test_migration_structure():
    """Test migration structure is valid."""
    from field_recognition.runtime.database import MIGRATIONS

    try:
        assert len(MIGRATIONS) >= 9

        # Check all migrations have required fields
        for m in MIGRATIONS:
            assert "version" in m
            assert "name" in m
            assert "up" in m

        # Check versions are sequential
        versions = [m["version"] for m in MIGRATIONS]
        assert versions == list(range(1, len(MIGRATIONS) + 1)), (
            f"Versions not sequential: {versions}"
        )

        print("✓ Migration structure valid")
        return None
    except Exception as e:
        print(f"✗ Migration structure invalid: {e}")
        raise


def test_python_compilation():
    """Test all Python files compile."""
    import py_compile
    from field_recognition.runtime.paths import source_path

    files = [
        "database.py",
        "auth.py",
        "task_queue.py",
        "camera_registry.py",
        "measurement_state.py",
        "instrument_config.py",
        "monitoring.py",
        "production_config.py",
        "production_bootstrap.py",
        "legacy_adapter.py",
    ]

    failed = []
    for filename in files:
        filepath = source_path(filename)
        if not filepath.exists():
            failed.append(f"{filename} (not found)")
            continue

        try:
            py_compile.compile(str(filepath), doraise=True)
        except py_compile.PyCompileError as e:
            failed.append(f"{filename} ({e})")

    if failed:
        print(f"✗ Compilation failed for: {', '.join(failed)}")
        raise
    else:
        print(f"✓ All {len(files)} production files compile successfully")
        return None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
