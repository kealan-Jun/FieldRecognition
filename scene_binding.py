"""Scene registration and camera presence, shared by UI and JSON tools."""
import json
import uuid
from fastapi import HTTPException
from pydantic import BaseModel, Field


class SceneEntry(BaseModel):
    scan_id: uuid.UUID
    scene_id: uuid.UUID
    operator: str | None = Field(default=None, min_length=1, max_length=80)


def install(core):
    db, now = core['db'], core['now']
    with db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS scenes(id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scene_visits(id TEXT PRIMARY KEY, camera TEXT, ended TEXT, document TEXT);
        ''')
        registry = json.loads((core['BASE'] / 'SceneRegistry.json').read_text())
        for item in registry['scenes']:
            if item.get('scene_name'):
                conn.execute('INSERT OR IGNORE INTO scenes VALUES(?,?)', (item['scene_id'], item['scene_name']))

    def scenes():
        with db() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM scenes ORDER BY name')]

    def visits():
        with db() as conn:
            return [json.loads(r['document']) for r in conn.execute('SELECT document FROM scene_visits WHERE ended IS NULL')]

    def enter(body: SceneEntry, *, automatic: bool = False):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT document FROM scans WHERE id=?', (str(body.scan_id),)).fetchone()
            if not row:
                raise HTTPException(404, '扫码记录不存在')
            scan = json.loads(row['document'])
            core['validate_capture_epoch'](conn, scan)
            scene = conn.execute('SELECT * FROM scenes WHERE id=?', (str(body.scene_id),)).fetchone()
            if not scene or not any(s['id'] == str(body.scene_id) for s in scan.get('scene_matches', [])):
                raise HTTPException(409, '此照片未识别到已登记的场景码')
            if body.operator:
                for binding in conn.execute('SELECT document FROM bindings WHERE camera=? AND ended IS NULL', (scan['camera_id'],)):
                    if json.loads(binding['document'])['operator'] != body.operator.strip():
                        raise HTTPException(409, '请先结束该相机的已有绑定，再更换实验员')
            active = conn.execute('SELECT * FROM scene_visits WHERE camera=? AND ended IS NULL', (scan['camera_id'],)).fetchall()
            for row in active:
                prior = json.loads(row['document'])
                if body.operator and prior.get('operator') and prior['operator'] != body.operator.strip():
                    raise HTTPException(409, '请先结束已有场景关联，再更换实验员')
                if prior['scene']['id'] == str(body.scene_id):
                    return prior
            timestamp = now()
            result = {'visit_id': str(uuid.uuid4()), 'camera_id': scan['camera_id'], 'scene': dict(scene),
                      'scan_id': scan['scan_id'], 'image_url': scan['image_url'], 'started_at': timestamp,
                      'operator': body.operator.strip() if body.operator else None,
                      'ended_at': None, 'identity_basis': 'unsigned_scene_qr_and_continuous_scan_opt_in' if automatic else 'unsigned_scene_qr_and_user_confirmation'}
            conn.execute('INSERT INTO scene_visits VALUES(?,?,NULL,?)', (result['visit_id'], result['camera_id'], json.dumps(result)))
            return result

    def validate(conn, camera, scene_name):
        row = conn.execute('SELECT document FROM scene_visits WHERE camera=? AND ended IS NULL', (camera,)).fetchone()
        if not row:
            raise HTTPException(409, '请先用同一相机扫描场景码并确认进入场景')
        visit = json.loads(row['document'])
        scene = conn.execute('SELECT * FROM scenes WHERE id=?', (visit['scene']['id'],)).fetchone()
        if not scene or scene['name'] != scene_name:
            raise HTTPException(409, '仪器所属场景与该相机当前场景不一致')
        return visit

    def optional_scene(conn, camera, scene_name):
        # An instrument QR establishes the instrument identity by itself. A scene
        # name from its registration is not evidence that a scene QR was scanned.
        for row in conn.execute('SELECT document FROM scene_visits WHERE camera=? AND ended IS NULL', (camera,)):
            visit = json.loads(row['document'])
            if visit['scene']['name'] == scene_name:
                return visit
        return None

    core['app'].post('/api/scene/enter')(enter)
    core.update(scene_records=scenes, scene_visits=visits, enter_scene=enter,
                validate_scene=validate, optional_scene=optional_scene, SceneEntry=SceneEntry)
