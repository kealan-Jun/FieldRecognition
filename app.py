"""Independent QR binding and resident PaddleOCR panel recognition."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import cv2
import httpx
import numpy as np
import aliyun_vision
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field, FiniteFloat, model_validator
from ocr_runtime import configured_device, create_model, OcrDeviceUnavailable
from activity import recent_activity, readout_page
from readout_timing import update_timing
from panel_detector import PanelDetector
from photo_measurements import MeasurementContext, configured_mode, attach as attach_measurement, check_retry

BASE = Path(__file__).parent
OCR_DEVICE = configured_device()
RECORD_MODE = configured_mode()
DATA = Path(os.environ.get('FIELD_DEMO_DATA', str(BASE / 'Data')))
DATA.mkdir(parents=True, exist_ok=True)
(DATA / 'Images').mkdir(exist_ok=True)
MAX_BYTES = 12 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 16_000_000


def now():
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(DATA / 'Demo.sqlite3', timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


with db() as conn:
    conn.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS instruments(id TEXT PRIMARY KEY, name TEXT, scene TEXT, model TEXT);
    CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY, document TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS bindings(id TEXT PRIMARY KEY, camera TEXT, ended TEXT, document TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, status TEXT, document TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS camera_resets(camera TEXT PRIMARY KEY, confirmed_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS camera_service_state(camera TEXT PRIMARY KEY, document TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS fallback_attempts(binding_id TEXT PRIMARY KEY, attempted_at REAL NOT NULL);
    ''')
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(instruments)')}
    for column, declaration in [('device_no','TEXT'),('measurement_ranges',"TEXT NOT NULL DEFAULT '{}'")]:
        if column not in columns:
            conn.execute(f'ALTER TABLE instruments ADD COLUMN {column} {declaration}')
    for item in json.loads((BASE / 'InstrumentRegistry.json').read_text())['instruments']:
        conn.execute('INSERT OR IGNORE INTO instruments(id,name,scene,model) VALUES(?,?,?,?)',
                     (item['instrument_id'], item['label'], '', ''))
    # Interrupted jobs stay explicit and cannot be mistaken for completed OCR.
    conn.execute("UPDATE jobs SET status='interrupted' WHERE status IN ('queued','running')")

receiver_camera = None
if os.environ.get('FIELD_RECEIVER_URL'):
    from receiver import ReceiverCamera
    receiver_camera = ReceiverCamera(os.environ['FIELD_RECEIVER_URL'],
                                     os.environ.get('FIELD_CAMERA_ID', 'lubancat-52d2ef0c_cam01'))


@asynccontextmanager
async def lifespan(application):
    # Production components integration
    production_adapter = None
    if os.environ.get('FIELD_PRODUCTION_ENABLED') == '1':
        try:
            from app_production import integrate_production
            production_adapter = integrate_production(globals(), application)
            print("✓ Production components enabled")
        except Exception as e:
            print(f"⚠ Production components failed to load: {e}")

    if receiver_camera:
        receiver_camera.start()
    with db() as conn:
        has_binding = conn.execute('SELECT 1 FROM bindings WHERE ended IS NULL LIMIT 1').fetchone()
    if has_binding or saved_photo_watcher.enabled() or video_ocr.enabled():
        queue_ocr_warmup()
    from photo_job_queue import recover
    recover(globals())
    saved_photo_watcher.start()
    archive_store.start()
    automatic_runner.start()
    video_ocr.start()
    yield
    stopping.set()
    video_ocr.close()
    automatic_runner.close()
    saved_photo_watcher.close()
    archive_store.close()
    live_scanner.close()
    if receiver_camera:
        receiver_camera.close()
    ocr_pool.shutdown(wait=False, cancel_futures=True)
    readout_pool.shutdown(wait=False, cancel_futures=True)
    panel_detector.close()


app = FastAPI(title='FieldRecognition · 现场识别', version='0.1.0', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')
ocr_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='panel-ocr')
readout_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='panel-request')
vision_call_lock = threading.Lock()
ocr_submit_lock = threading.RLock()
stopping = threading.Event()
ocr_lock = threading.RLock()
ocr_warmup_lock = threading.Lock()
ocr_warmup_future = None
ocr_state = {'status': 'not_loaded', 'device': OCR_DEVICE, 'engine': 'PaddleOCR / PP-OCRv5 mobile',
             'resident': False, 'load_count': 0, 'loaded_at': None}
ocr_model = None
panel_detector = PanelDetector()


def instrument_record(row):
    record = dict(row)
    record['measurement_ranges'] = json.loads(record.get('measurement_ranges') or '{}')
    return record


def get_instrument(instrument_id):
    with db() as conn:
        row = conn.execute('SELECT * FROM instruments WHERE id=?', (instrument_id,)).fetchone()
    return instrument_record(row) if row else None


def validate_capture_epoch(conn, scan):
    service = conn.execute('SELECT document FROM camera_service_state WHERE camera=?', (scan['camera_id'],)).fetchone()
    if service and not json.loads(service['document'])['online']:
        raise HTTPException(409, '设备采集服务已离线，重新启动后再扫码绑定')
    reset = conn.execute('SELECT confirmed_at FROM camera_resets WHERE camera=?', (scan['camera_id'],)).fetchone()
    if reset and scan['received_at'] <= reset['confirmed_at']:
        raise HTTPException(409, '这是设备采集服务上一次会话的照片，请重新扫码')


def save_image(data, source, camera):
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, '图片为空或超过 12 MB')
    try:
        with Image.open(io.BytesIO(data)) as original:
            original.load()
            original_extension = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp',
                                  'BMP': '.bmp', 'TIFF': '.tiff'}.get(original.format, '.bin')
            if original.width * original.height > 16_000_000:
                raise ValueError('图片像素过大')
            # EXIF orientation must match what the user sees and crops.
            from PIL import ImageOps
            image = ImageOps.exif_transpose(original).convert('RGB')
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, '无法解码图片，或图片像素超过上限') from exc
    ident = str(uuid.uuid4())
    path = DATA / 'Images' / f'{ident}.png'
    image.save(path)
    source_hash = hashlib.sha256(data).hexdigest()
    original_blob = f'Originals/{source_hash}{original_extension}'
    from archive_store import immutable_write
    immutable_write(DATA / original_blob, data)
    return {'capture_id': ident, 'image_url': f'/api/images/{ident}',
            'source': source, 'camera_id': camera, 'received_at': now(),
            'source_sha256': source_hash, 'original_blob': original_blob,
            'image_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'width': image.width, 'height': image.height, 'path': str(path)}


def qr_matches(values, points):
    matches, scene_matches, unknown = [], [], []
    for ordinal, value in enumerate(values):
        if not value:
            continue
        try:
            payload = json.loads(value)
            if not isinstance(payload, dict) or payload.get('v') != 1 or payload.get('type') not in {'instrument', 'scene'}:
                raise ValueError('unknown schema')
            instrument_id = str(uuid.UUID(payload['id']))
        except (ValueError, TypeError, KeyError):
            unknown.append('不是本系统的仪器码')
            continue
        if payload['type'] == 'scene':
            scene = next((s for s in scene_records() if s['id'] == instrument_id), None)
            if scene:
                if not any(s['id'] == instrument_id for s in scene_matches):
                    scene_matches.append(scene | {'polygon': np.asarray(points[ordinal]).tolist(),
                        'qr_hash':hashlib.sha256(value.encode('utf-8')).hexdigest()})
            else:
                unknown.append('场景编号尚未登记：' + instrument_id)
            continue
        asset = get_instrument(instrument_id)
        if not asset:
            unknown.append('仪器编号尚未登记：' + instrument_id)
            continue
        if not any(row['id'] == instrument_id for row in matches):
            matches.append(asset | {'polygon': np.asarray(points[ordinal]).tolist(),
                'qr_hash':hashlib.sha256(value.encode('utf-8')).hexdigest()})
    return {'matches': matches, 'scene_matches': scene_matches, 'unknown': unknown,
                        'status': 'matched' if matches or scene_matches else 'not_registered' if unknown else 'no_qr',
                        'signature_status': 'unsigned_demo_label'}


def scan_image(data, source, camera, *, decoded=None, metadata=None, operator=None, scan_session_id=None):
    capture = save_image(data, source, camera)
    if decoded is None:
        from qr_decode import decode_qr
        decoded = decode_qr(cv2.imread(capture['path']))
    values, points, diagnostics = decoded
    result = capture | qr_matches(values, points) | {'scan_id': capture['capture_id'], 'qr_diagnostics': diagnostics}
    if metadata is not None:
        result['frame_metadata'] = metadata
    if operator is not None:
        result['operator'] = operator
        result['scan_session_id'] = scan_session_id
    with db() as conn:
        conn.execute('INSERT INTO scans VALUES(?,?)', (result['scan_id'], json.dumps(result)))
    return result


def save_camera_scan(frame, metadata, *, decoded=None, operator=None, scan_session_id=None):
    ok, encoded = cv2.imencode('.png', frame)
    if not ok:
        raise HTTPException(500, '画面编码失败')
    return scan_image(encoded.tobytes(), 'neck_camera_gwhp_main', receiver_camera.target,
                      decoded=decoded, metadata=metadata, operator=operator, scan_session_id=scan_session_id)


class MeasurementRange(BaseModel):
    unit: str | None = Field(default=None, max_length=20)
    range: tuple[FiniteFloat | None, FiniteFloat | None] = (None,None)

    @model_validator(mode='after')
    def ordered(self):
        low, high = self.range
        if low is not None and high is not None and low>high:
            raise ValueError('范围最小值不能大于最大值')
        return self


class InstrumentEdit(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scene: str = Field(min_length=1, max_length=100)
    model: str = Field(default='', max_length=100)
    device_no: str | None = Field(default=None,max_length=100)
    measurement_ranges: dict[str,MeasurementRange] | None = None


class BindingRequest(BaseModel):
    scan_id: uuid.UUID
    instrument_id: uuid.UUID
    operator: str = Field(min_length=1, max_length=80)
    wearer_id: str | None = Field(default=None,max_length=100)


class OcrRequest(BaseModel):
    binding_id: uuid.UUID | None = None
    capture_id: uuid.UUID
    crop: list[int] | None = None
    auto_associate: bool = False
    measurement: MeasurementContext | None = None


@app.get('/')
def index():
    return FileResponse(BASE / 'static' / 'index.html', headers={'Cache-Control': 'no-cache'})


@app.get('/api/state')
def state():
    from history_records import job_rows, panel_readings
    with db() as conn:
        instruments = [instrument_record(row) for row in conn.execute('SELECT * FROM instruments ORDER BY name')]
        bindings = [json.loads(row['document']) for row in conn.execute('SELECT document FROM bindings WHERE ended IS NULL OR rowid IN (SELECT rowid FROM bindings ORDER BY rowid DESC LIMIT 30) ORDER BY rowid DESC')]
        jobs = [json.loads(row['document']) | {'status': row['status']} for row in conn.execute('SELECT status,document FROM jobs ORDER BY rowid DESC LIMIT 20')]
        last_hit = conn.execute("SELECT document FROM scans WHERE json_extract(document,'$.camera_id')=? "
                               "AND json_extract(document,'$.source')='neck_camera_gwhp_main' "
                               "AND (json_array_length(document,'$.matches')>0 OR json_array_length(document,'$.scene_matches')>0) "
                               "ORDER BY rowid DESC LIMIT 1", (receiver_camera.target,)).fetchone() if receiver_camera else None
        activity = recent_activity(conn, receiver_camera.target if receiver_camera else os.environ.get('FIELD_CAMERA_ID'))
        latest_rows = job_rows(conn, receiver_camera.target if receiver_camera else os.environ.get('FIELD_CAMERA_ID'),
                               limit=1, recent=True)
        latest_panel_job = (json.loads(latest_rows[0]['document']) | {'status': latest_rows[0]['status']}) if latest_rows else None
        if latest_panel_job:
            related = panel_readings(latest_panel_job)
            latest_panel_job = latest_panel_job | {'readings': related, 'lines': related}
    return {'scenes': scene_records(), 'scene_visits': scene_visits(), 'instruments': instruments, 'bindings': bindings, 'jobs': jobs, 'ocr': dict(ocr_state),
            'last_camera_scan': json.loads(last_hit['document']) if last_hit else None,
            'activity': activity, 'latest_panel_job': latest_panel_job,
            'record_policy': {'mode': RECORD_MODE, 'automatic_write_scope': 'test_only',
                              'production_photo_submission': 'draft_confirmation',
                              'drafts_url': '/photo-measurements'},
            'archive': archive_store.snapshot(),
            'vision_fallback': aliyun_vision.public_config(), 'photo_watch': saved_photo_watcher.snapshot(),
            'automation': automatic_runner.snapshot(),
            'video_ocr': video_ocr.snapshot(),
            'panel_detector': panel_detector.snapshot(),
            'camera': receiver_camera.snapshot() if receiver_camera else {'configured': bool(os.environ.get('FIELD_CAMERA_SNAPSHOT_URL')),
                       'id': os.environ.get('FIELD_CAMERA_ID', 'UnconfiguredNeckCamera'),
                       'mode': 'http_snapshot'}, 'product': 'FieldRecognition'}


@app.put('/api/instruments/{instrument_id}')
def edit_instrument(instrument_id: uuid.UUID, body: InstrumentEdit):
    if not get_instrument(str(instrument_id)):
        raise HTTPException(404, '仪器不存在')
    if not body.name.strip() or not body.scene.strip():
        raise HTTPException(422, '请填写仪器名称和场景')
    with db() as conn:
        conn.execute('UPDATE instruments SET name=?,scene=?,model=? WHERE id=?',
                     (body.name.strip(), body.scene.strip(), body.model.strip(), str(instrument_id)))
        if 'device_no' in body.model_fields_set:
            conn.execute('UPDATE instruments SET device_no=? WHERE id=?',
                         ((body.device_no or '').strip() or None,str(instrument_id)))
        if 'measurement_ranges' in body.model_fields_set:
            conn.execute('UPDATE instruments SET measurement_ranges=? WHERE id=?',
                         (json.dumps({k:v.model_dump() for k,v in (body.measurement_ranges or {}).items()}),str(instrument_id)))
    return get_instrument(str(instrument_id))


@app.post('/api/scans')
async def scan(file: UploadFile = File(...), camera_id: str = Form('UploadedPhoto')):
    if not camera_id.strip() or len(camera_id) > 100:
        raise HTTPException(422, '相机编号无效')
    data = await file.read(MAX_BYTES + 1)
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(scan_image, data, 'uploaded_photo', camera_id)


@app.post('/api/camera/capture')
def camera_capture():
    if receiver_camera:
        try:
            frame, metadata = receiver_camera.frame()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return save_camera_scan(frame, metadata)
    url = os.environ.get('FIELD_CAMERA_SNAPSHOT_URL')
    camera = os.environ.get('FIELD_CAMERA_ID')
    if not url or not camera:
        raise HTTPException(409, '挂脖设备尚未连接：需要设备编号及返回 JPEG/PNG 的取图接口')
    started = time.monotonic()
    try:
        with httpx.stream('GET', url, timeout=12, follow_redirects=False, trust_env=False) as response:
            response.raise_for_status()
            data = bytearray()
            for block in response.iter_bytes(65536):
                data.extend(block)
                if len(data) > MAX_BYTES or time.monotonic() - started > 15:
                    raise ValueError('capture limit')
        return scan_image(bytes(data), 'neck_camera_http_snapshot', camera)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, '挂脖设备取图失败，请检查连接和取图服务') from exc


@app.get('/api/images/{capture_id}')
def image_file(capture_id: uuid.UUID):
    path = DATA / 'Images' / f'{capture_id}.png'
    if not path.is_file():
        raise HTTPException(404, '图片不存在')
    return FileResponse(path)


@app.post('/api/bindings')
def bind(body: BindingRequest, *, automatic: bool = False):
    with live_scanner.lock:
        result = save_binding(body, automatic=automatic)
    # Enqueue only after the binding transaction commits, without delaying its response.
    queue_ocr_warmup()
    return result


def save_binding(body: BindingRequest, *, automatic: bool = False):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return persist_binding(conn, body, automatic=automatic)


def persist_binding(conn, body, *, automatic=False, handoff_id=None):
    row = conn.execute('SELECT document FROM scans WHERE id=?', (str(body.scan_id),)).fetchone()
    if not row:
        raise HTTPException(404, '扫码记录不存在')
    scan = json.loads(row['document'])
    validate_capture_epoch(conn, scan)
    if not any(item['id'] == str(body.instrument_id) for item in scan['matches']):
        raise HTTPException(409, '该图片没有识别到此仪器码')
    asset = get_instrument(str(body.instrument_id))
    if not asset['scene']:
        raise HTTPException(409, '先在仪器登记中填写所属场景')
    visit = optional_scene(conn, scan['camera_id'], asset['scene'])
    for row in conn.execute('SELECT document FROM scene_visits WHERE camera=? AND ended IS NULL', (scan['camera_id'],)):
        operator = json.loads(row['document']).get('operator')
        if operator and operator != body.operator.strip():
            raise HTTPException(409, '请先结束该相机的已有场景关联，再更换实验员')
    previous = conn.execute('SELECT document FROM bindings WHERE camera=? AND ended IS NULL', (scan['camera_id'],)).fetchall()
    for row in previous:
        existing = json.loads(row['document'])
        if existing['operator'] != body.operator.strip():
            raise HTTPException(409, '请先结束该相机的已有绑定，再更换实验员')
        if existing['instrument']['id'] == str(body.instrument_id):
            return existing
    from instrument_ownership import check_available
    check_available(conn, str(body.instrument_id), scan['camera_id'], handoff_id=handoff_id)
    timestamp = now()
    result = {'binding_id': str(uuid.uuid4()), 'operator': body.operator.strip(),
              'wearer_id':(body.wearer_id or '').strip() or None,
              'qr_hash':next(item.get('qr_hash') for item in scan['matches'] if item['id']==str(body.instrument_id)),
              'camera_id': scan['camera_id'], 'instrument': asset, 'started_at': timestamp,
              'ended_at': None, 'scan_id': scan['scan_id'], 'image_url': scan['image_url'], 'handoff_id': handoff_id,
              'identity_basis': 'unsigned_qr_and_continuous_scan_opt_in' if automatic else 'unsigned_qr_and_operator_confirmation',
              'activity_confirmed': False, 'scene_visit_id': visit['visit_id'] if visit else None,
              'scene': visit['scene'] if visit else {'id': None, 'name': asset['scene']},
              'scene_qr_verified': visit is not None,
              'scene_basis': 'decoded_scene_qr' if visit else 'instrument_registration'}
    service = conn.execute('SELECT document FROM camera_service_state WHERE camera=?', (scan['camera_id'],)).fetchone()
    result['device_service'] = json.loads(service['document']) if service else None
    if not result['operator']:
        raise HTTPException(422, '实验员不能为空')
    conn.execute('INSERT INTO bindings VALUES(?,?,NULL,?)', (result['binding_id'], result['camera_id'], json.dumps(result)))
    return result


@app.post('/api/bindings/{binding_id}/end')
def end_binding(binding_id: uuid.UUID):
    with live_scanner.lock:
        return finish_binding(binding_id)


def finish_binding(binding_id):
    with db() as conn:
        row = conn.execute('SELECT * FROM bindings WHERE id=?', (str(binding_id),)).fetchone()
        if not row:
            raise HTTPException(404, '绑定不存在')
        result = json.loads(row['document'])
        if not row['ended']:
            if receiver_camera and result['camera_id'] == receiver_camera.target:
                automatic_runner.pause('binding_ended')
            result['ended_at'] = now()
            conn.execute('UPDATE bindings SET ended=?,document=? WHERE id=?', (result['ended_at'], json.dumps(result), str(binding_id)))
    return result


class EndRelations(BaseModel):
    camera_id: str = Field(min_length=1, max_length=100)


@app.post('/api/camera/relations/end')
def end_relations(body: EndRelations):
    with live_scanner.lock:
        if receiver_camera and body.camera_id == receiver_camera.target:
            automatic_runner.pause('binding_ended')
        timestamp = now()
        ended = []
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for table in ('bindings', 'scene_visits'):
                for row in conn.execute(f'SELECT * FROM {table} WHERE camera=? AND ended IS NULL', (body.camera_id,)).fetchall():
                    document = json.loads(row['document']) | {'ended_at': timestamp, 'end_reason': 'operator_ended_session'}
                    conn.execute(f'UPDATE {table} SET ended=?,document=? WHERE id=?', (timestamp, json.dumps(document), row['id']))
                    ended.append(row['id'])
        return {'camera_id': body.camera_id, 'ended_ids': ended, 'ended_at': timestamp}


def create_ocr_model():
    return create_model(OCR_DEVICE)


def load_ocr():
    global ocr_model
    with ocr_lock:
        if ocr_model is None:
            started = time.monotonic()
            ocr_state.update(status='loading', error=None, error_detail=None, resident=False)
            try:
                ocr_model = create_ocr_model()
            except Exception as exc:
                ocr_state.update(status='error', error=type(exc).__name__, resident=False,
                                 error_detail=str(exc) if isinstance(exc, OcrDeviceUnavailable) else None)
                raise
            ocr_state.update(loaded_at=now(), load_count=ocr_state['load_count'] + 1,
                             load_seconds=round(time.monotonic() - started, 3))
        ocr_state.update(status='ready', resident=True, error=None, error_detail=None)
        return ocr_model


def warm_ocr():
    try:
        load_ocr()
        panel_detector.warmup()
    except Exception:
        # load_ocr exposes the failure in /api/state; the successful binding is retained.
        return False
    return True


def queue_ocr_warmup():
    global ocr_warmup_future
    with ocr_warmup_lock:
        if ocr_model is not None or (ocr_warmup_future is not None and not ocr_warmup_future.done()):
            return ocr_warmup_future
        ocr_state.update(status='queued', error=None)
        try:
            # Same single worker as inference, so loading/prediction never run in parallel.
            ocr_warmup_future = ocr_pool.submit(warm_ocr)
        except RuntimeError as exc:
            ocr_state.update(status='error', error=type(exc).__name__)
        return ocr_warmup_future


def predict_panel(panel, x=0, y=0):
    """Manual requests and saved-photo events share the resident model."""
    started = time.monotonic()
    ocr_started_at = now()
    invoked = False
    try:
        with ocr_lock:
            model = load_ocr()
            invoked = True
            output = list(model.predict(panel))
            lines = []
            for result in output:
                for text, score, polygon in zip(result['rec_texts'], result['rec_scores'], result['rec_polys']):
                    poly = np.asarray(polygon).astype(float)
                    poly[:, 0] += x
                    poly[:, 1] += y
                    lines.append({'text': text, 'confidence': float(score), 'polygon': poly.tolist(),
                                  'numeric_candidates': re.findall(r'[-+]?\d+(?:\.\d+)?', text)})
        return {'status': 'completed', 'lines': lines, 'model': ocr_state['engine'],
                'ocr_started_at': ocr_started_at, 'ocr_finished_at': now(),
                'actual_model_invocation': True, 'device': OCR_DEVICE,
                'wall_seconds': round(time.monotonic() - started, 3)}
    except Exception as exc:
        ocr_state.update(status='error', error=type(exc).__name__)
        return {'status': 'failed', 'lines': [], 'error': type(exc).__name__,
                'ocr_started_at': ocr_started_at, 'ocr_finished_at': now(),
                'model': ocr_state['engine'], 'device': OCR_DEVICE, 'actual_model_invocation': invoked,
                'wall_seconds': round(time.monotonic() - started, 3)}


def run_ocr(document):
    from panel_readout import run
    run(globals(), document)


def current_panel_bindings(camera_id):
    with db() as conn:
        rows = conn.execute('SELECT document FROM bindings WHERE camera=? AND ended IS NULL', (camera_id,)).fetchall()
    return [b for row in rows if current_readout_binding(b := json.loads(row['document']))]


def predict_readout(panel, x=0, y=0, *, video=False, binding_snapshots=None):
    from panel_regions import predict
    if binding_snapshots is None:
        binding_snapshots = current_panel_bindings(receiver_camera.target) if video and receiver_camera else []
    return predict(globals(), panel, x, y, video=video,
                   allowed_instrument_ids={b['instrument']['id'] for b in binding_snapshots})


def current_readout_binding(document):
    if stopping.is_set():
        return False
    if (document.get('job_id') and document.get('capture_id')
            and (document.get('request_trigger') in {'voice_photo_directory', 'video_stream'} or
                 document.get('binding_time_basis') == 'source_capture_time')):
        # The persisted photograph and its binding-at-capture snapshot survive a
        # later device disconnect. Do not discard a requested historical reading.
        return True
    if document.get('binding_id') is None:
        return bool(document.get('job_id') and document.get('capture_id')
                    and document.get('instrument_association') in {'unbound_photo', 'multiple_candidates', 'same_image_qr_unbound'})
    with db() as conn:
        row = conn.execute('SELECT * FROM bindings WHERE id=?', (document['binding_id'],)).fetchone()
        if not row or row['ended']:
            return False
        binding = json.loads(row['document'])
        asset = get_instrument(binding['instrument']['id'])
        if not asset or asset['scene'] != binding['instrument']['scene']:
            return False
        visit_id = binding.get('scene_visit_id')
        if visit_id and not conn.execute('SELECT 1 FROM scene_visits WHERE id=? AND ended IS NULL', (visit_id,)).fetchone():
            return False
        service = conn.execute('SELECT document FROM camera_service_state WHERE camera=?', (binding['camera_id'],)).fetchone()
        return not service or json.loads(service['document'])['online']


def reserve_fallback(binding_id):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = conn.execute('SELECT attempted_at FROM fallback_attempts WHERE binding_id=?', (binding_id,)).fetchone()
        current = time.time()
        if previous and current - previous['attempted_at'] < 30:
            return False
        conn.execute('INSERT OR REPLACE INTO fallback_attempts VALUES(?,?)', (binding_id, current))
    return True


@app.post('/api/ocr', status_code=202)
def submit_ocr(body: OcrRequest):
    with ocr_submit_lock:
        return enqueue_ocr(body)


def enqueue_ocr(body, *, trigger='explicit_request', precomputed_local=None):
    with db() as conn:
        binding = conn.execute('SELECT * FROM bindings WHERE id=?', (str(body.binding_id),)).fetchone()
        capture = conn.execute('SELECT document FROM scans WHERE id=?', (str(body.capture_id),)).fetchone()
        if not capture:
            raise HTTPException(404, '图片不存在')
        capture = json.loads(capture['document'])
        if binding and binding['camera'] != capture['camera_id']:
            raise HTTPException(409, '图片来源与绑定相机不一致')
        linked = json.loads(binding['document']) if binding else None
        from readout_context import binding_contains_photo
        historical_binding = bool(linked and binding_contains_photo(linked, capture))
        if body.binding_id is not None and (not binding or
                ((binding['ended'] or capture.get('external_photo')) and not historical_binding)):
            raise HTTPException(409, '照片不属于该仪器的有效使用时段')
        if linked and not historical_binding:
            current_asset = get_instrument(linked['instrument']['id'])
            if not current_asset or current_asset['scene'] != linked['instrument']['scene']:
                raise HTTPException(409, '仪器所属场景已更改，请重新绑定')
            visit_id = linked.get('scene_visit_id')
            if visit_id and not conn.execute('SELECT 1 FROM scene_visits WHERE id=? AND ended IS NULL', (visit_id,)).fetchone():
                raise HTTPException(409, '场景已变化，请重新绑定仪器')
            if capture['received_at'] < linked['started_at'] and capture['scan_id'] != linked['scan_id']:
                raise HTTPException(409, '图片早于本次绑定，请重新拍照')
        seen = {item['id'] for item in capture['matches']}
        if linked and seen and (linked['instrument']['id'] not in seen or (len(seen) > 1 and body.crop is None)):
            raise HTTPException(409, '图片仪器二维码与当前绑定冲突，请重新选择仪器')
        from readout_context import at_capture
        context = at_capture(globals(), conn, capture, linked=linked,
                             automatic=body.auto_associate or bool(capture['matches']) or bool(capture.get('external_photo')) or trigger in {'voice_photo_directory', 'video_stream'})
        linked = context.pop('resolved_binding')
        binding_id = linked['binding_id'] if linked else None
        crop = body.crop if body.crop is not None else [0, 0, capture['width'], capture['height']]
        if len(crop) != 4 or min(crop[:2]) < 0 or min(crop[2:]) < 16 or crop[0]+crop[2] > capture['width'] or crop[1]+crop[3] > capture['height']:
            raise HTTPException(422, '面板选框超出图片，或区域太小')
        repeatable = RECORD_MODE == 'production' or body.measurement is not None
        lookup = 'SELECT status,document FROM jobs' + ('' if repeatable else " WHERE status IN ('queued','running')")
        for pending in conn.execute(lookup):
            existing = json.loads(pending['document'])
            if (existing['binding_id'] == binding_id and existing['capture_id'] == str(body.capture_id)
                    and existing['crop'] == crop):
                check_retry(existing, body.measurement, RECORD_MODE)
                return existing | {'status': pending['status']}
        if conn.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 4:
            raise HTTPException(429, 'OCR 正在处理，请稍候再提交')
        document = {'job_id': str(uuid.uuid4()), 'binding_id': binding_id,
                    'capture_id': str(body.capture_id), 'crop': crop, 'submitted_at': now(),
                    'status': 'queued', 'image_url': capture['image_url'],
                    'image_sha256': capture['image_sha256'], 'instrument': linked['instrument'] if linked else None,
                    'operator': linked['operator'] if linked else capture.get('operator'), 'camera_id': capture['camera_id'],
                    'operator_basis': 'binding_snapshot' if linked else capture.get('operator_basis', 'not_recorded'),
                    'operator_registration': capture.get('operator_registration'),
                    'scene_visit_id': linked.get('scene_visit_id') if linked else None, 'scene': linked['scene'] if linked else None,
                    'scene_basis': linked.get('scene_basis', 'decoded_scene_qr') if linked else 'unbound',
                    'scene_qr_verified': linked.get('scene_qr_verified', bool(linked.get('scene_visit_id'))) if linked else False,
                    'instrument_association': ('same_image_qr' if seen else 'operator_selected_current_binding') if linked else 'unbound_photo',
                    'source': capture['source'], 'frame_metadata': capture.get('frame_metadata'),
                    'timing': dict(capture.get('photo_observation') or {}),
                    'external_photo': capture.get('external_photo'), 'request_trigger': trigger}
        document['resume_pending'] = trigger != 'video_stream'
        document['video_observation'] = capture.get('video_observation')
        document.update(context)
        if not linked and context['instrument_candidates']:
            document['instrument_association'] = 'multiple_candidates' if len(context['instrument_candidates']) > 1 else 'same_image_qr_unbound'
            if len(context['instrument_candidates']) == 1:
                # A decoded registered QR establishes identity without fabricating
                # a new session binding for this photograph.
                document['instrument'] = context['instrument_candidates'][0]['instrument']
                document['scene'] = context['workbench']
                document['scene_basis'] = (context['workbench'] or {}).get('basis', 'instrument_registration')
        document['instrument_identity_basis'] = ('decoded_photo_qr' if seen and document['instrument'] else
                                                'session_binding' if linked else 'multiple_candidates' if context['instrument_candidates'] else 'not_observed')
        if precomputed_local is not None:
            document['precomputed_local'] = precomputed_local
        update_timing(document)
        attach_measurement(globals(), conn, document, body.measurement)
        conn.execute('INSERT INTO jobs VALUES(?,?,?)', (document['job_id'], 'queued', json.dumps(document)))
    readout_pool.submit(run_ocr, dict(document))
    return document


@app.get('/api/jobs/{job_id}')
def get_job(job_id: uuid.UUID):
    with db() as conn:
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (str(job_id),)).fetchone()
    if not row:
        raise HTTPException(404, '任务不存在')
    document = json.loads(row['document']) | {'status': row['status']}
    return document | {'archive': archive_store.job_receipt(document)}


@app.get('/api/export')
def export():
    result = state()
    result['exported_at'] = now()
    return result


@app.get('/api/jobs/{job_id}/measurements')
def get_measurements(job_id: uuid.UUID):
    document = get_job(job_id)
    return {'job_id':str(job_id),'records':document.get('measurement_records',[]),
            'status':document['status'],
            'record_scope': document.get('record_scope', 'legacy_test_only'),
            'measurement_id': document.get('measurement_id'),
            'format_available':'measurement_records' in document}


@app.get('/api/readouts')
def readouts(limit: int = 20, before: int | None = None, related_only: bool = False):
    if not 1 <= limit <= 50 or (before is not None and before < 1):
        raise HTTPException(422, '页大小需为 1–50，游标需为正整数')
    camera = receiver_camera.target if receiver_camera else os.environ.get('FIELD_CAMERA_ID')
    with db() as conn:
        return readout_page(conn, camera, limit=limit, before=before,related_only=related_only)


@app.get('/api/workbenches')
def workbenches(limit: int = 200, related_only: bool = False):
    if not 1 <= limit <= 500:
        raise HTTPException(422, '记录范围需为 1–500')
    from workbench_records import summarize
    camera = receiver_camera.target if receiver_camera else os.environ.get('FIELD_CAMERA_ID')
    with db() as conn:
        return summarize(conn, camera, limit=limit,related_only=related_only)


@app.get('/api/archive/files/{relative:path}')
def archive_file(relative: str):
    from archive_integrity import relative_file
    if not archive_store.enabled():
        raise HTTPException(404, '未启用 NAS 留存')
    if (relative not in {'Readme.html', 'Index.json', '00_归档导航.html'}
            and not re.fullmatch(r'(?:Receipts|Objects|Integrity)/[A-Za-z0-9_./-]+\.(?:json|png|jpg|jpeg|webp|bmp|tiff)', relative)
            and not re.fullmatch(r'Browse/[A-Za-z0-9_./-]+\.(?:html|json)', relative)
            and not re.fullmatch(r'01_业务数据/[\w./-]+\.(?:html|json)', relative)):
        raise HTTPException(404, '归档文件不存在')
    try:
        path = relative_file(archive_store._root(create=False), relative)
        if not path.is_file():
            raise FileNotFoundError()
    except (OSError, ValueError):
        raise HTTPException(404, '归档文件暂不可用')
    return FileResponse(path, headers={'Cache-Control': 'no-store'})


# Agent and browser share the same capture, QR, binding and OCR implementation.
from scene_binding import install as install_scenes
install_scenes(globals())

from live_scan import install as install_live_scan
live_scanner = install_live_scan(globals())

from saved_photo import install as install_saved_photo
install_saved_photo(globals())

from photo_watch import SavedPhotoWatcher
saved_photo_watcher = SavedPhotoWatcher(globals())

from automation import install as install_automation
automatic_runner = install_automation(globals())

from video_ocr import install as install_video_ocr
video_ocr = install_video_ocr(globals())

from instrument_ownership import install as install_ownership
install_ownership(globals())

from archive_store import ArchiveStore
from photo_measurements import install as install_measurements
install_measurements(globals())
archive_store = ArchiveStore(globals())

from agent_tools import install_tools
install_tools(app, globals())
