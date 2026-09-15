"""Import an existing Agent photograph and its receipt, without taking another photo."""
import base64
import binascii
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from photo_measurements import MeasurementContext, check_retry


class PhotoResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    capture_id: str = Field(min_length=1, max_length=200)
    camera_id: str = Field(min_length=1, max_length=100)
    captured_at: AwareDatetime
    source_ref: str = Field(min_length=1, max_length=2000)
    image_base64: str = Field(min_length=1, max_length=16 * 1024 * 1024)
    sha256: str | None = Field(default=None, pattern=r'^[a-fA-F0-9]{64}$')
    timestamp_basis: str = Field(default='agent_photo_receipt', max_length=100)
    source_written_at: AwareDatetime | None = None


class SavedPhotoRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    binding_id: uuid.UUID | None = None
    photo: PhotoResult | None = None
    image_path: str | None = Field(default=None, min_length=1, max_length=2000)
    crop: list[int] | None = None
    measurement: MeasurementContext | None = None

    @model_validator(mode='after')
    def one_photo(self):
        if (self.photo is None) == (self.image_path is None):
            raise ValueError('Provide exactly one of photo or image_path')
        return self


class BurstRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    burst_id: str = Field(min_length=1, max_length=100)
    photos: list[PhotoResult] = Field(min_length=1, max_length=3)
    experiment_context_ref: str = Field(min_length=1, max_length=2000)
    instrument_ids: list[uuid.UUID] = Field(default_factory=list, max_length=16)

    @model_validator(mode='after')
    def same_camera(self):
        if len({p.camera_id for p in self.photos}) != 1 or len({p.capture_id for p in self.photos}) != len(self.photos):
            raise ValueError('一次连拍必须来自同一相机，照片编号不能重复')
        return self


def load_saved_photo(image_path, *, expected_camera, max_bytes):
    configured = os.environ.get('FIELD_SAVED_PHOTO_ROOT')
    if not configured:
        raise HTTPException(409, '尚未配置已有照片目录；可由 Agent 传入 photo 原图与回执')
    root = Path(configured).resolve()
    supplied = Path(image_path)
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        path = candidate.resolve(strict=True)
        relative = path.relative_to(root)
        # Actual voice_photos layout, not a recursive or latest-file search.
        camera, day, moment, filename = relative.parts
    except OSError:
        raise HTTPException(503, '照片存储暂不可读，保留队列等待恢复') from None
    except ValueError:
        raise HTTPException(422, '照片路径不存在或不在已配置的语音拍照目录内') from None
    if camera != expected_camera:
        raise HTTPException(409, '拍照文件所属相机与当前绑定不一致')
    match = re.fullmatch(r'(\d{8})_(\d{6})(?:_\d+)?\.(?:jpg|jpeg|png)', filename, re.I)
    if not match or not path.is_file():
        raise HTTPException(422, '请提供拍照结果中的具体照片文件，不支持目录或视频')
    try:
        captured = datetime.strptime(match[1] + match[2], '%Y%m%d%H%M%S').replace(
            tzinfo=ZoneInfo(os.environ.get('FIELD_SAVED_PHOTO_TIMEZONE', 'Asia/Shanghai')))
        if day != captured.strftime('%Y-%m-%d') or moment != captured.strftime('%H-%M-%S'):
            raise ValueError('Photo date mismatch')
        before = path.stat()
        if not 0 < before.st_size <= max_bytes:
            raise HTTPException(413, '照片为空或超过 12 MB')
        with path.open('rb') as source:
            raw = source.read(max_bytes + 1)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(raw) != after.st_size:
            raise HTTPException(409, '照片仍在写入，请等待拍照保存完成')
    except OSError:
        raise HTTPException(503, '照片读取失败，保留队列等待存储恢复') from None
    except (ValueError, KeyError):
        raise HTTPException(422, '无法读取完整照片或解析其拍摄时间') from None
    return PhotoResult(capture_id=relative.as_posix(), camera_id=camera, captured_at=captured,
                       source_ref=str(path), image_base64=base64.b64encode(raw).decode(),
                       sha256=hashlib.sha256(raw).hexdigest(),
                       source_written_at=datetime.fromtimestamp(after.st_mtime, timezone.utc),
                       timestamp_basis='nas_filename_local_time_not_hardware_verified')


def install(core):
    with core['db']() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS imported_photos(camera_id TEXT, source_capture_id TEXT, '
                     'sha256 TEXT NOT NULL, capture_id TEXT NOT NULL, PRIMARY KEY(camera_id,source_capture_id))')

    def _read_saved_panel(body, *, trigger='explicit_request', observation=None):
        with core['ocr_submit_lock']:
            with core['db']() as conn:
                row = conn.execute('SELECT document FROM bindings WHERE id=?', (str(body.binding_id),)).fetchone() if body.binding_id else None
            binding = json.loads(row['document']) if row else None
            camera = core['receiver_camera']
            target = (binding['camera_id'] if binding else body.photo.camera_id if body.photo else camera.target if camera else
                      os.environ.get('FIELD_CAMERA_ID') or (body.photo.camera_id if body.photo else None))
            if not target:
                raise HTTPException(409, '请先配置语音照片所属相机')
            read_started = core['now']()
            photo = body.photo or load_saved_photo(body.image_path, expected_camera=target, max_bytes=core['MAX_BYTES'])
            photo_read_at = core['now']()
            if photo.camera_id != target:
                raise HTTPException(409, '拍照结果的相机与当前绑定不一致')
            if body.binding_id:
                from readout_context import binding_contains_photo
                if not binding or not binding_contains_photo(binding, {'external_photo': {'captured_at': photo.captured_at.isoformat()}}):
                    raise HTTPException(409, '照片拍摄时间不属于该仪器绑定时段')
            if ((binding and photo.captured_at < datetime.fromisoformat(binding['started_at'])) or
                    photo.captured_at > datetime.now(timezone.utc) + timedelta(seconds=60)):
                raise HTTPException(409, '拍照时间早于本次绑定或在未来，请核对拍照回执')
            try:
                raw = base64.b64decode(photo.image_base64, validate=True)
            except (ValueError, binascii.Error):
                raise HTTPException(422, 'image_base64 必须是原照片字节的纯 base64') from None
            digest = hashlib.sha256(raw).hexdigest()
            if photo.sha256 and digest != photo.sha256.lower():
                raise HTTPException(422, '照片内容与拍照回执的 SHA-256 不一致')
            external = photo.model_dump(mode='json', exclude={'image_base64', 'sha256'}, exclude_none=True) | {'source_sha256': digest}
            with core['db']() as conn:
                existing = conn.execute('SELECT * FROM imported_photos WHERE camera_id=? AND source_capture_id=?',
                                        (photo.camera_id, photo.capture_id)).fetchone()
                if existing:
                    if existing['sha256'] != digest:
                        raise HTTPException(409, '同一个拍照结果编号对应的照片内容发生变化')
                    capture = json.loads(conn.execute('SELECT document FROM scans WHERE id=?', (existing['capture_id'],)).fetchone()['document'])
                    comparison = dict(external)
                    if 'source_written_at' not in capture['external_photo']:
                        comparison.pop('source_written_at', None)
                    if capture['external_photo'] != comparison:
                        raise HTTPException(409, '同一个拍照结果编号的原始回执发生变化')
                else:
                    capture = None
            if capture is None:
                # The caller resolves NAS access. source_ref is opaque provenance, never
                # interpreted as a filesystem path or a URL to fetch by this service.
                capture = core['scan_image'](raw, 'agent_saved_photo', photo.camera_id, persist=False)
                capture['external_photo'] = external
                capture['photo_observation'] = dict(observation or {}) | {'imported_at': core['now'](),
                    'read_started_at': read_started, 'photo_read_at': photo_read_at,
                    'source_written_at': external.get('source_written_at'),
                    'source_written_basis': ('nas_file_mtime' if body.image_path else
                                             'agent_receipt' if photo.source_written_at else 'not_recorded')}
                # Attribute only a registration that already existed when this photo
                # was taken. A later registrant must not be assigned to older photos.
                with core['db']() as conn:
                    row = conn.execute('SELECT document FROM automation_settings WHERE camera=?', (target,)).fetchone()
                registration = json.loads(row['document']) if row else {}
                registered = registration.get('registered_at')
                if registered and datetime.fromisoformat(registered) <= photo.captured_at:
                    capture.update(operator=registration.get('operator') or None,
                                   operator_basis='camera_registration',
                                   operator_registration={'operator': registration.get('operator'), 'registered_at': registered})
                else:
                    capture.update(operator=None, operator_basis='not_recorded', operator_registration=None)
                with core['db']() as conn:
                    # Identity and dedup mapping commit together. A crash before this
                    # transaction cannot create an extra scan/receipt on retransmission.
                    conn.execute('INSERT INTO scans(id,document) VALUES(?,?)', (capture['capture_id'], json.dumps(capture)))
                    conn.execute('INSERT INTO imported_photos VALUES(?,?,?,?)',
                                 (photo.camera_id, photo.capture_id, digest, capture['capture_id']))
            requested_crop = body.crop if body.crop is not None else [0, 0, capture['width'], capture['height']]
            # Retransmission of the same saved photograph reuses even a finished job.
            # A genuinely new photograph or a different crop is a new request.
            with core['db']() as conn:
                for row in conn.execute('SELECT status,document FROM jobs ORDER BY rowid DESC'):
                    job = json.loads(row['document'])
                    if (job['capture_id'] == capture['capture_id']
                            and job['crop'] == requested_crop):
                        check_retry(job, body.measurement, core['RECORD_MODE'])
                        return job | {'status': row['status']}
            try:
                return core['enqueue_ocr'](core['OcrRequest'](binding_id=binding['binding_id'] if binding else None,
                                        capture_id=capture['capture_id'], crop=body.crop, measurement=body.measurement), trigger=trigger)
            except HTTPException as exc:
                # A photo must still be read if its optional automatic association is
                # stale or conflicts with a decoded QR. Explicit binding requests stay strict.
                if exc.status_code != 409 or body.binding_id or not binding:
                    raise
                return core['enqueue_ocr'](core['OcrRequest'](capture_id=capture['capture_id'], crop=body.crop,
                                         measurement=body.measurement), trigger=trigger)

    def read_saved_panel(body, *, trigger='explicit_request', observation=None):
        from worker_support import process_mutex
        from security import require_camera
        target=body.photo.camera_id if body.photo else core['receiver_camera'].target if core['receiver_camera'] else os.environ.get('FIELD_CAMERA_ID')
        if body.binding_id:
            with core['db']() as conn:
                row=conn.execute('SELECT camera FROM bindings WHERE id=?',(str(body.binding_id),)).fetchone()
            if row:target=row[0]
        require_camera(target)
        with process_mutex(core['DATA'],'photo-import:'+str(target)):
            return _read_saved_panel(body,trigger=trigger,observation=observation)

    core['SavedPhotoRequest'] = SavedPhotoRequest
    core['read_saved_panel'] = read_saved_panel
    @core['app'].post('/api/ocr/photo-result', status_code=202)
    def endpoint(body: SavedPhotoRequest):
        return read_saved_panel(body)

    @core['app'].post('/api/ocr/photo-burst', status_code=202)
    def burst(body: BurstRequest):
        context = MeasurementContext(burst_id=body.burst_id, expected_photos=len(body.photos),
            experiment_context_ref=body.experiment_context_ref, instrument_ids=body.instrument_ids)
        jobs = []
        # Partial submissions are resumable by replaying the SAME burst and photo IDs.
        # Earlier members keep their raw receipts and never become separate records.
        with core['ocr_submit_lock']:
            for photo in body.photos:
                try:
                    jobs.append(read_saved_panel(SavedPhotoRequest(photo=photo, measurement=context)))
                except HTTPException as exc:
                    raise HTTPException(exc.status_code, {'message': exc.detail,
                        'measurement_id': jobs[0].get('measurement_id') if jobs else None,
                        'accepted_job_ids': [j['job_id'] for j in jobs],
                        'retry': '保留 burst_id 和每张照片编号，原样重试可继续未完成的提交'}) from None
        return {'measurement_id': jobs[0]['measurement_id'], 'job_ids': [j['job_id'] for j in jobs],
                'record_mode': core['RECORD_MODE'], 'status': 'collecting',
                'draft_url': '/api/photo-measurements/' + jobs[0]['measurement_id']}
