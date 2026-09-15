"""Date/camera event bundles. A burst, retries and confirmation share one result."""
import copy
import html
import json
import posixpath
from datetime import datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from archive_browse import page, token
from archive_readable import raw_json
from measurement_records import build_records, capture_clock, measurement_value, photo_time, string, RULE_VERSION

KINDS = {'Bindings': '二维码与绑定', 'VideoReadings': '视频面板读数',
         'VoicePhotoReadings': '语音照片读数', 'PhotoReadings': '上传照片读数'}


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
        latest[key] = dict(row) | {'doc': json.loads(row['document'])}
        versions.setdefault(key, []).append({'sequence': row['seq'], 'receipt': row['receipt_path']})
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
            groups[row['doc']['measurement_id']] = row
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
        file = 'Binding.json' if kind == 'Bindings' else 'Result.json' if kind != 'SourceMaterials' else 'Source.json'
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
        linked = [jobs[jid] for jid in group['job_ids'] if jid in jobs]
        grouped_jobs.update(group['job_ids'])
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


def standard_records(job_rows, group):
    """Aggregate only an explicit burst; conflicting values remain null for correction."""
    by_capture = {}
    for row in sorted(job_rows, key=lambda r: r['seq']):
        by_capture[row['doc'].get('capture_id') or row['entity_id']] = row['doc']
    entries = {}
    for original in by_capture.values():
        job = copy.deepcopy(original)
        if group and job.get('capture_id') in group.get('capture_time_corrections', {}):
            job['external_photo'] = (job.get('external_photo') or {}) | {'captured_at': group['capture_time_corrections'][job['capture_id']]}
        # Recompute derived output under current explicit rules, without rewriting receipts.
        for entry in build_records(job):
            entries.setdefault(entry['instrument_id'], []).append(entry)
    # A reviewer can explicitly assign initially unbound material. That decision
    # is separate from a QR binding; do not manufacture a binding or QR digest.
    for field in (group or {}).get('fields', []):
        asset = field.get('instrument') or {}
        iid = asset.get('id')
        if not iid or iid in entries or not field.get('corrected'):
            continue
        names = ['质量'] if field.get('name') == '质量' else ['温度', '转速'] if field.get('name') in {'温度', '转速'} else []
        if not names:
            continue
        source = next(iter(by_capture.values()), {})
        entries[iid] = [{'instrument_id': iid, 'record': {
            'wearer_id': string((source.get('operator_registration') or {}).get('wearer_id')),
            'device_model': string(asset.get('model')), 'device_no': string(asset.get('device_no')),
            'qr_hash': None, 'photo_time': photo_time(source),
            'values': [measurement_value(name, {}, asset)[0] for name in names]},
            'evidence': {'identity_basis': 'explicit_field_correction', 'qr_basis': None,
                         'capture_id': source.get('capture_id')}}]
    result = []
    for iid, parts in entries.items():
        first = copy.deepcopy(parts[0])
        conflicts = []
        for value in first['record']['values']:
            name = value['name']
            alternatives = [v for p in parts for v in p['record']['values'] if v['name'] == name]
            unique = {(v['value'], v['unit']) for v in alternatives}
            if len(unique) > 1:
                value.update(value=None, unit=value['unit'] if len({v['unit'] for v in alternatives}) == 1 else None)
                conflicts.append(name)
            field = next((f for f in (group or {}).get('fields', []) if (f.get('instrument') or {}).get('id') == iid and f.get('name') == name), None)
            if field:
                # Group fields are the explicitly reconciled/corrected result, with raw candidates retained.
                revised, _ = measurement_value(name, field, field['instrument'])
                if not field.get('corrected') and any(c.get('unit_basis') == 'unit_conflict' for c in field.get('original_candidates', [])):
                    revised.update(value=None, unit=None, range=[None, None])
                value.update(revised)
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
        records = standard_records(bundle['jobs'], group)
        observations = []
        for job in bundle['jobs']:
            job = job['doc']
            observations.append({k: job.get(k) for k in ('job_id', 'capture_id', 'status', 'lines', 'readings',
                'local_ocr', 'fallback', 'panel_regions', 'recognition_versions', 'panel_detection', 'timing',
                'instrument', 'all_binding_snapshots', 'binding_snapshots', 'workbench', 'workbenches', 'ownership_conflicts',
                'frame_metadata', 'video_observation', 'model', 'device', 'submitted_at', 'finished_at', 'request_trigger')})
        content = {'schema': 'field-recognition-event/1', 'derived_view': True, 'event_id': bundle['key'],
            'event_at': bundle['event_at'], 'time_basis': bundle['time_basis'], 'folder_time_basis': bundle['folder_time_basis'],
            'camera_id': bundle['camera_id'], 'operator': doc.get('operator') or next((s['operator'] for s in sources if s['operator']), None),
            'category': bundle['kind'], 'record_scope': (group or doc).get('record_scope', 'evidence' if bundle['kind'] == 'Bindings' else 'legacy_test_only'),
            'status': (group or doc).get('status') or ('ended' if doc.get('ended_at') else 'active' if doc.get('binding_id') else 'source_retained'),
            'sources': sources, 'photos': photos, 'receipt_versions': history}
        if bundle['kind'] == 'Bindings':
            content['binding'] = doc
        else:
            content.update(measurement_id=group.get('measurement_id') if group else bundle['key'].split(':', 1)[1],
                instrument_measurements=records, observations=observations)
            if group:
                # Sources/evidence are normalized above; the decision history stays intact.
                content['decision'] = {k: v for k, v in group.items() if k != 'sources'}
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
                'photo': next((v['path'] for v in photos.values() if 'original' in v['kind']), next((v['path'] for v in photos.values()), None))})
    esc = lambda x: html.escape(str(x if x is not None else '未记录'), quote=True)
    statuses = {'source_retained': '原始材料已留存', 'active': '已绑定', 'ended': '已结束', 'completed': '已识别',
                'failed': '识别失败', 'queued': '等待识别', 'running': '识别中', 'collecting': '连拍处理中',
                'draft': '待确认', 'confirmed': '已确认', 'rejected': '已驳回', 'cancelled': '已取消', 'interrupted': '已中断'}
    intro = '<p>按日期与相机打开当日报告，再从事件打开结果和照片。一个测量目录对应一次拍照或一次明确的连拍。</p>'
    intro += '<article><ul><li><b>Bindings</b>：Binding.json 记录二维码、人员、仪器或场景、开始/结束和交接信息。</li><li><b>VideoReadings</b>：Result.json 记录视频面板读数，只留存有效证据帧。</li><li><b>VoicePhotoReadings / PhotoReadings</b>：Result.json 记录语音照片或上传照片的一次测量；连拍、重传和确认不另建测量。</li><li><b>Photos</b>：原始图片及识别使用的标准化图片。相同字节仅保存一份，共用照片通过 JSON 路径引用。</li><li><b>Regions</b>：实际送入识别的面板/数字裁剪，与结果中的区域、字段和图片哈希核对。</li><li><b>DailyReport</b>：当日事件索引，不复制照片或实验记录。UnknownTime 表示没有可靠拍摄时间，目录日期仅为首次接收日期。</li></ul><p>Result.json 的 instrument_measurements 按仪器分别记录温度、转速或质量；原文和处理过程在 observations，草稿校正及确认在 decision。所有图片路径相对于归档根目录。</p></article>'
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
            body += '<tr><td>' + esc(moment.strftime('%H:%M:%S.%f')[:-3] if moment else '拍摄时间未知') + '</td><td>' + esc(KINDS[entry['category']]) + '<br>' + esc(entry['target']) + '</td><td>' + esc(entry['operator']) + '<br>' + esc(statuses.get(entry['status'], entry['status'])) + '</td><td><a href="' + link(entry['result']) + '">结果 JSON</a>'
            if entry['photo']:
                body += ' · <a href="' + link(entry['photo']) + '">原图</a>'
            body += '</td></tr>'
        views[base + '/DailyReport.html'] = page(day, body + '</table>')
        intro += '<li><a href="' + esc(base + '/DailyReport.html') + '">' + esc(day) + '</a> · ' + str(len(entries)) + ' 个事件</li>'
    intro += '</ul></article><details><summary>维护与溯源</summary><p>.System 为隐藏维护目录：Receipts 保存不可覆盖的原始回执；Integrity 保存完整性报告；MigrationMap.json 保存旧路径映射和照片位置；SourceMaterials 保留历史未分类材料；PendingAssets 为发布过程中待归位的图片。无需在这些目录日常查数。</p><a href=".System/Audit.html">全量历史版本</a></details>'
    views['Readme.html'] = page('现场识别归档', intro)
    return views
