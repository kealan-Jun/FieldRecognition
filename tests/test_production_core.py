"""Tests for production core components."""
import os
import sys
import tempfile
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from database import Database, apply_migrations
from auth import AuthService
from task_queue import TaskQueue
from camera_registry import CameraRegistry
from measurement_state import MeasurementStateMachine
from instrument_config import InstrumentConfig


@pytest.fixture
def test_db():
    """Create temporary test database with fresh schema."""
    with tempfile.NamedTemporaryFile(suffix='.sqlite3', delete=False) as f:
        db_path = f.name

    # Initialize empty database
    db = Database(db_path)

    # Apply all migrations to fresh database
    apply_migrations(db)

    yield db

    db.close()
    try:
        os.unlink(db_path)
    except:
        pass


def test_database_migrations(test_db):
    """Test database migrations apply successfully."""
    with test_db.connection() as conn:
        # Check tables exist
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]

        assert 'users' in tables
        assert 'sessions' in tables
        assert 'camera_registry' in tables
        assert 'instrument_types' in tables
        assert 'measurement_confirmations' in tables


def test_auth_create_admin(test_db):
    """Test creating initial admin user."""
    auth = AuthService(test_db)

    user = auth.create_initial_admin('admin', 'Administrator', 'password123')

    assert user.username == 'admin'
    assert user.role == 'admin'
    assert not user.disabled

    # Cannot create second admin when users exist
    with pytest.raises(Exception):
        auth.create_initial_admin('admin2', 'Admin2', 'pass')


def test_auth_login(test_db):
    """Test user login and session creation."""
    auth = AuthService(test_db)
    auth.create_initial_admin('testuser', 'Test User', 'testpass')

    session, user = auth.login('testuser', 'testpass')

    assert session.user_id == user.id
    assert user.username == 'testuser'

    # Verify session
    verified_user = auth.verify_session(session.id)
    assert verified_user.id == user.id


def test_auth_wrong_password(test_db):
    """Test login with wrong password fails."""
    auth = AuthService(test_db)
    auth.create_initial_admin('testuser', 'Test User', 'testpass')

    with pytest.raises(Exception):
        auth.login('testuser', 'wrongpass')


def test_task_queue_claim_and_complete(test_db):
    """Test task queue claim and complete."""
    queue = TaskQueue(test_db, 'worker-1')

    # Insert a test task
    import json
    import uuid
    from datetime import datetime, timezone

    task_id = str(uuid.uuid4())
    task_doc = {
        'job_id': task_id,
        'task_type': 'ocr',
        'created_at': datetime.now(timezone.utc).isoformat()
    }

    with test_db.transaction() as conn:
        conn.execute(
            'INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',
            (task_id, 'queued', json.dumps(task_doc), None, None, 0, 3)
        )

    # Claim task
    claimed = queue.claim_task()
    assert claimed is not None
    assert claimed['job_id'] == task_id
    assert claimed['lease_holder'] == 'worker-1'

    # Complete task
    queue.complete_task(task_id, {'result': 'success'})

    # Verify completed
    with test_db.connection() as conn:
        row = conn.execute('SELECT status FROM jobs WHERE id=?', (task_id,)).fetchone()
        assert row['status'] == 'completed'


def test_task_queue_lease_expiry(test_db):
    """Test expired lease recovery."""
    import json
    import uuid
    from datetime import datetime, timezone, timedelta

    task_id = str(uuid.uuid4())
    task_doc = {'job_id': task_id, 'task_type': 'ocr'}

    # Insert task with expired lease
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()

    with test_db.transaction() as conn:
        conn.execute(
            'INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',
            (task_id, 'running', json.dumps(task_doc), 'dead-worker', past_time, 0, 3)
        )

    # New worker can claim expired lease
    queue = TaskQueue(test_db, 'worker-2')
    claimed = queue.claim_task()

    assert claimed is not None
    assert claimed['job_id'] == task_id
    assert claimed['lease_holder'] == 'worker-2'


def test_camera_registry(test_db):
    """Test camera registry."""
    registry = CameraRegistry(test_db)

    # Register camera
    camera = registry.register_camera(
        camera_id='cam001',
        display_name='Test Camera',
        receiver_url='http://localhost:8080'
    )

    assert camera.camera_id == 'cam001'
    assert camera.enabled

    # Get camera
    context = registry.get_camera('cam001')
    assert context is not None
    assert context.camera_id == 'cam001'

    # List cameras
    cameras = registry.list_cameras()
    assert len(cameras) == 1


def test_measurement_state_machine(test_db):
    """Test measurement state transitions."""
    import uuid

    sm = MeasurementStateMachine(test_db)

    # Create measurement in draft state
    measurement_id = str(uuid.uuid4())
    instrument_uuid = str(uuid.uuid4())

    measurement = sm.create_measurement(
        measurement_id=measurement_id,
        burst_id=None,
        camera_id='cam001',
        operator='test_user',
        job_ids=['job1'],
        context={'expected_photos': 1}
    )

    assert measurement['state'] == 'draft'

    # Add fields first
    from measurement_state import FieldValue
    fields = [FieldValue(
        field_id='f1',
        instrument_id=instrument_uuid,
        name='温度',
        value=25.5,
        unit='°C'
    )]

    measurement = sm.update_fields(measurement_id, fields, 'test_user', 'Initial reading')

    # Submit for confirmation
    measurement = sm.submit_for_confirmation(measurement_id, 'test_user')
    assert measurement['state'] == 'pending_confirmation'

    # Confirm measurement
    measurement, record_id = sm.confirm_measurement(
        measurement_id, 'reviewer', measurement['revision']
    )

    assert measurement['state'] == 'confirmed'
    assert record_id is not None


def test_instrument_config(test_db):
    """Test instrument configuration."""
    from instrument_config import MeasurementDefinition

    config = InstrumentConfig(test_db)

    # Built-in measurements should exist
    temp_def = config.get_measurement_definition('温度')
    assert temp_def is not None
    assert temp_def.unit == '°C'

    # List instrument types
    types = config.list_instrument_types()
    assert len(types) >= 2  # Should have built-in types


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
