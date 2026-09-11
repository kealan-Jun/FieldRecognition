"""Scene registration and camera presence, shared by UI and JSON tools."""
import json
import uuid
from fastapi import HTTPException
from pydantic import BaseModel


class SceneEntry(BaseModel):
    scan_id: uuid.UUID
    scene_id: uuid.UUID


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
            active = conn.execute('SELECT * FROM scene_visits WHERE camera=? AND ended IS NULL', (scan['camera_id'],)).fetchone()
            if active:
                prior = json.loads(active['document'])
                if prior['scene']['id'] == str(body.scene_id):
                    return prior
            if conn.execute('SELECT 1 FROM bindings WHERE camera=? AND ended IS NULL', (scan['camera_id'],)).fetchone():
                raise HTTPException(409, '当前采集服务已有仪器绑定；服务重启或主动结束后才能切换场景')
            timestamp = now()
            for table in ['scene_visits', 'bindings']:
                for row in conn.execute(f'SELECT * FROM {table} WHERE camera=? AND ended IS NULL', (scan['camera_id'],)).fetchall():
                    doc = json.loads(row['document']) | {'ended_at': timestamp, 'end_reason': 'scene_changed'}
                    conn.execute(f'UPDATE {table} SET ended=?,document=? WHERE id=?', (timestamp, json.dumps(doc), row['id']))
            result = {'visit_id': str(uuid.uuid4()), 'camera_id': scan['camera_id'], 'scene': dict(scene),
                      'scan_id': scan['scan_id'], 'image_url': scan['image_url'], 'started_at': timestamp,
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

    core['app'].post('/api/scene/enter')(enter)
    core.update(scene_records=scenes, scene_visits=visits, enter_scene=enter,
                validate_scene=validate, SceneEntry=SceneEntry)
