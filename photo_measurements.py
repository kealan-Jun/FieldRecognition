"""Photo/burst drafts. Raw OCR receipts never become production records implicitly."""
import copy
from functools import lru_cache
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import uuid

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat

from measurement_records import number, unit_evidence

RULE_VERSION = 'photo-measurement/2'
TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
UNITS = {'温度': {'°C'}, '转速': {'rpm'}, '质量': {'g', 'mg', 'kg'}}


class MeasurementContext(BaseModel):
    model_config = ConfigDict(extra='forbid')
    burst_id: str | None = Field(default=None, min_length=1, max_length=100)
    expected_photos: int = Field(default=1, ge=1, le=16)
    experiment_context_ref: str | None = Field(default=None, min_length=1, max_length=2000)
    instrument_ids: list[uuid.UUID] = Field(default_factory=list, max_length=16)


class FieldCorrection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field_id: str
    instrument_id: uuid.UUID
    name: str
    value: FiniteFloat = Field(strict=True)
    unit: str = Field(min_length=1, max_length=20)


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    revision: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=100)


class Revision(Decision):
    reason: str = Field(min_length=1, max_length=1000)
    fields: list[FieldCorrection] = Field(default_factory=list, max_length=64)
    experiment_context_ref: str | None = Field(default=None, min_length=1, max_length=2000)
    capture_times: dict[str, AwareDatetime] = Field(default_factory=dict)


class Rejection(Decision):
    reason: str = Field(min_length=1, max_length=1000)


def configured_mode():
    mode = os.environ.get('FIELD_RECORD_MODE', 'test').lower().strip()
    if mode not in {'test', 'production'}:
        raise ValueError('FIELD_RECORD_MODE must be test or production')
    return mode


@lru_cache(maxsize=1)
def versions():
    base = Path(__file__).parent
    packages = {}
    for package in ('paddleocr', 'paddlepaddle-gpu', 'paddlepaddle'):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            pass
    return {'rules': RULE_VERSION, 'packages': packages,
        'code_sha256': {name: hashlib.sha256((base / name).read_bytes()).hexdigest()
            for name in ('photo_measurements.py', 'panel_regions.py', 'digit_regions.py',
                         'reading_results.py', 'measurement_records.py', 'ocr_runtime.py',
                         'readout_context.py', 'instrument_ownership.py')}}


def attach(core, conn, job, context):
    """Called in the same transaction as INSERT jobs; group only on explicit burst ID."""
    mode = core['RECORD_MODE']
    job.update(record_mode=mode, record_scope='test_only', recognition_versions=versions())
    if job.get('request_trigger') == 'video_stream':
        # The production photo workflow does not promote video observations.
        return
    if mode == 'test' and context is None:
        return
    context = context or MeasurementContext()
    if context.expected_photos > 1 and not context.burst_id:
        raise HTTPException(422, '连拍必须提供 burst_id 和预期照片数量')
    metadata = context.model_dump(mode='json')
    job['measurement_context'] = metadata
    row = conn.execute('SELECT document FROM photo_measurements WHERE camera=? AND burst_key=?',
        (job['camera_id'], context.burst_id)).fetchone() if context.burst_id else None
    if row:
        group = json.loads(row['document'])
        if group['context'] != metadata or group['record_mode'] != mode:
            raise HTTPException(409, '同一连拍编号的上下文、照片数量或模式不一致')
        if group['status'] != 'collecting' or len(group['job_ids']) >= group['context']['expected_photos']:
            raise HTTPException(409, '这次测量已收齐照片或已结束，请使用新的连拍编号')
    else:
        group = {'measurement_id': str(uuid.uuid4()), 'camera_id': job['camera_id'],
            'record_mode': mode, 'record_scope': 'draft' if mode == 'production' else 'test_only',
            'context': metadata, 'status': 'collecting', 'revision': 0, 'job_ids': [],
            'sources': [], 'fields': [], 'corrections': [], 'created_at': core['now']()}
        conn.execute('INSERT INTO photo_measurements(id,camera,burst_key,status,document) VALUES(?,?,?,?,?)',
            (group['measurement_id'], job['camera_id'], context.burst_id, group['status'], json.dumps(group)))
    group['job_ids'].append(job['job_id'])
    group['revision'] += 1
    conn.execute('UPDATE photo_measurements SET document=? WHERE id=?',
        (json.dumps(group), group['measurement_id']))
    job.update(measurement_id=group['measurement_id'], record_scope=group['record_scope'])


def check_retry(job, context, mode):
    requested = context.model_dump(mode='json') if context else None
    stored = job.get('measurement_context')
    if requested != stored and not (requested is None and stored == MeasurementContext().model_dump(mode='json')):
        raise HTTPException(409, '照片已属于另一测量上下文，不能通过重试重新归组')
    if job.get('record_mode', 'test') != mode:
        raise HTTPException(409, '这张照片已有其他模式的识别回执，不能自动改成生产记录')


def candidates(jobs):
    fields = {}
    for job in jobs:
        regions = {r['panel_id']: r for r in job.get('panel_regions', [])}
        readings = copy.deepcopy(job.get('readings', []))
        # Preserve a visible but unreadable region so it can be explicitly corrected.
        for pid, region in regions.items():
            if not any(r.get('panel_id') == pid for r in readings):
                readings.append({'panel_id': pid, 'instrument': region.get('instrument'),
                    'measurement_name': region.get('measurement_name'), 'value': None,
                    'binding_id': region.get('binding_id'), 'quality_issue': 'unreadable'})
        for reading in readings:
            region = regions.get(reading.get('panel_id'), {})
            asset = reading.get('instrument') or {}
            iid, name = asset.get('id'), reading.get('measurement_name')
            key = (iid, name, None if name else reading.get('panel_id') or reading.get('reading_id'))
            fid = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:20]
            field = fields.setdefault(fid, {'field_id': fid, 'instrument': asset or None,
                'name': name, 'original_candidates': [], 'corrected': False})
            unit, unit_basis = unit_evidence(name, reading, asset)
            bindings = job.get('all_binding_snapshots', job.get('binding_snapshots', []))
            binding = next((b for b in bindings if b.get('binding_id') == reading.get('binding_id')), {})
            qr = next((q for q in job.get('qr_matches', []) if q.get('id') == iid), {})
            digest = qr.get('qr_hash') or binding.get('qr_hash')
            field['original_candidates'].append({'job_id': job['job_id'], 'capture_id': job['capture_id'],
                'image_sha256': job.get('image_sha256'),
                'raw_text': reading.get('text'), 'raw_value': reading.get('value'), 'value': number(reading),
                'unit': unit, 'unit_basis': unit_basis,
                'quality_issue': 'unit_conflict' if unit_basis == 'unit_conflict' else reading.get('quality_issue'), 'panel_id': reading.get('panel_id'),
                'bbox': region.get('bbox'), 'polygon': reading.get('polygon'), 'digit_region': region.get('digit_region'),
                'panel_image_url': region.get('image_url'), 'panel_image_sha256': region.get('image_sha256'),
                'confidence': reading.get('confidence'), 'confidence_basis': 'model_score_not_measured_accuracy',
                'qr_hash': digest if re.fullmatch('[a-f0-9]{64}', digest or '') else None,
                'qr_scan_id': job['capture_id'] if qr else binding.get('scan_id'),
                'qr_conflict': bool(job.get('qr_matches')) and iid not in {q['id'] for q in job['qr_matches']},
                'identity_basis': reading.get('association_basis'), 'binding_id': reading.get('binding_id')})
    for field in fields.values():
        rows = field['original_candidates']
        unique = {(r['value'], r['unit']) for r in rows if r['value'] is not None}
        units = {r['unit'] for r in rows}
        field.update(value=next(iter(unique))[0] if len(unique) == 1 else None,
            unit=next(iter(units)) if len(units) == 1 else None,
            agreement={'matching_photos': len({r['image_sha256'] or r['capture_id'] for r in rows if r['value'] is not None}) if len(unique) == 1 else 0,
                       'photos_with_region': len({r['capture_id'] for r in rows}),
                       'basis': 'exact_value_and_unit_agreement', 'accuracy': None})
        issues = []
        if len(unique) > 1:
            issues.append('多图读数或单位冲突')
        if any(r['quality_issue'] for r in rows):
            issues.append('存在不可读或格式异常的原始结果')
        if not field['instrument'] or not all(r['qr_hash'] and r['bbox'] for r in rows):
            issues.append('二维码身份或面板归属不明确')
        if any(r['qr_conflict'] for r in rows):
            issues.append('照片二维码与面板仪器不一致')
        field['recognition_issues'] = issues
    return list(fields.values())


def blockers(group):
    issues = []
    if group['status'] == 'collecting':
        issues.append('连拍尚未收齐或识别尚未完成')
    if not group['context'].get('experiment_context_ref'):
        issues.append('缺少实验上下文引用')
    if not group['fields']:
        issues.append('没有可定位的读数字段，请补拍清晰面板')
    overrides = group.get('capture_time_corrections', {})
    for source in group['sources']:
        if not source.get('captured_at') and source['capture_id'] not in overrides:
            issues.append('缺少采集时间：' + source['capture_id'])
        if source['status'] != 'completed':
            issues.append('有照片未完成识别：' + source['job_id'])
    seen = set()
    for field in group['fields']:
        prefix = (field.get('name') or '未定位字段') + '：'
        if not field['corrected']:
            issues.extend(prefix + issue for issue in field['recognition_issues'])
        iid = (field.get('instrument') or {}).get('id')
        if not iid or field.get('name') not in UNITS:
            issues.append(prefix + '需要指定仪器及指标')
        if field.get('value') is None:
            issues.append(prefix + '需要校正数值')
        if field.get('unit') not in UNITS.get(field.get('name'), set()):
            issues.append(prefix + '单位不符或未知')
        declared = group['context']['instrument_ids']
        if declared and iid not in declared:
            issues.append(prefix + '仪器与输入标识不符')
        key = (iid, field.get('name'))
        if key in seen:
            issues.append(prefix + '同一仪器指标存在重复字段，请重新采集清晰区域')
        seen.add(key)
    return list(dict.fromkeys(issues))


def verify_evidence(core, group):
    """Confirmation verifies the preserved byte evidence, not recognition accuracy."""
    root = core['DATA'].resolve()
    for source in group['sources']:
        files = [(Path('Images') / (str(uuid.UUID(source['capture_id'])) + '.png'), source['image_sha256']),
                 (source.get('original_blob'), source.get('source_sha256'))]
        for region in source.get('panel_regions', []):
            if region.get('image_sha256'):
                files.append((Path('Images') / (str(uuid.UUID(region['evidence_id'])) + '.png'), region['image_sha256']))
        for relative, digest in files:
            try:
                path = (root / relative).resolve()
                path.relative_to(root)
                if not re.fullmatch('[a-f0-9]{64}', digest or '') or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError('hash mismatch')
            except (TypeError, ValueError, OSError):
                raise HTTPException(409, '测量图片证据缺失或哈希不一致，不能确认提交') from None


def verify_ownership(conn, group):
    from datetime import datetime
    for field in group['fields']:
        iid = field['instrument']['id']
        captures = {r['capture_id'] for r in field['original_candidates']}
        others = [json.loads(row[0]) for row in conn.execute(
            "SELECT document FROM bindings WHERE camera<>? AND json_extract(document,'$.instrument.id')=?", (group['camera_id'], iid))]
        for source in group['sources']:
            if source['capture_id'] not in captures:
                continue
            moment = datetime.fromisoformat(group.get('capture_time_corrections', {}).get(source['capture_id']) or source['captured_at'])
            for other in others:
                if datetime.fromisoformat(other['started_at']) <= moment and (not other.get('ended_at') or moment < datetime.fromisoformat(other['ended_at'])):
                    raise HTTPException(409, '采集时该仪器由 ' + other['operator'] + '（' + other['camera_id'] + '）使用；材料保留，归属须另行处理')


def refresh(core, conn, mid):
    row = conn.execute('SELECT document FROM photo_measurements WHERE id=?', (mid,)).fetchone()
    if not row:
        return
    group = json.loads(row['document'])
    if group['status'] != 'collecting':
        return
    jobs, sources = [], []
    for jid in group['job_ids']:
        row = conn.execute('SELECT status,document FROM jobs WHERE id=?', (jid,)).fetchone()
        if not row:
            return
        job = json.loads(row['document']) | {'status': row['status']}
        capture = json.loads(conn.execute('SELECT document FROM scans WHERE id=?', (job['capture_id'],)).fetchone()[0])
        jobs.append(job)
        sources.append({'job_id': jid, 'capture_id': job['capture_id'], 'status': row['status'],
            'captured_at': (job.get('external_photo') or {}).get('captured_at'),
            'external_photo': job.get('external_photo'),
            'operator': job.get('operator'), 'operator_registration': job.get('operator_registration'),
            'binding_snapshots': job.get('all_binding_snapshots', job.get('binding_snapshots', [])),
            'binding_time_basis': job.get('binding_time_basis'), 'ownership_conflicts': job.get('ownership_conflicts', []),
            'image_url': capture['image_url'], 'image_sha256': capture['image_sha256'],
            'original_blob': capture.get('original_blob'), 'source_sha256': capture.get('source_sha256'),
            'original_url': f"/api/captures/{job['capture_id']}/original",
            'width': capture['width'], 'height': capture['height'],
            'raw_lines': job.get('lines', []), 'local_ocr': job.get('local_ocr'),
            'fallback': job.get('fallback'), 'panel_regions': job.get('panel_regions', []),
            'panel_detection': job.get('panel_detection'), 'versions': job.get('recognition_versions'),
            'model': job.get('model'), 'device': job.get('device')})
    previous_sources = group['sources']
    group['sources'] = sources
    if len(jobs) == group['context']['expected_photos'] and all(j['status'] in TERMINAL and not j.get('resume_pending') for j in jobs):
        group['fields'] = candidates(jobs)
        group['status'] = 'draft'
    elif sources == previous_sources:
        return
    group['revision'] += 1
    group['updated_at'] = core['now']()
    group['blockers'] = blockers(group)
    conn.execute('UPDATE photo_measurements SET status=?,document=? WHERE id=?',
        (group['status'], json.dumps(group), mid))


def install(core):
    with core['db']() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS photo_measurements(
                id TEXT PRIMARY KEY, camera TEXT NOT NULL, burst_key TEXT, status TEXT NOT NULL, document TEXT NOT NULL,
                UNIQUE(camera,burst_key));
            CREATE TABLE IF NOT EXISTS experiment_records(id TEXT PRIMARY KEY,document TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS experiment_records_no_update BEFORE UPDATE ON experiment_records
                BEGIN SELECT RAISE(ABORT,'Confirmed records are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS experiment_records_no_delete BEFORE DELETE ON experiment_records
                BEGIN SELECT RAISE(ABORT,'Confirmed records are immutable'); END;
        ''')

    def fetch(conn, mid):
        row = conn.execute('SELECT document FROM photo_measurements WHERE id=?', (str(mid),)).fetchone()
        if not row:
            raise HTTPException(404, '测量草稿不存在')
        from security import require_camera
        document = json.loads(row['document'])
        require_camera(document['camera_id'])
        return document

    def store(conn, group):
        group['revision'] += 1
        group['updated_at'] = core['now']()
        group['blockers'] = blockers(group)
        conn.execute('UPDATE photo_measurements SET status=?,document=? WHERE id=?',
            (group['status'], json.dumps(group), group['measurement_id']))
        return group

    def editable(group, body):
        if group['revision'] != body.revision:
            raise HTTPException(409, '草稿已更新，请刷新后核对最新版本')
        if group['status'] != 'draft':
            raise HTTPException(409, '只有收齐照片并完成识别的草稿可修订或确认')

    @core['app'].get('/photo-measurements')
    def page():
        return FileResponse(core['BASE'] / 'static' / 'photo-measurements.html', headers={'Cache-Control': 'no-store'})

    @core['app'].get('/api/photo-measurements')
    def listing(limit: int = 30):
        from security import visible
        if not 1 <= limit <= 100:
            raise HTTPException(422, '数量需为 1–100')
        with core['db']() as conn:
            rows = conn.execute('SELECT document FROM photo_measurements ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()
        return {'record_mode': core['RECORD_MODE'], 'items': [json.loads(r[0]) for r in rows if visible(json.loads(r[0]))]}

    @core['app'].get('/api/photo-measurements/{mid}')
    def detail(mid: uuid.UUID):
        with core['db']() as conn:
            return fetch(conn, mid)

    @core['app'].get('/api/captures/{capture_id}/original')
    def original(capture_id: uuid.UUID):
        with core['db']() as conn:
            row = conn.execute('SELECT document FROM scans WHERE id=?', (str(capture_id),)).fetchone()
        capture = json.loads(row[0]) if row else {}
        relative = capture.get('original_blob')
        if not relative:
            raise HTTPException(404, '这条历史记录没有原始字节副本')
        path = (core['DATA'] / relative).resolve()
        try:
            path.relative_to((core['DATA'] / 'Originals').resolve())
            if hashlib.sha256(path.read_bytes()).hexdigest() != capture['source_sha256']:
                raise ValueError('hash mismatch')
        except (OSError, KeyError, ValueError):
            raise HTTPException(409, '原件不可用或哈希不匹配') from None
        return FileResponse(path, headers={'Cache-Control': 'no-store'})

    @core['app'].post('/api/photo-measurements/{mid}/revisions')
    def revise(mid: uuid.UUID, body: Revision):
        from security import require_role, actor_name
        require_role('admin', 'reviewer')
        body = body.model_copy(update={'actor': actor_name(body.actor)})
        with core['db']() as conn:
            conn.execute('BEGIN IMMEDIATE')
            group = fetch(conn, mid)
            editable(group, body)
            before = copy.deepcopy(group['fields'])
            fields = {f['field_id']: f for f in group['fields']}
            if len({f.field_id for f in body.fields}) != len(body.fields):
                raise HTTPException(422, '修订字段不可重复')
            for change in body.fields:
                field = fields.get(change.field_id)
                asset = core['get_instrument'](str(change.instrument_id))
                if not field or not asset or change.unit not in UNITS.get(change.name, set()):
                    raise HTTPException(422, '字段、仪器或指标单位无效')
                field.update(instrument=asset, name=change.name, value=change.value, unit=change.unit,
                    corrected=True, identity_basis='user_correction')
            if not set(body.capture_times) <= {s['capture_id'] for s in group['sources']}:
                raise HTTPException(422, '采集时间必须对应本次测量的照片')
            if body.experiment_context_ref:
                group['context']['experiment_context_ref'] = body.experiment_context_ref
            group.setdefault('capture_time_corrections', {}).update(
                {cid: moment.isoformat() for cid, moment in body.capture_times.items()})
            group['corrections'].append({'actor': body.actor, 'reason': body.reason, 'at': core['now'](),
                'based_on_revision': body.revision, 'before_fields': before,
                'changes': body.model_dump(mode='json')})
            return store(conn, group)

    @core['app'].post('/api/photo-measurements/{mid}/confirm')
    def confirm(mid: uuid.UUID, body: Decision):
        from security import require_role, actor_name
        require_role('admin', 'reviewer')
        body = body.model_copy(update={'actor': actor_name(body.actor)})
        with core['db']() as conn:
            conn.execute('BEGIN IMMEDIATE')
            group = fetch(conn, mid)
            # An identical retry returns the already committed record; it cannot add another one.
            if group['status'] == 'confirmed':
                if group['confirmation'] != {'actor': body.actor, 'based_on_revision': body.revision}:
                    raise HTTPException(409, '本次测量已经确认')
                return json.loads(conn.execute('SELECT document FROM experiment_records WHERE id=?', (str(mid),)).fetchone()[0])
            editable(group, body)
            if group['record_mode'] != 'production':
                raise HTTPException(409, '测试结果不能提交为生产实验记录')
            issues = blockers(group)
            if issues:
                raise HTTPException(409, {'message': '请先校正以下问题', 'issues': issues})
            verify_evidence(core, group)
            verify_ownership(conn, group)
            group['status'] = 'confirmed'
            group['confirmation'] = {'actor': body.actor, 'based_on_revision': body.revision}
            group['confirmed_at'] = core['now']()
            record = copy.deepcopy(group) | {'record_id': str(mid), 'record_scope': 'production_confirmed',
                'confirmed_fields': [{k: f[k] for k in ('field_id', 'instrument', 'name', 'value', 'unit')}
                                     for f in group['fields']]}
            conn.execute('INSERT INTO experiment_records(id,document) VALUES(?,?)', (str(mid), json.dumps(record)))
            store(conn, group)
        # Read through a fresh connection after COMMIT. A lost response can retry
        # the same decision and receive the same immutable record.
        with core['db']() as conn:
            return json.loads(conn.execute('SELECT document FROM experiment_records WHERE id=?', (str(mid),)).fetchone()[0])

    @core['app'].post('/api/photo-measurements/{mid}/reject')
    def reject(mid: uuid.UUID, body: Rejection):
        from security import require_role, actor_name
        require_role('admin', 'reviewer')
        body = body.model_copy(update={'actor': actor_name(body.actor)})
        with core['db']() as conn:
            conn.execute('BEGIN IMMEDIATE')
            group = fetch(conn, mid)
            if group['revision'] != body.revision or group['status'] not in {'draft', 'collecting'}:
                raise HTTPException(409, '草稿已更新或已经结束')
            group.update(status='rejected', rejection=body.model_dump() | {'at': core['now']()})
            return store(conn, group)

    @core['app'].get('/api/experiment-records')
    def records(limit: int = 30):
        from security import require_role
        require_role('admin','reviewer')
        if not 1 <= limit <= 100:
            raise HTTPException(422, '数量需为 1–100')
        with core['db']() as conn:
            return {'items': [json.loads(row[0]) for row in conn.execute(
                'SELECT document FROM experiment_records ORDER BY rowid DESC LIMIT ?', (limit,))]}

    @core['app'].get('/api/experiment-records/{record_id}')
    def read_record(record_id: uuid.UUID):
        with core['db']() as conn:
            row = conn.execute('SELECT document FROM experiment_records WHERE id=?', (str(record_id),)).fetchone()
        if not row:
            raise HTTPException(404, '已确认实验记录不存在')
        return json.loads(row[0])

    core['measurement_actions'] = {'detail':detail,'revise':revise,'confirm':confirm,'reject':reject}
