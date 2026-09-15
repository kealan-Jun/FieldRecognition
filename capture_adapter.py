"""Unified capture event abstraction for different sources."""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class CaptureSource(str, Enum):
    """Capture source types."""
    RECEIVER = 'receiver'  # Live receiver stream
    NAS_PHOTO = 'nas_photo'  # NAS voice photo
    MANUAL_UPLOAD = 'manual_upload'  # Web upload
    AGENT = 'agent'  # Agent photo


class CaptureEvent(BaseModel):
    """Unified capture event from any source."""

    # Identity
    capture_id: str = Field(description="Unique capture identifier")
    camera_id: str = Field(description="Camera that captured this")
    source: CaptureSource = Field(description="Source of this capture")

    # Session context
    session_id: Optional[str] = Field(default=None, description="Device service session")
    burst_id: Optional[str] = Field(default=None, description="Burst/连拍 identifier")

    # Timing
    captured_at: datetime = Field(description="When photo was captured")
    received_at: datetime = Field(description="When system received it")

    # File reference
    source_ref: str = Field(description="Source file path or reference")
    file_size: Optional[int] = Field(default=None, description="File size in bytes")
    content_hash: Optional[str] = Field(default=None, description="SHA256 hash")

    # Completion status
    write_completed: bool = Field(default=True, description="Write transaction completed")
    verified: bool = Field(default=False, description="Content verified")

    # Metadata
    metadata: dict = Field(default_factory=dict, description="Source-specific metadata")


class CaptureAdapter:
    """Base adapter for capture sources."""

    def should_process(self, event: CaptureEvent) -> bool:
        """Check if this event should be processed."""
        return event.write_completed

    def get_image_data(self, event: CaptureEvent) -> bytes:
        """Get image data from source reference."""
        raise NotImplementedError

    def mark_processed(self, event: CaptureEvent):
        """Mark event as processed (optional)."""
        pass


class NASPhotoAdapter(CaptureAdapter):
    """Adapter for NAS voice photos."""

    def __init__(self, nas_root: str):
        self.nas_root = nas_root

    def get_image_data(self, event: CaptureEvent) -> bytes:
        """Read image from NAS path."""
        from pathlib import Path

        # source_ref is relative or absolute path
        path = Path(event.source_ref)
        if not path.is_absolute():
            path = Path(self.nas_root) / path

        if not path.exists():
            raise FileNotFoundError(f"Photo not found: {path}")

        return path.read_bytes()

    def should_process(self, event: CaptureEvent) -> bool:
        """Check if file is stable and complete."""
        if not event.write_completed:
            return False

        from pathlib import Path
        import time

        path = Path(event.source_ref)
        if not path.is_absolute():
            path = Path(self.nas_root) / path

        # Check file hasn't changed recently (stability check)
        if path.exists():
            mtime = path.stat().st_mtime
            age = time.time() - mtime
            return age > 0.5  # 500ms stability

        return False


class ReceiverAdapter(CaptureAdapter):
    """Adapter for receiver stream captures."""

    def __init__(self, receiver_url: str):
        self.receiver_url = receiver_url

    def get_image_data(self, event: CaptureEvent) -> bytes:
        """Image data already in metadata."""
        data = event.metadata.get('image_data')
        if data is None:
            raise ValueError("Receiver capture missing image_data in metadata")
        return data

    def should_process(self, event: CaptureEvent) -> bool:
        """Receiver frames are immediately ready."""
        return True


class ManualUploadAdapter(CaptureAdapter):
    """Adapter for web uploads."""

    def get_image_data(self, event: CaptureEvent) -> bytes:
        """Image data in metadata or read from temp file."""
        data = event.metadata.get('image_data')
        if data:
            return data

        # Fall back to file reference
        from pathlib import Path
        path = Path(event.source_ref)
        return path.read_bytes()


def create_adapter(source: CaptureSource, **kwargs) -> CaptureAdapter:
    """Factory to create appropriate adapter."""
    if source == CaptureSource.NAS_PHOTO:
        return NASPhotoAdapter(kwargs.get('nas_root'))
    elif source == CaptureSource.RECEIVER:
        return ReceiverAdapter(kwargs.get('receiver_url'))
    elif source == CaptureSource.MANUAL_UPLOAD:
        return ManualUploadAdapter()
    elif source == CaptureSource.AGENT:
        return ManualUploadAdapter()  # Same as manual upload
    else:
        raise ValueError(f"Unknown source: {source}")
