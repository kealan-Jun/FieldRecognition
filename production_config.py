"""Production deployment configuration and validation."""
import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ProductionConfig(BaseModel):
    """Production deployment configuration with validation."""

    # Core settings
    record_mode: Literal['test', 'production'] = Field(default='test')
    database_path: str = Field(default='Data/Demo.sqlite3')

    # Authentication (required in production)
    auth_enabled: bool = Field(default=False)
    require_device_credentials: bool = Field(default=False)
    session_duration_hours: int = Field(default=8, ge=1, le=72)

    # OCR settings
    ocr_device: str = Field(default='cpu')
    ocr_device_verified: bool = Field(default=False)

    # Camera settings
    receiver_url: str | None = None
    camera_id: str | None = None
    camera_snapshot_url: str | None = None

    # Photo watch
    saved_photo_root: str | None = None
    saved_photo_watch_enabled: bool = Field(default=False)
    saved_photo_timezone: str = Field(default='Asia/Shanghai')

    # Archive
    archive_enabled: bool = Field(default=False)
    archive_root: str | None = None
    archive_mount: str | None = None
    archive_check_seconds: int = Field(default=86400, ge=3600)

    # Video OCR
    video_ocr_enabled: bool = Field(default=False)

    # Aliyun fallback
    aliyun_fallback_enabled: bool = Field(default=False)
    dashscope_api_key: str | None = None
    aliyun_base_url: str = Field(default='https://dashscope.aliyuncs.com/compatible-mode/v1')
    aliyun_model: str = Field(default='qwen3.8-max')
    aliyun_no_digits_seconds: int = Field(default=5, ge=1, le=60)

    # Auto run
    auto_run_enabled: bool = Field(default=False)

    # Server
    host: str = Field(default='127.0.0.1')
    port: int = Field(default=8188, ge=1024, le=65535)

    @field_validator('record_mode')
    @classmethod
    def validate_record_mode(cls, v):
        """Validate record mode."""
        if v not in ('test', 'production'):
            raise ValueError('record_mode must be test or production')
        return v

    @field_validator('archive_root')
    @classmethod
    def validate_archive_root(cls, v, info):
        """Validate archive root exists if archive enabled."""
        if info.data.get('archive_enabled') and not v:
            raise ValueError('archive_root required when archive_enabled=true')
        return v

    def validate_production_requirements(self) -> list[str]:
        """
        Validate production deployment requirements.

        Returns:
            List of validation errors (empty if valid)
        """
        errors = []

        if self.record_mode != 'production':
            return errors  # Only validate production mode

        # Authentication must be enabled in production
        if not self.auth_enabled:
            errors.append('auth_enabled must be true in production mode')

        # Archive should be enabled in production
        if not self.archive_enabled:
            errors.append('Warning: archive_enabled should be true in production')

        # Device credentials should be required
        if not self.require_device_credentials:
            errors.append('Warning: require_device_credentials should be true in production')

        # Photo watch recommended
        if not self.saved_photo_watch_enabled and not self.receiver_url:
            errors.append('Warning: No photo source configured (neither photo_watch nor receiver)')

        # Archive path validation
        if self.archive_enabled:
            if not self.archive_root:
                errors.append('archive_root must be set when archive_enabled=true')
            elif not Path(self.archive_root).exists():
                errors.append(f'archive_root does not exist: {self.archive_root}')

        # NAS photo path validation
        if self.saved_photo_watch_enabled:
            if not self.saved_photo_root:
                errors.append('saved_photo_root must be set when saved_photo_watch_enabled=true')
            elif not Path(self.saved_photo_root).exists():
                errors.append(f'saved_photo_root does not exist: {self.saved_photo_root}')

        return errors

    @classmethod
    def from_environment(cls) -> 'ProductionConfig':
        """Load configuration from environment variables."""
        return cls(
            record_mode=os.environ.get('FIELD_RECORD_MODE', 'test'),
            database_path=os.environ.get('FIELD_DEMO_DATA', 'Data/Demo.sqlite3'),
            auth_enabled=os.environ.get('FIELD_AUTH_ENABLED', '0') == '1',
            require_device_credentials=os.environ.get('FIELD_REQUIRE_DEVICE_CREDENTIALS', '0') == '1',
            ocr_device=os.environ.get('FIELD_OCR_DEVICE', 'cpu'),
            receiver_url=os.environ.get('FIELD_RECEIVER_URL'),
            camera_id=os.environ.get('FIELD_CAMERA_ID'),
            camera_snapshot_url=os.environ.get('FIELD_CAMERA_SNAPSHOT_URL'),
            saved_photo_root=os.environ.get('FIELD_SAVED_PHOTO_ROOT'),
            saved_photo_watch_enabled=os.environ.get('FIELD_SAVED_PHOTO_WATCH_ENABLED', '0') == '1',
            saved_photo_timezone=os.environ.get('FIELD_SAVED_PHOTO_TIMEZONE', 'Asia/Shanghai'),
            archive_enabled=os.environ.get('FIELD_ARCHIVE_ENABLED', '0') == '1',
            archive_root=os.environ.get('FIELD_ARCHIVE_ROOT'),
            archive_mount=os.environ.get('FIELD_ARCHIVE_MOUNT'),
            archive_check_seconds=int(os.environ.get('FIELD_ARCHIVE_CHECK_SECONDS', '86400')),
            video_ocr_enabled=os.environ.get('FIELD_VIDEO_OCR_ENABLED', '0') == '1',
            aliyun_fallback_enabled=os.environ.get('FIELD_ALIYUN_FALLBACK_ENABLED', '0') == '1',
            dashscope_api_key=os.environ.get('DASHSCOPE_API_KEY'),
            aliyun_base_url=os.environ.get('FIELD_ALIYUN_BASE_URL',
                                          'https://dashscope.aliyuncs.com/compatible-mode/v1'),
            aliyun_model=os.environ.get('FIELD_ALIYUN_MODEL', 'qwen3.8-max'),
            aliyun_no_digits_seconds=int(os.environ.get('FIELD_ALIYUN_NO_DIGITS_SECONDS', '5')),
            auto_run_enabled=os.environ.get('FIELD_AUTO_RUN_ENABLED', '0') == '1',
            host=os.environ.get('FIELD_HOST', '127.0.0.1'),
            port=int(os.environ.get('FIELD_PORT', '8188'))
        )


def validate_production_deployment() -> bool:
    """
    Validate production deployment configuration.

    Returns:
        True if valid, False otherwise (exits on critical errors)
    """
    try:
        config = ProductionConfig.from_environment()
    except Exception as e:
        print(f"❌ Configuration validation failed: {e}", file=sys.stderr)
        return False

    errors = config.validate_production_requirements()

    if not errors:
        print("✓ Production configuration validated")
        return True

    print("Production configuration issues:", file=sys.stderr)
    critical_errors = [e for e in errors if not e.startswith('Warning:')]
    warnings = [e for e in errors if e.startswith('Warning:')]

    if critical_errors:
        print("\n❌ Critical errors:", file=sys.stderr)
        for error in critical_errors:
            print(f"  - {error}", file=sys.stderr)

    if warnings:
        print("\n⚠️  Warnings:", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)

    if critical_errors and config.record_mode == 'production':
        print("\n❌ Cannot start in production mode with critical errors", file=sys.stderr)
        return False

    return True


def check_database_migrations(db_path: str) -> bool:
    """
    Check if database migrations are needed.

    Args:
        db_path: Path to database file

    Returns:
        True if migrations up to date, False if migrations needed
    """
    from database import Database, get_migration_status

    db = Database(db_path)
    status = get_migration_status(db)

    if status['pending']:
        print(f"⚠️  {len(status['pending'])} pending database migrations:")
        for migration in status['pending']:
            print(f"  - v{migration['version']}: {migration['name']}")
        print("\nRun migrations with: python -m database migrate")
        return False

    print(f"✓ Database migrations up to date (v{status['current_version']})")
    return True


if __name__ == '__main__':
    """Run configuration validation."""
    import argparse

    parser = argparse.ArgumentParser(description='Validate production deployment configuration')
    parser.add_argument('--check-migrations', action='store_true',
                       help='Also check database migration status')
    args = parser.parse_args()

    valid = validate_production_deployment()

    if args.check_migrations:
        config = ProductionConfig.from_environment()
        db_ok = check_database_migrations(config.database_path)
        valid = valid and db_ok

    sys.exit(0 if valid else 1)
