"""Explicit component configuration changes for an already-created test runtime."""

from field_recognition.runtime.production_config import ProductionConfig


COMPONENT_FIELDS = frozenset(
    {
        "archive_enabled",
        "archive_root",
        "archive_mount",
        "archive_check_seconds",
        "saved_photo_watch_enabled",
        "saved_photo_root",
        "saved_photo_timezone",
        "camera_id",
        "auto_run_enabled",
        "video_ocr_enabled",
    }
)


def refresh_runtime_config(app, *fields):
    """Copy selected environment inputs into this fixture's typed configuration.

    Runtime components keep their explicit settings. Tests that change an input
    after creating ``app`` opt into this refresh without replacing its database,
    service role, camera endpoint, or recognition configuration.
    """
    if not fields or set(fields) - COMPONENT_FIELDS:
        raise ValueError("Select the component configuration fields to refresh")
    environment = ProductionConfig.from_environment()
    runtime = app.runtime
    runtime["config"] = runtime.config.model_copy(
        update={field: getattr(environment, field) for field in fields}
    )
    return runtime.config
