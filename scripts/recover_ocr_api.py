"""Retry only the workbench OCR HTTP dependency after a delayed boot mount.

Installed as a root-owned copy; never imports user-writable project modules.
The existing API unit owns its mount dependencies. GPU workers and schedules
are deliberately outside this helper's authority.
"""
import json
from pathlib import Path
import subprocess

UNIT = 'realityloop-ocr-api.service'
PAUSE = Path('/etc/field-recognition/ocr-recovery.paused')


def recover(*, run=subprocess.run, paused=None):
    if PAUSE.exists() if paused is None else paused:
        return {'status': 'paused'}
    enabled = run(['systemctl', 'is-enabled', UNIT], capture_output=True, text=True, timeout=10)
    if enabled.returncode or enabled.stdout.strip() != 'enabled':
        return {'status': 'disabled'}
    state = run(['systemctl', 'show', UNIT, '-p', 'ActiveState', '--value'],
                capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    if state not in {'inactive', 'failed'}:
        return {'status': state}
    # --no-block lets systemd wait for the NAS without hanging the timer.
    # Repeated start requests coalesce with an already pending dependency job.
    run(['systemctl', 'start', '--no-block', UNIT], check=True,
        capture_output=True, text=True, timeout=10)
    return {'status': 'start_requested', 'previous_state': state, 'unit': UNIT}


def main():
    try:
        print(json.dumps(recover()))
    except (OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'retry_next_tick', 'error': type(exc).__name__}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
