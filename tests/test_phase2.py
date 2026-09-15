"""Test suite for phase 2 improvements."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_ocr_worker_import():
    """Test OCR worker can be imported."""
    try:
        import ocr_worker
        print("✓ OCR worker imports successfully")
        return True
    except ImportError as e:
        print(f"✗ OCR worker import failed: {e}")
        return False


def test_handoff_service():
    """Test handoff service functionality."""
    try:
        from handoff_service import HandoffService, HandoffState
        from database import Database
        import tempfile
        import os

        # Create temp database
        with tempfile.NamedTemporaryFile(suffix='.sqlite3', delete=False) as f:
            db_path = f.name

        db = Database(db_path)

        # Initialize tables
        with db.transaction() as conn:
            conn.executescript('''
                CREATE TABLE bindings(id TEXT PRIMARY KEY, camera TEXT, ended TEXT, document TEXT);
                CREATE TABLE binding_handoffs(
                    id TEXT PRIMARY KEY, binding_id TEXT, instrument_id TEXT,
                    offered_by TEXT, offered_to TEXT, offered_at TEXT,
                    state TEXT, accepted_at TEXT, rejected_at TEXT,
                    rejection_reason TEXT, status TEXT, document TEXT
                );
            ''')

            # Create test binding
            import json
            binding = {
                'id': 'test-binding',
                'instrument_id': 'inst1',
                'operator': 'user1',
                'camera': 'cam1'
            }
            conn.execute(
                'INSERT INTO bindings VALUES(?,?,?,?)',
                ('test-binding', 'cam1', None, json.dumps(binding))
            )

        # Test handoff service
        service = HandoffService(db)

        # Request handoff
        handoff = service.request_handoff(
            'test-binding', 'inst1', 'user1', 'user2', 'Please take over'
        )

        assert handoff.state == HandoffState.PENDING
        assert handoff.offered_by == 'user1'
        assert handoff.offered_to == 'user2'

        # List pending
        pending = service.list_pending_handoffs('user2')
        assert len(pending) == 1

        db.close()
        os.unlink(db_path)

        print("✓ Handoff service works correctly")
        return True

    except Exception as e:
        print(f"✗ Handoff service test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_capture_adapter():
    """Test capture adapter abstraction."""
    try:
        from capture_adapter import CaptureEvent, CaptureSource, create_adapter
        from datetime import datetime, timezone

        # Create test event
        event = CaptureEvent(
            capture_id='test123',
            camera_id='cam1',
            source=CaptureSource.MANUAL_UPLOAD,
            captured_at=datetime.now(timezone.utc),
            received_at=datetime.now(timezone.utc),
            source_ref='/tmp/test.jpg',
            metadata={'image_data': b'fake_image_data'}
        )

        # Create adapter
        adapter = create_adapter(CaptureSource.MANUAL_UPLOAD)

        # Test should_process
        assert adapter.should_process(event)

        # Test get_image_data
        data = adapter.get_image_data(event)
        assert data == b'fake_image_data'

        print("✓ Capture adapter works correctly")
        return True

    except Exception as e:
        print(f"✗ Capture adapter test failed: {e}")
        return False


def test_api_v1():
    """Test API v1 utilities."""
    try:
        from api_v1 import ErrorCode, APIError, APIResponse, PaginationParams

        # Test error model
        error = APIError(
            error=ErrorCode.NOT_FOUND,
            message="Resource not found",
            request_id="req123"
        )
        assert error.error == ErrorCode.NOT_FOUND

        # Test response model
        response = APIResponse(data={'result': 'ok'}, request_id="req123")
        assert response.success is True

        # Test pagination
        params = PaginationParams(limit=10, offset=0)
        assert params.limit == 10

        print("✓ API v1 utilities work correctly")
        return True

    except Exception as e:
        print(f"✗ API v1 test failed: {e}")
        return False


def test_archive_worker_import():
    """Test archive worker can be imported."""
    try:
        import archive_worker
        print("✓ Archive worker imports successfully")
        return True
    except ImportError as e:
        print(f"✗ Archive worker import failed: {e}")
        return False


if __name__ == '__main__':
    print("=== Phase 2 Components Test ===\n")

    tests = [
        test_ocr_worker_import,
        test_handoff_service,
        test_capture_adapter,
        test_api_v1,
        test_archive_worker_import,
    ]

    results = []
    for test_func in tests:
        print(f"\nRunning {test_func.__name__}...")
        try:
            results.append(test_func())
        except Exception as e:
            print(f"✗ Test crashed: {e}")
            results.append(False)

    print(f"\n{'='*50}")
    passed = sum(results)
    total = len(results)
    print(f"Results: {passed}/{total} tests passed")

    if passed == total:
        print("✓ All phase 2 tests passed")
        sys.exit(0)
    else:
        print("✗ Some tests failed")
        sys.exit(1)
