"""Inspect upgrades without interrupting live FieldRecognition services.

Live updates are restricted to documentation, tests and this standalone tool.
Runtime changes require services to be already stopped; this tool never stops
them. Use --revision to inspect a tested commit, then --apply to apply it.
Failed offline preparation never starts writers or rolls back business data.
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
DEPLOYMENT_UNITS = ['field-recognition.target', 'field-recognition-prepare.service',
                    'field-recognition-demo.service', *WRITERS]


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


def non_runtime_path(path):
    # Fail closed: static assets, dependencies, configuration, units and all
    # imported Python remain runtime changes, even when no process restart was
    # explicitly requested. Never edit them underneath running processes.
    return (path in {'AGENTS.md', 'README.md', 'AgentTools.md', 'scripts/upgrade_runtime.py'}
            or (path.startswith('docs/') and path.endswith('.md'))
            or path.startswith('tests/'))


def plan(root, revision=None, *, run=command):
    root = Path(root).resolve()
    status = run(root, ['git', 'status', '--porcelain'], capture_output=True).stdout
    if status.strip():
        raise RuntimeError('Commit reviewed source changes before deployment')
    before = run(root, ['git', 'rev-parse', 'HEAD'], capture_output=True).stdout.strip()
    if revision:
        revision = run(root, ['git', 'rev-parse', '--verify', revision + '^{commit}'], capture_output=True).stdout.strip()
        run(root, ['git', 'merge-base', '--is-ancestor', before, revision])
    after = revision or before
    changed = run(root, ['git', 'diff', '--name-only', '--no-renames', '-z', before, after],
                  capture_output=True).stdout.split('\0')
    changed = [path for path in changed if path]
    runtime = [path for path in changed if not non_runtime_path(path)]
    return {'before': before, 'after': after, 'changed_paths': changed, 'runtime_paths': runtime,
            'mode': 'offline_required' if runtime else 'non_runtime' if changed else 'unchanged'}


def ensure_offline(root, *, run=command):
    for unit in DEPLOYMENT_UNITS:
        response = run(root, ['systemctl', '--user', 'show', unit, '-p', 'LoadState',
                             '-p', 'ActiveState', '-p', 'MainPID', '-p', 'ControlPID'],
                       capture_output=True, timeout=10)
        values = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
        idle = values.get('ActiveState') in {'inactive', 'failed'}
        known = values.get('LoadState') in {'loaded', 'not-found'}
        pids = ('MainPID', 'ControlPID') if unit.endswith('.service') and values.get('LoadState') != 'not-found' else ()
        if not known or not idle or any(values.get(key) != '0' for key in pids):
            raise RuntimeError('Live runtime upgrade blocked: ' + unit +
                '. No service was stopped and no code was changed. Keep the tested revision staged; '
                'the current deployment does not support uninterrupted runtime replacement.')


def apply(root, revision=None, *, run=command):
    root = Path(root).resolve()
    report = plan(root, revision, run=run)
    if report['mode'] == 'unchanged':
        return report
    if report['runtime_paths']:
        ensure_offline(root, run=run)
    run(root, ['git', 'merge', '--ff-only', report['after']])
    if report['mode'] == 'non_runtime':
        return report
    env = database_environment(root)
    python = str(root / '.venv/bin/python')
    # Subprocesses import the newly selected version, never this launcher's old modules.
    run(root, [python, '-m', 'runtime_prepare'], env=env)
    run(root, [python, 'scripts/install_runtime.py', '--no-start'], env=env)
    run(root, ['systemctl', '--user', 'reset-failed', *WRITERS, 'field-recognition-prepare.service'])
    run(root, ['systemctl', '--user', 'start', 'field-recognition.target'])
    return report | {'mode': 'offline_applied'}


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
        if args.revision:
            print(json.dumps(plan(root, args.revision), ensure_ascii=False, indent=2))
            return
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
