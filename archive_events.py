"""Date/camera event bundles. A burst, retries and confirmation share one result."""
import copy
import hashlib
import html
import json
import posixpath
import time
from datetime import datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from archive_browse import page, token
from archive_readable import raw_json
from measurement_records import build_records, capture_clock, measurement_value, RULE_VERSION

KINDS = {'Bindings': '二维码与绑定', 'VideoReadings': '视频面板读数',
         'VoicePhotoReadings': '语音照片读数', 'PhotoReadings': '上传照片读数'}


def processing_outcome(job):
    """Recognition completion is independent of later human confirmation."""
    status = job.get('status')
    terminal = status in {'completed', 'failed', 'cancelled', 'interrupted'}
    if job.get('resume_pending') or job.get('phase') in {'retry_wait', 'replayed', 'waiting_service'}:
        terminal = False
    skipped = job.get('recognition_skipped') or (job.get('local_ocr') or {}).get('recognition_skipped')
    return {'task_id': job.get('job_id'), 'capture_id': job.get('capture_id'), 'status': status,
            'terminal': terminal, 'outcome': ('failed' if status != 'completed' else 'skipped' if skipped else 'succeeded') if terminal else 'pending',
            'reason': job.get('skip_reason') or (job.get('local_ocr') or {}).get('skip_reason') or job.get('error')}


def aware(value):
    try:
        moment = datetime.fromisoformat(value)
        return moment.astimezone(ZoneInfo('Asia/Shanghai')) if moment.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def latest_rows(rows):
    latest, versions = {}, {}
    for row in sorted(rows, key=lambda r: r['seq']):
        key = (row['entity'], row['entity_id'])
        current = dict(row)
        for field in ('published_at', 'readable_at', 'readability_basis'):
            current.setdefault(field, None)
        latest[key] = current | {'doc': json.loads(row['document'])}
        versions.setdefault(key, []).append({'sequence': row['seq'], 'receipt': row['receipt_path'],
            'queued_at': row['recorded_at'], 'published_at': current['published_at'],
            'readable_at': current['readable_at'], 'readability_basis': current['readability_basis']})
    return latest, versions


def clock_for(doc, capture):
    value, basis = capture_clock(doc)
    if not aware(value):
        value, basis = capture_clock(capture)
    return (value, basis) if aware(value) else (None, None)


def prepare(rows, paths):
    latest, versions = latest_rows(rows)
    captures = {i: r['doc'] for (e, i), r in latest.items() if e == 'scans'}
    jobs = {i: r for (e, i), r in latest.items() if e == 'jobs'}
    groups = {r['doc']['measurement_id']: r for (e, _), r in latest.items() if e == 'photo_measurements'}
    # A confirmed record supersedes its draft view; both immutable histories remain linked.
    for (entity, _), row in latest.items():
        if entity == 'experiment_records':
            mid = row['doc']['measurement_id']
            current = groups.get(mid)
            if not current or current['doc'].get('status') == 'confirmed' and row['doc'].get('record_revision',1) >= current['doc'].get('record_revision',1):
                groups[mid] = row
    bundles, used = [], set()

    def add(key, kind, members, source_ids, job_rows=(), group=None):
        first = min(members, key=lambda r: r['seq'])
        doc = first['doc']
        sources = [captures[i] for i in dict.fromkeys(source_ids) if i in captures]
        timestamps = [clock_for(j['doc'], captures.get(j['doc'].get('capture_id'), {})) for j in job_rows]
        if not timestamps:
            timestamps = [clock_for(s, s) for s in sources]
        timestamp, basis = min((t for t in timestamps if t[0]), key=lambda t: aware(t[0]), default=(None, None))
        if kind == 'Bindings':
            timestamp = doc.get('started_at') or doc.get('requested_at') or doc.get('received_at')
            basis = 'binding_event_time' if aware(timestamp) else None
        # Folder fallback is explicit; never written as a photo capture timestamp.
        local = aware(timestamp) or aware(first['recorded_at'])
        camera = doc.get('camera_id') or next((s.get('camera_id') for s in sources if s.get('camera_id')), None)
        day = local.strftime('%Y-%m-%d') + '_' + token(camera)
        stamp = local.strftime('%H-%M-%S.%f')[:-3] if basis else 'UnknownTime'
        proposed = f'{day}/{kind}/{stamp}_{token(key.split(":", 1)[1])}'
        if kind == 'SourceMaterials':
            proposed = '.System/SourceMaterials/' + proposed
        directory = paths.directory(key, proposed)
        file = 'Binding.json' if kind == 'Bindings' else 'Evidence.json' if kind != 'SourceMaterials' else 'Source.json'
        keys = {(r['entity'], r['entity_id']) for r in members}
        for sid in source_ids:
            if ('scans', sid) in latest:
                keys.add(('scans', sid))
        if group:
            keys.update(k for k, r in latest.items() if k[0] in {'photo_measurements', 'experiment_records'}
                        and r['doc']['measurement_id'] == group['measurement_id'])
        history = sorted((v for k in keys for v in versions.get(k, [])), key=lambda v: v['sequence'])
        bundles.append({'key': key, 'kind': kind, 'directory': directory, 'file': file,
            'camera_id': camera, 'event_at': timestamp if basis else None, 'time_basis': basis,
            'folder_time_basis': basis or 'receipt_date_capture_time_unknown', 'day': directory.split('/')[0],
            'members': members, 'sources': sources, 'jobs': list(job_rows), 'group': group,
            'history': history, 'entity_keys': keys})
        used.update(source_ids)

    for (entity, ident), row in latest.items():
        if entity in {'bindings', 'scene_visits', 'binding_handoffs'}:
            add(entity + ':' + ident, 'Bindings', [row], [row['doc'].get('scan_id')])
    grouped_jobs = set()
    for mid, row in groups.items():
        group = row['doc']
        member_ids = list(dict.fromkeys(group['job_ids'] + group.get('superseded_job_ids',[])))
        linked = [jobs[jid] for jid in member_ids if jid in jobs]
        grouped_jobs.update(member_ids)
        ids = [j['doc']['capture_id'] for j in linked] + [s['capture_id'] for s in group.get('sources', [])]
        kind = 'VoicePhotoReadings' if any((captures.get(i) or {}).get('external_photo') for i in ids) else 'PhotoReadings'
        add('measurement:' + mid, kind, [row] + linked, ids, linked, group)
    standalone = {}
    for jid, row in jobs.items():
        if jid not in grouped_jobs:
            standalone.setdefault(row['doc'].get('capture_id') or row['entity_id'], []).append(row)
    for cid, linked in standalone.items():
        doc = linked[0]['doc']; source = captures.get(cid, {})
        kind = 'VideoReadings' if doc.get('request_trigger') == 'video_stream' else (
            'VoicePhotoReadings' if doc.get('external_photo') or source.get('external_photo') else 'PhotoReadings')
        add('capture:' + cid, kind, linked, [cid], linked)
    for cid, source in captures.items():
        if cid in used:
            continue
        kind = 'Bindings' if source.get('matches') or source.get('scene_matches') else (
            'VoicePhotoReadings' if source.get('external_photo') else 'SourceMaterials')
        add('capture:' + cid, kind, [latest[('scans', cid)]], [cid])
    return bundles, latest


def standard_records(job_rows, group, *, diagnostics=None):
    """Aggregate only an explicit burst; conflicting values remain null for correction."""
    by_capture = {}
    from result_reprocessing import preferred_jobs
    preferred = {d['job_id'] for d in preferred_jobs([r['doc'] for r in job_rows])}
    for row in sorted(job_rows, key=lambda r: r['seq']):
        if row['doc']['job_id'] not in preferred:
            continue
        by_capture[row['doc'].get('capture_id') or row['entity_id']] = row['doc']
    entries = {}
    for original in by_capture.values():
        job = copy.deepcopy(original)
        if group and job.get('capture_id') in group.get('capture_time_corrections', {}):
            job['external_photo'] = (job.get('external_photo') or {}) | {'captured_at': group['capture_time_corrections'][job['capture_id']]}
        # Recompute derived output under current explicit rules, without rewriting receipts.
        for entry in build_records(job):
            snapshot = next((b for b in job.get('all_binding_snapshots', job.get('binding_snapshots', []))
                             if b.get('binding_id') == entry['evidence'].get('binding_id')), {})
            entry['evidence']['operator'] = snapshot.get('operator', job.get('operator'))
            entries.setdefault(entry['instrument_id'], []).append(entry)
    # Corrections remain in the decision/evidence history. Only an instrument
    # established by build_records from the task's binding/region evidence may
    # receive a Result; a correction cannot manufacture a missing QR binding.
    result = []
    for iid, parts in entries.items():
        sessions = {(part['evidence'].get('binding_id'), part['record'].get('wearer_id'),
                     part['evidence'].get('operator')) for part in parts}
        if len(sessions) > 1:
            if diagnostics is not None:
                diagnostics.append({'instrument_id': iid, 'reason': 'binding_session_conflict',
                    'status': 'needs_review', 'observations': [part['evidence'] for part in parts]})
            continue  # A burst cannot collapse usage periods into its first wearer.
        first = copy.deepcopy(parts[0])
        conflicts = []
        for value in first['record']['values']:
            name = value['name']
            alternatives = [v for p in parts for v in p['record']['values'] if v['name'] == name]
            unique = {(v['value'], v['unit'], v.get('display_state')) for v in alternatives}
            if len(unique) > 1:
                value.update(value=None, unit=value['unit'] if len({v['unit'] for v in alternatives}) == 1 else None)
                value.pop('display_state', None)
                conflicts.append(name)
            field = next((f for f in (group or {}).get('fields', []) if (f.get('instrument') or {}).get('id') == iid and f.get('name') == name), None)
            if field:
                # Group fields are the explicitly reconciled/corrected result, with raw candidates retained.
                revised, _ = measurement_value(name, field, field['instrument'])
                if not field.get('corrected') and any(c.get('unit_basis') == 'unit_conflict' for c in field.get('original_candidates', [])):
                    revised.update(value=None, unit=None, range=[None, None])
                value.update(revised)
                if field.get('corrected') or revised['value'] is not None:
                    value.pop('display_state', None)
                if field.get('corrected') and name in conflicts:
                    conflicts.remove(name)
        first['evidence'] = {'schema_version': 'instrument-measurement/1', 'rule_version': RULE_VERSION,
            'observations': [p['evidence'] for p in parts], 'conflicting_fields': conflicts,
            'human_verified': bool(group and group.get('status') == 'confirmed')}
        result.append(first)
    return result


def make_views(bundles, paths, receipts):
    views, days = {}, {}
    owners = {}
    # Choose one physical location per hash, even if QR and readout share a photograph.
    for bundle in sorted(bundles, key=lambda b: (b['kind'] == 'SourceMaterials', b['directory'])):
        artifacts = {}
        for version in bundle['history']:
            for label, item in receipts[version['sequence']].get('artifacts', {}).items():
                if isinstance(item, dict):
                    artifacts.setdefault(item['sha256'], (label, item))
        bundle['artifacts'] = artifacts
        region_names = {r.get('image_sha256'): {'温度': 'Temperature', '转速': 'Speed', '质量': 'Mass'}.get(r.get('measurement_name'), 'Panel')
                        for job in bundle['jobs'] for r in job['doc'].get('panel_regions', [])}
        for digest, (label, item) in artifacts.items():
            suffix = PurePosixPath(item['path']).suffix
            role = region_names.get(digest, 'Panel') if 'panel' in label else 'Original' if 'original' in label else 'Frame' if bundle['kind'] == 'VideoReadings' else 'Photo'
            folder = 'Regions' if 'panel' in label else 'Photos'
            owners.setdefault(digest, f"{bundle['directory']}/{folder}/{role}{digest[:16]}{suffix}")
    placed = {}
    for bundle in bundles:
        for digest, (_, item) in bundle['artifacts'].items():
            if digest not in placed:
                placed[digest] = paths.place(item, owners[digest])
    for bundle in bundles:
        directory = bundle['directory']; file = directory + '/' + bundle['file']
        history = [{**v, 'receipt': paths.name(v['receipt'])} for v in bundle['history']]
        photos = {digest: placed[digest] | {'kind': label} for digest, (label, _) in bundle['artifacts'].items()}
        def evidence(digest):
            return photos.get(digest)
        sources = []
        for source in bundle['sources']:
            timestamp, basis = clock_for(source, source)
            sources.append({'capture_id': source['capture_id'], 'captured_at': timestamp, 'capture_time_basis': basis,
                'received_at': source.get('received_at'), 'external_photo': source.get('external_photo'),
                'video_observation': source.get('video_observation'), 'frame_metadata': source.get('frame_metadata'), 'camera_id': source.get('camera_id'),
                'operator': source.get('operator'), 'original': evidence(source.get('source_sha256')),
                'image': evidence(source.get('image_sha256')), 'qr_matches': source.get('matches', []),
                'scene_matches': source.get('scene_matches', [])})
        group = bundle['group']
        doc = max(bundle['members'], key=lambda r: r['seq'])['doc']
        merge_started = time.monotonic()
        aggregation_issues = []
        records = standard_records(bundle['jobs'], group, diagnostics=aggregation_issues)
        candidate_merge_ms = round((time.monotonic() - merge_started) * 1000, 2)
        observations = []
        for job in bundle['jobs']:
            row = job
            job = copy.deepcopy(row['doc'])
            job['archive'] = {'published_at': row['published_at'], 'readable_at': row['readable_at']}
            if job.get('submitted_at'):
                from readout_timing import update_timing
                update_timing(job, preserve_recorded_durations=True)
            observations.append({k: job.get(k) for k in ('job_id', 'capture_id', 'status', 'lines', 'readings',
                'operator','wearer_id','operator_basis','attribution_status','attribution_reason',
                'binding_snapshot_version','binding_effective_at','binding_time_basis',
                'allowed_instrument_ids','allowed_instrument_ids_basis',
                'local_ocr', 'fallback', 'panel_regions', 'recognition_versions', 'panel_detection', 'timing',
                'instrument', 'all_binding_snapshots', 'binding_snapshots', 'workbench', 'workbenches', 'ownership_conflicts',
                'frame_metadata', 'video_observation', 'model', 'device', 'submitted_at', 'finished_at', 'request_trigger',
                'field_rules','recognition_root_job_id','recognition_revision','supersedes_job_id','reprocessing','display_fields',
                'actual_model_invocation','recognition_skipped','skip_reason','failure_reason','error','outcome')})
        content = {'schema': 'field-recognition-event/2', 'derived_view': True, 'event_id': bundle['key'],
            'event_at': bundle['event_at'], 'time_basis': bundle['time_basis'], 'folder_time_basis': bundle['folder_time_basis'],
            'camera_id': bundle['camera_id'], 'operator': doc.get('operator') or next((s['operator'] for s in sources if s['operator']), None),
            'category': bundle['kind'], 'record_scope': (group or doc).get('record_scope', 'evidence' if bundle['kind'] == 'Bindings' else 'legacy_test_only'),
            'status': (group or doc).get('status') or ('ended' if doc.get('ended_at') else 'active' if doc.get('binding_id') else 'source_retained'),
            'sources': sources, 'photos': photos, 'receipt_versions': history}
        if observations:
            people = {(b.get('operator'),b.get('wearer_id')) for observation in observations
                      for b in observation.get('all_binding_snapshots') or observation.get('binding_snapshots') or []}
            if len(people) > 1:
                content.update(operator=None, personnel_attribution='multiple_sessions_see_observations')
        latest_member = max(bundle['members'], key=lambda r: r['seq'])
        content['archive'] = {'queued_at': latest_member['recorded_at'],
                              'published_at': latest_member['published_at'],
                              'readable_at': latest_member['readable_at'],
                              'readability_basis': latest_member['readability_basis']}
        if bundle['kind'] == 'Bindings':
            content['binding'] = doc
        else:
            measurement_files = []
            for entry in sorted(records, key=lambda r: r['instrument_id']):
                result_file = paths.result_file(bundle['key'], entry['instrument_id'], directory)
                raw = raw_json(entry['record'])
                views[result_file] = raw
                assets = [r.get('instrument') or {} for j in bundle['jobs'] for r in j['doc'].get('panel_regions', [])]
                assets += [f.get('instrument') or {} for f in (group or {}).get('fields', [])]
                name = next((a.get('name') for a in assets if a.get('id') == entry['instrument_id'] and a.get('name')), None)
                measurement_files.append({'instrument_id': entry['instrument_id'], 'instrument_name': name, 'result': result_file,
                    'sha256': hashlib.sha256(raw).hexdigest(), 'evidence': entry['evidence']})
            content.update(measurement_id=group.get('measurement_id') if group else bundle['key'].split(':', 1)[1],
                measurement_files=measurement_files, observations=observations,
                aggregation_issues=aggregation_issues)
            # Completed is a worker lifecycle state, not proof that OCR was invoked.
            from result_reprocessing import preferred_jobs
            current_jobs = preferred_jobs([r['doc'] for r in bundle['jobs']])
            latest_attempts = {}
            for row in bundle['jobs']:
                job = row['doc']
                key = job.get('recognition_root_job_id', job['job_id'])
                if key not in latest_attempts or job.get('recognition_revision', 1) > latest_attempts[key].get('recognition_revision', 1):
                    latest_attempts[key] = job
            processing_jobs = list(latest_attempts.values())
            content['group_id'] = (group or {}).get('context', {}).get('burst_id') or content['measurement_id']
            content['task_ids'] = [j['job_id'] for j in processing_jobs]
            content['task_outcomes'] = [processing_outcome(j) for j in processing_jobs]
            expected = (group or {}).get('context', {}).get('expected_photos', 1)
            content['processing_terminal'] = bool(processing_jobs) and all(t['terminal'] for t in content['task_outcomes']) and len({j.get('capture_id') for j in processing_jobs}) >= expected
            outcomes = {t['outcome'] for t in content['task_outcomes']}
            content['processing_outcome'] = ('failed' if 'failed' in outcomes else 'skipped' if outcomes == {'skipped'} else 'succeeded') if content['processing_terminal'] else 'pending'
            content['processing_status'] = content['status']
            if content['status'] == 'completed' and not records:
                skipped = current_jobs and all(j.get('recognition_skipped') or
                    (j.get('local_ocr') or {}).get('recognition_skipped') for j in current_jobs)
                content['status'] = 'skipped' if skipped else 'unassigned' if any(j.get('readings') for j in current_jobs) else 'unreadable'
            content['skip_reasons'] = list(dict.fromkeys(j.get('skip_reason') or
                (j.get('local_ocr') or {}).get('skip_reason') for j in current_jobs if
                j.get('skip_reason') or (j.get('local_ocr') or {}).get('skip_reason')))
            if not measurement_files:
                # Keep old HTTP links readable without pretending that a measurement exists.
                paths.alias(directory + '/Result.json', file)
            if group:
                # Sources/evidence are normalized above; the decision history stays intact.
                content['decision'] = {k: v for k, v in group.items() if k != 'sources'}
            # Each file is atomic independently. Readers use this manifest and
            # validate hashes; a new Result with an old Evidence is not a snapshot.
            content['publication'] = {'protocol': 'evidence_manifest_sha256/1',
                'atomic_scope': 'one_file_same_directory_rename',
                'reader_rule': 'parse Evidence, parse every measurement_file and verify sha256; retry mismatches',
                'result_count': len(measurement_files),
                'generation': hashlib.sha256(json.dumps({'receipts': [v['sequence'] for v in history],
                    'results': [(m['result'], m['sha256']) for m in measurement_files]}, sort_keys=True).encode()).hexdigest()}
            # Keep the first measured rebuild for this generation. Re-reading or
            # re-indexing unchanged history must not rewrite its evidence bytes.
            timings = paths.data.setdefault('event_stage_timings', {})
            previous = timings.get(bundle['key'], {})
            if previous.get('generation') != content['publication']['generation']:
                timings[bundle['key']] = {'generation': content['publication']['generation'],
                    'group_id': content['group_id'], 'task_ids': content['task_ids'],
                    'durations_ms': {'candidate_merge_ms': candidate_merge_ms},
                    'duration_clock_basis': {'candidate_merge_ms': 'process_monotonic'},
                    'scope': 'first_derived_view_rebuild_for_generation'}
            content['timing'] = timings[bundle['key']]
        views[file] = raw_json(content)
        for entity, ident in bundle['entity_keys']:
            old = 'Records/' + ''.join(w.title() for w in entity.split('_')) + '/' + token(ident)
            paths.alias(old + '/Record.json', file)
            paths.alias(old + '/Readme.html', bundle['day'] + '/DailyReport/DailyReport.html' if bundle['kind'] != 'SourceMaterials' else 'Readme.html')
        if bundle['kind'] != 'SourceMaterials':
            target = (doc.get('instrument') or {}).get('name') or (doc.get('scene') or {}).get('name') or '、'.join(
                dict.fromkeys(r['instrument'].get('name') or r['instrument']['id'] for j in bundle['jobs'] for r in j['doc'].get('panel_regions', []) if r.get('instrument')))
            target = target or '、'.join(v.get('name') or v['id'] for v in doc.get('matches', []) + doc.get('scene_matches', []))
            days.setdefault(bundle['day'], []).append({'event_id': bundle['key'], 'event_at': bundle['event_at'],
                'category': bundle['kind'], 'target': target or '归属待确认', 'operator': content['operator'],
                'status': content['status'], 'result': file,
                'measurement_files': [{k: m[k] for k in ('instrument_id', 'instrument_name', 'result')} for m in content.get('measurement_files', [])],
                'photo': next((v['path'] for v in photos.values() if 'original' in v['kind']), next((v['path'] for v in photos.values()), None))})
    esc = lambda x: html.escape(str(x if x is not None else '未记录'), quote=True)
    statuses = {'source_retained': '原始材料已留存', 'active': '已绑定', 'ended': '已结束', 'completed': '已识别',
                'failed': '识别失败', 'skipped': '未执行读数识别', 'unassigned': '读数未归属', 'unreadable': '未读出结构化读数',
                'queued': '等待识别', 'running': '识别中', 'collecting': '连拍处理中',
                'draft': '待确认', 'confirmed': '已确认', 'rejected': '已驳回', 'cancelled': '已取消', 'interrupted': '已中断'}
    intro = '<p>按日期与相机打开当日报告，再从事件打开结果和照片。一个测量目录对应一次拍照或一次明确的连拍。</p>'
    intro += '<article><ul><li><b>Bindings</b>：Binding.json 记录二维码、人员、仪器或场景、开始/结束和交接。</li><li><b>VideoReadings / VoicePhotoReadings / PhotoReadings</b>：一次视频观测、语音照片或明确连拍共用一个事件目录。</li><li><b>Result.json、Result02.json…</b>：每台仪器独立一个文件，直接保存 wearer_id、device_model、device_no、qr_hash、photo_time、values 六字段。搅拌器为温度和转速，天平为质量；未知值为 null。文件与仪器的对应关系在 Evidence.json 中，后续增加仪器不会改变已有对应关系。</li><li><b>Evidence.json</b>：来源照片、拍摄时间依据、绑定快照、原始 OCR、模型与规则版本、处理状态和确认记录。measurement_files 指向各台仪器的结果，不重复保存结果正文。</li><li><b>Photos / Regions</b>：原图、处理图及面板/数字裁剪；按哈希共用，同一字节只保留一份。</li><li><b>DailyReport</b>：当日事件索引，直接打开每台仪器的读数或追溯文件。</li></ul><p>未绑定且跳过识别时只留 Evidence.json 和照片，不生成伪造的 Result.json。未知仪器不能默认当作 A 或 B；已明确仪器但数字看不清时，其固定字段的 value 为 null。所有图片和结果路径相对于归档根目录；正式写入状态以 Evidence.json 的 record_scope 和 decision 为准。</p></article>'
    intro += '<article><h2>日期与相机</h2><ul>'
    for day, entries in sorted(days.items(), reverse=True):
        base = day + '/DailyReport'
        entries.sort(key=lambda e: e['event_at'] or '', reverse=True)
        views[base + '/DailyReport.json'] = raw_json({'schema': 'field-recognition-day/1', 'timezone': 'Asia/Shanghai', 'events': entries})
        body = '<p><a href="../../Readme.html">归档首页</a> · <a href="DailyReport.json">当日索引 JSON</a></p>'
        body += '<p>点击结果查看结构化读数、来源与历史回执；原图可直接在浏览器打开。</p><table><tr><th>北京时间</th><th>事件 / 对象</th><th>人员 / 状态</th><th>结果与照片</th></tr>'
        for entry in entries:
            moment = aware(entry['event_at'])
            link = lambda target: esc(posixpath.relpath(target, base))
            body += '<tr><td>' + esc(moment.strftime('%H:%M:%S.%f')[:-3] if moment else '拍摄时间未知') + '</td><td>' + esc(KINDS[entry['category']]) + '<br>' + esc(entry['target']) + '</td><td>' + esc(entry['operator']) + '<br>' + esc(statuses.get(entry['status'], entry['status'])) + '</td><td><a href="' + link(entry['result']) + '">' + ('绑定记录' if entry['category'] == 'Bindings' else '追溯与状态') + '</a>'
            for measurement in entry.get('measurement_files', []):
                body += ' · <a href="' + link(measurement['result']) + '">' + esc(measurement['instrument_name'] or measurement['instrument_id']) + ' 读数</a>'
            if entry['photo']:
                body += ' · <a href="' + link(entry['photo']) + '">原图</a>'
            body += '</td></tr>'
        views[base + '/DailyReport.html'] = page(day, body + '</table>')
        intro += '<li><a href="' + esc(base + '/DailyReport.html') + '">' + esc(day) + '</a> · ' + str(len(entries)) + ' 个事件</li>'
    intro += '</ul></article><details><summary>维护与溯源</summary><p>.System 为隐藏维护目录：Receipts 保存不可覆盖的原始回执；Integrity 保存完整性报告；MigrationMap.json 保存旧路径映射和照片位置；SourceMaterials 保留历史未分类材料；PendingAssets 为发布过程中待归位的图片。无需在这些目录日常查数。</p><a href=".System/Audit.html">全量历史版本</a></details>'
    views['Readme.html'] = page('现场识别归档', intro)
    return views
