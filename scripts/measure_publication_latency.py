#!/usr/bin/env python3
"""Isolated three-photo replay and independently parsed Evidence/Result latency.

This script never starts services, edits a database, calls OCR or writes outside
an explicitly initialized empty test root. Configure a test service separately.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import time
import uuid
from zoneinfo import ZoneInfo

MARKER = '.field-recognition-test-only'


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def atomic_bytes(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(root):
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('init requires a new or empty directory')
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(root / MARKER, {'scope': 'test_only', 'created_at': utc_now()})
    for name in ('voice_photos', 'archive', 'reports'):
        (root / name).mkdir()
    return root


def test_root(root):
    root = Path(root).resolve()
    if json.loads((root / MARKER).read_text()).get('scope') != 'test_only':
        raise ValueError('test_only root marker required')
    for name in ('voice_photos', 'archive', 'reports'):
        (root / name).resolve().relative_to(root)
    return root


def inject_group(root, camera_id, images, *, clock=time.monotonic, moment=None):
    root = test_root(root)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', camera_id) or len(images) != 3:
        raise ValueError('one camera and exactly three images required')
    moment = (moment or datetime.now(timezone.utc)).astimezone(ZoneInfo('Asia/Shanghai'))
    directory = root / 'voice_photos' / camera_id / moment.strftime('%Y-%m-%d/%H-%M-%S')
    directory.resolve().relative_to(root)
    if directory.exists():
        raise ValueError('capture directory already exists; use interval >= 1 second')
    group_id = 'latency-' + uuid.uuid4().hex
    payloads, photos = [], []
    for index, source in enumerate(images):
        source = Path(source)
        suffix = source.suffix.lower()
        if suffix not in {'.png', '.jpg', '.jpeg'}:
            raise ValueError('images must be PNG or JPEG')
        raw = source.read_bytes()
        filename = moment.strftime('%Y%m%d_%H%M%S') + f'_{index:03d}' + suffix
        capture_id = str(uuid.uuid4())
        photos.append({'filename': filename, 'capture_id': capture_id,
            'captured_at': moment.isoformat(), 'sha256': hashlib.sha256(raw).hexdigest()})
        payloads.append((filename, raw))
    receipt = {'schema_version': 'field-photo-receipt/1', 'camera_id': camera_id,
        'measurement': {'burst_id': group_id, 'expected_photos': 3,
                        'experiment_context_ref': 'test-only://latency/' + group_id, 'instrument_ids': []},
        'photos': photos}
    atomic_json(directory / 'PhotoReceipt.json', receipt)
    writes = []
    for filename, raw in payloads:
        atomic_bytes(directory / filename, raw)
        writes.append(clock())
    # This is the writer client's completion, not NAS server time or physical capture.
    t0 = clock()
    return {'group_id': group_id, 'camera_id': camera_id, 'expected_tasks': 3,
        'source_capture_ids': [p['capture_id'] for p in photos], 't0_utc': utc_now(),
        't0_monotonic': t0, 'photo_write_completed_monotonic': writes,
        'capture_time_basis': 'test_replay_injection_time_not_original_photo_time',
        'evidence_first_parse_ms': None, 'result_first_parse_ms': None,
        'evidence_terminal_parse_ms': None, 'manifest_verified_ms': None,
        'outcome': 'pending', 'task_outcomes': [], 'result_files': []}


def parse_visible(root, first_reads, *, clock=time.monotonic):
    """Parse physical files only; HTTP Result aliases must not count as readings."""
    evidence, results = [], {}
    for path in (root / 'archive').glob('*/**/*.json'):
        if path.name != 'Evidence.json' and not re.fullmatch(r'Result(?:\d+)?\.json', path.name):
            continue
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, ValueError):
            continue  # A non-atomic backend remains unobserved until parsing succeeds.
        digest = hashlib.sha256(raw).hexdigest()
        stamp = first_reads.setdefault((str(path), digest), clock())
        entry = (path, value, digest, stamp)
        if path.name == 'Evidence.json':
            evidence.append(entry)
        else:
            results[path.relative_to(root / 'archive').as_posix()] = entry
    return evidence, results


def observe(group, visible, *, clock=time.monotonic):
    evidences, results = visible
    for path, value, digest, stamp in evidences:
        context = (value.get('decision') or {}).get('context') or {}
        source_ids = {(source.get('external_photo') or {}).get('capture_id') for source in value.get('sources', [])}
        if value.get('group_id') != group['group_id'] and context.get('burst_id') != group['group_id'] and not source_ids.intersection(group['source_capture_ids']):
            continue
        elapsed = lambda tick: round((tick - group['t0_monotonic']) * 1000, 2)
        if group['evidence_first_parse_ms'] is None:
            group['evidence_first_parse_ms'] = elapsed(stamp)
        group['evidence_path'] = str(path)
        group['task_outcomes'] = value.get('task_outcomes', [])
        manifest = value.get('measurement_files', [])
        parsed = []
        for item in manifest:
            result = results.get(item['result'])
            if not result or result[2] != item.get('sha256'):
                continue
            record = result[1]
            if not isinstance(record, dict) or 'values' not in record:
                continue
            parsed.append({'path': item['result'], 'sha256': result[2], 'first_parse_ms': elapsed(result[3])})
        if parsed and group['result_first_parse_ms'] is None:
            group['result_first_parse_ms'] = min(row['first_parse_ms'] for row in parsed)
        group['result_files'] = parsed
        if value.get('processing_terminal'):
            if group['evidence_terminal_parse_ms'] is None:
                group['evidence_terminal_parse_ms'] = elapsed(stamp)
            if len(parsed) == len(manifest):
                group['manifest_verified_ms'] = elapsed(clock())
                group['outcome'] = value.get('processing_outcome', 'unknown_terminal')
                group['result_status'] = 'parsed' if manifest else 'absent_terminal'
                group['skip_reasons'] = value.get('skip_reasons', [])
    return group


def summarize(groups, *, evidence_target_ms=None, result_target_ms=None):
    def metric(name, target):
        values = [row[name] for row in groups if row.get(name) is not None]
        result = {'sample_count': len(values), 'denominator': 'groups_with_observed_' + name,
            'min_ms': min(values) if values else None, 'max_ms': max(values) if values else None,
            'mean_ms': round(statistics.mean(values), 2) if values else None, 'target_ms': target}
        if target is not None:
            result['above_target_count'] = sum(value > target for value in values)
        return result
    tasks = {item['task_id']: item for group in groups for item in group['task_outcomes']}
    return {'group_count': len(groups), 'group_outcomes': dict(Counter(g['outcome'] for g in groups)),
        'expected_task_count': sum(g['expected_tasks'] for g in groups), 'observed_task_count': len(tasks),
        'task_outcomes': dict(Counter(t['outcome'] for t in tasks.values())),
        'evidence': metric('evidence_first_parse_ms', evidence_target_ms),
        'result': metric('result_first_parse_ms', result_target_ms),
        'terminal_manifest': metric('manifest_verified_ms', None),
        'clock_basis': 'writer_and_reader_same_process_monotonic; UTC timestamps are labels only',
        'measurement_scope': 'three_photo_write_completion_to_first_successful_JSON_parse; not OCR inference time',
        'limitations': 'poll interval and client cache affect visibility; small samples do not establish long-term stability'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init'); init.add_argument('--root', type=Path, required=True)
    run = sub.add_parser('run'); run.add_argument('--root', type=Path, required=True)
    run.add_argument('--camera-id', required=True); run.add_argument('--images', nargs=3, type=Path, required=True)
    run.add_argument('--groups', type=int, default=10); run.add_argument('--interval', type=float, default=60)
    run.add_argument('--timeout', type=float, default=120); run.add_argument('--poll', type=float, default=.2)
    run.add_argument('--evidence-target-ms', type=float); run.add_argument('--result-target-ms', type=float)
    args = parser.parse_args()
    if args.command == 'init':
        print(initialize(args.root)); return
    if args.groups < 1 or args.interval < 1 or args.timeout <= 0 or not .01 <= args.poll <= 60:
        parser.error('groups >= 1, interval >= 1, timeout > 0, 0.01 <= poll <= 60 required')
    root = test_root(args.root)
    report = root / 'reports' / ('latency-' + uuid.uuid4().hex + '.json')
    groups, first_reads = [], {}
    next_injection = time.monotonic()
    while len(groups) < args.groups or any(g['outcome'] == 'pending' for g in groups):
        now = time.monotonic()
        if len(groups) < args.groups and now >= next_injection:
            groups.append(inject_group(root, args.camera_id, args.images))
            next_injection = now + args.interval
        visible = parse_visible(root, first_reads)
        for group in groups:
            if group['outcome'] != 'pending':
                continue
            observe(group, visible)
            if group['outcome'] == 'pending' and time.monotonic() - group['t0_monotonic'] >= args.timeout:
                group.update(outcome='timeout', wait_ms=round((time.monotonic()-group['t0_monotonic'])*1000, 2))
        result = {'schema': 'field-publication-latency/1', 'scope': 'test_only', 'poll_seconds': args.poll,
                  'groups': groups, 'summary': summarize(groups, evidence_target_ms=args.evidence_target_ms,
                                                       result_target_ms=args.result_target_ms)}
        atomic_json(report, result)
        if any(g['outcome'] == 'pending' for g in groups) or len(groups) < args.groups:
            time.sleep(args.poll)
    print(json.dumps({'report': str(report), 'summary': result['summary']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
