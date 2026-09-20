"""Stop writers before advancing code, rehearse migrations, then start and verify.

Run --apply --revision <tested-local-commit>. The default only inspects.
Failed preparation keeps writers stopped and preserves the database backup;
it never rolls a database back over newly created business records.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.request

WRITERS = ['field-recognition-' + name + '.service' for name in ('api', 'ocr', 'cameras', 'archive')]


def command(root, args, **kwargs):
    return subprocess.run(args, cwd=root, check=True, text=True, **kwargs)


def database_environment(root):
    env = os.environ.copy()
    for path in (root / 'Data/Runtime/ServiceEnvironment.env', root / '.env'):
        if path.exists():
            for line in path.read_text().splitlines():
                key, sep, value = line.partition('=')
                if sep and key.strip() in {'FIELD_DEMO_DATA', 'FIELD_DATABASE_PATH'}:
                    values = shlex.split(value, comments=False)
                    if len(values) != 1:
                        raise ValueError('Use one explicit database path in ' + str(path))
                    env[key.strip()] = values[0]
    return env


def apply(root, revision=None, *, run=command):
    root = Path(root).resolve()
    env = database_environment(root)
    python = str(root / '.venv/bin/python')
    status = run(root, ['git', 'status', '--porcelain'], capture_output=True).stdout
    if status.strip():
        raise RuntimeError('Commit reviewed source changes before deployment')
    before = run(root, ['git', 'rev-parse', 'HEAD'], capture_output=True).stdout.strip()
    if revision:
        revision = run(root, ['git', 'rev-parse', '--verify', revision + '^{commit}'], capture_output=True).stdout.strip()
        run(root, ['git', 'merge-base', '--is-ancestor', before, revision])
    run(root, ['systemctl', '--user', 'stop', 'field-recognition.target', *WRITERS])
    load = run(root, ['systemctl', '--user', 'show', 'field-recognition-prepare.service',
                      '-p', 'LoadState', '--value'], capture_output=True).stdout.strip()
    if load != 'not-found':
        run(root, ['systemctl', '--user', 'stop', 'field-recognition-prepare.service'])
    for unit in WRITERS:
        pid = run(root, ['systemctl', '--user', 'show', unit, '-p', 'MainPID', '--value'],
                  capture_output=True).stdout.strip()
        if pid != '0':
            raise RuntimeError('Writer has not stopped: ' + unit)
    if revision:
        run(root, ['git', 'merge', '--ff-only', revision])
    # Subprocesses import the newly selected version, never this launcher's old modules.
    run(root, [python, '-m', 'runtime_prepare'], env=env)
    run(root, [python, 'scripts/install_runtime.py', '--no-start'], env=env)
    run(root, ['systemctl', '--user', 'reset-failed', *WRITERS, 'field-recognition-prepare.service'])
    run(root, ['systemctl', '--user', 'start', 'field-recognition.target'])
    return {'before': before, 'after': revision or before}


def verify(url, timeout=60):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(url.rstrip('/') + '/health/ready', timeout=3) as response:
                result = json.load(response)
            if result.get('status') in {'ready', 'degraded'}:
                return result
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError('Service startup check failed; inspect the managed unit logs')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--revision', help='Tested, committed local revision; fast-forward only')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--url', default='http://127.0.0.1:8188')
    args = parser.parse_args()
    root = args.project_root.resolve()
    if not args.apply:
        command(root, [str(root / '.venv/bin/python'), '-m', 'runtime_prepare', '--check'],
                env=database_environment(root))
        return
    report = apply(root, args.revision)
    report['health'] = verify(args.url)
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    data = Path(database_environment(root).get('FIELD_DEMO_DATA', root / 'Data'))
    if not data.is_absolute():
        data = root / data
    receipt = data / 'Runtime' / ('Upgrade-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '.json')
    with open(receipt, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps(report | {'receipt': str(receipt)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
