"""Test suite for phase 2 improvements."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_ocr_worker_import():
    """Test OCR worker can be imported."""
    try:
        import ocr_worker
        print("✓ OCR worker imports successfully")
        return None
    except ImportError as e:
        print(f"✗ OCR worker import failed: {e}")
        raise


def test_handoff_service_cannot_bypass_recipient_scan(tmp_path):
    import pytest
    from handoff_service import HandoffService
    from database import Database
    with pytest.raises(ValueError, match='recipient QR evidence'):
        HandoffService(Database(tmp_path/'unused.sqlite3'))
    assert not (tmp_path/'unused.sqlite3').exists()


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
        return None

    except Exception as e:
        print(f"✗ Capture adapter test failed: {e}")
        raise


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
        return None

    except Exception as e:
        print(f"✗ API v1 test failed: {e}")
        raise


def test_archive_worker_import():
    """Test archive worker can be imported."""
    try:
        import archive_worker
        print("✓ Archive worker imports successfully")
        return None
    except ImportError as e:
        print(f"✗ Archive worker import failed: {e}")
        raise


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
