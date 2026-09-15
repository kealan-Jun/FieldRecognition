"""Exclusive instrument occupancy and an explicit request/release/accept handoff."""
import json
import uuid
from datetime import datetime,timezone,timedelta
from security import current, require_camera, require_role, actor_name, visible

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


class HandoffRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_id: uuid.UUID
    binding_id: uuid.UUID
    recipient_scan_id: uuid.UUID
    recipient_operator: str = Field(min_length=1, max_length=80)
    recipient_wearer_id: str | None = Field(default=None, max_length=100)


class HandoffDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    actor: str = Field(min_length=1, max_length=80)
    camera_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)


def expire_requests(conn):
    now=datetime.now(timezone.utc)
    for row in conn.execute("SELECT document FROM binding_handoffs WHERE status IN ('requested','released')"):
        item=json.loads(row[0]);expires=item.get('expires_at')
        if expires and datetime.fromisoformat(expires)<=now:
            item.update(status='expired',expired_at=now.isoformat(),revision=item['revision']+1)
            conn.execute('UPDATE binding_handoffs SET status=?,document=? WHERE id=?',('expired',json.dumps(item),item['handoff_id']))


def check_available(conn, instrument_id, camera_id, *, handoff_id=None):
    expire_requests(conn)
    for row in conn.execute("SELECT document FROM bindings WHERE ended IS NULL AND json_extract(document,'$.instrument.id')=?", (instrument_id,)):
        active = json.loads(row[0])
        if active['camera_id'] != camera_id:
            raise HTTPException(409, {'code': 'instrument_in_use', 'message': '仪器正在被另一位实验员使用，必须明确交接',
                'binding_id': active['binding_id'], 'camera_id': active['camera_id'], 'operator': active['operator'],
                'instrument_id': instrument_id, 'started_at': active['started_at'], 'handoff_required': True})
    for row in conn.execute("SELECT document FROM binding_handoffs WHERE instrument_id=? AND status='released'", (instrument_id,)):
        transfer = json.loads(row[0])
        if transfer['handoff_id'] != handoff_id:
            raise HTTPException(409, {'code': 'handoff_reserved', 'message': '仪器已交出，等待指定接收人确认',
                'handoff_id': transfer['handoff_id'], 'recipient_camera_id': transfer['recipient_camera_id']})


def install(core):
    with core['db']() as conn:
        conn.executescript('''CREATE TABLE IF NOT EXISTS binding_handoffs(
            id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, status TEXT NOT NULL, document TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS pending_instrument_handoff ON binding_handoffs(instrument_id)
                WHERE status IN ('requested','released');''')
        # Triggers also protect competing inserts from other local clients. Existing
        # historical rows are preserved even if an older version allowed overlap.
        for action in ('INSERT', 'UPDATE'):
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS exclusive_instrument_{action.lower()}
                BEFORE {action} ON bindings WHEN NEW.ended IS NULL AND EXISTS (
                    SELECT 1 FROM bindings WHERE ended IS NULL AND id<>NEW.id AND
                    json_extract(document,'$.instrument.id')=json_extract(NEW.document,'$.instrument.id'))
                BEGIN SELECT RAISE(ABORT,'Instrument already occupied'); END''')

    def fetch(conn, hid):
        row = conn.execute('SELECT document FROM binding_handoffs WHERE id=?', (str(hid),)).fetchone()
        if not row:
            raise HTTPException(404, '交接请求不存在')
        return json.loads(row[0])

    def save(conn, item):
        item['revision'] += 1
        conn.execute('UPDATE binding_handoffs SET status=?,document=? WHERE id=?',
            (item['status'], json.dumps(item), item['handoff_id']))
        return item

    @core['app'].get('/api/handoffs')
    def listing():
        with core['db']() as conn:
            return {'items': [doc for r in conn.execute('SELECT document FROM binding_handoffs ORDER BY rowid DESC LIMIT 100') for doc in [json.loads(r[0])] if not current() or current().role in {'admin','reviewer'} or {doc['source_camera_id'],doc['recipient_camera_id']} & current().cameras],
                'identity_assurance': 'authenticated_account' if current() else 'local_registered_operator_not_authenticated_account'}

    @core['app'].get('/api/handoffs/eligible-scans')
    def scans():
        with core['db']() as conn:
            rows = conn.execute("SELECT document FROM scans WHERE json_array_length(document,'$.matches')>0 ORDER BY rowid DESC LIMIT 50").fetchall()
        return {'items': [{k: doc.get(k) for k in ('scan_id', 'camera_id', 'received_at', 'matches')}
                           for row in rows for doc in [json.loads(row[0])] if visible(doc)]}

    @core['app'].post('/api/handoffs')
    def request(body: HandoffRequest):
        require_role('admin','operator')
        if current():
            body=body.model_copy(update={'recipient_operator':actor_name(),'recipient_wearer_id':current().user_id})
        with core['live_scanner'].lock, core['db']() as conn:
            conn.execute('BEGIN IMMEDIATE')
            expire_requests(conn)
            existing = conn.execute('SELECT document FROM binding_handoffs WHERE id=?', (str(body.request_id),)).fetchone()
            if existing:
                transfer = json.loads(existing[0])
                if transfer['request'] != body.model_dump(mode='json'):
                    raise HTTPException(409, '同一交接请求编号的内容不能更改')
                return transfer
            old = conn.execute('SELECT document FROM bindings WHERE id=? AND ended IS NULL', (str(body.binding_id),)).fetchone()
            scan = conn.execute('SELECT document FROM scans WHERE id=?', (str(body.recipient_scan_id),)).fetchone()
            if not old or not scan:
                raise HTTPException(409, '原绑定已结束或接收人的扫码证据不存在')
            old, scan = json.loads(old[0]), json.loads(scan[0])
            core['validate_capture_epoch'](conn, scan)
            if scan['camera_id'] == old['camera_id'] or not any(q['id'] == old['instrument']['id'] for q in scan['matches']):
                raise HTTPException(409, '接收相机必须重新扫到该仪器码')
            if conn.execute("SELECT 1 FROM binding_handoffs WHERE instrument_id=? AND status IN ('requested','released')", (old['instrument']['id'],)).fetchone():
                raise HTTPException(409, '此仪器已有未完成的交接请求')
            transfer = {'handoff_id': str(body.request_id), 'instrument_id': old['instrument']['id'],
                'instrument': old['instrument'], 'binding_id': old['binding_id'], 'source_camera_id': old['camera_id'],
                'source_operator': old['operator'], 'recipient_camera_id': scan['camera_id'],
                'camera_id': scan['camera_id'], 'scan_id': scan['scan_id'], 'image_url': scan['image_url'],
                'recipient_operator': body.recipient_operator, 'request': body.model_dump(mode='json'),
                'status': 'requested', 'revision': 1, 'created_at': core['now'](),
                'expires_at':(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat(),
                'identity_assurance': 'authenticated_account' if current() else 'local_registered_operator_not_authenticated_account'}
            conn.execute('INSERT INTO binding_handoffs(id,instrument_id,status,document) VALUES(?,?,?,?)',
                (transfer['handoff_id'], transfer['instrument_id'], transfer['status'], json.dumps(transfer)))
            return transfer

    @core['app'].post('/api/handoffs/{hid}/{action}')
    def transition(hid: uuid.UUID, action: str, body: HandoffDecision):
        require_role('admin','operator')
        require_camera(body.camera_id)
        body=body.model_copy(update={'actor':actor_name(body.actor)})
        if action not in {'release', 'accept', 'cancel', 'reject'}:
            raise HTTPException(404, '交接动作不存在')
        # Expiration persists even if the following stale decision is rejected.
        with core['db']() as conn:
            conn.execute('BEGIN IMMEDIATE')
            expire_requests(conn)
        with core['live_scanner'].lock, core['db']() as conn:
            conn.execute('BEGIN IMMEDIATE')
            expire_requests(conn)
            transfer = fetch(conn, hid)
            expected_actor = transfer['recipient_operator'] if action in {'accept','reject'} else transfer['source_operator']
            expected_camera = transfer['recipient_camera_id'] if action in {'accept','reject'} else transfer['source_camera_id']
            if (body.actor, body.camera_id) != (expected_actor, expected_camera):
                raise HTTPException(409, '交出须由原使用人确认，接收须由指定接收人确认')
            receipt = body.model_dump()
            if transfer.get(action + '_decision') == receipt:
                return transfer
            if body.revision != transfer['revision']:
                raise HTTPException(409, '交接状态已变化，请刷新')
            timestamp = core['now']()
            if action == 'release':
                if transfer['status'] != 'requested':
                    raise HTTPException(409, '此交接不能再交出')
                row = conn.execute('SELECT document FROM bindings WHERE id=? AND ended IS NULL', (transfer['binding_id'],)).fetchone()
                if not row:
                    raise HTTPException(409, '原使用关系已结束，请取消交接并重新扫码')
                previous = json.loads(row[0]) | {'ended_at': timestamp, 'end_reason': 'explicit_handoff', 'handoff_id': str(hid)}
                conn.execute('UPDATE bindings SET ended=?,document=? WHERE id=?',
                    (timestamp, json.dumps(previous), transfer['binding_id']))
                transfer.update(status='released', released_at=timestamp)
            elif action == 'accept':
                if transfer['status'] != 'released':
                    raise HTTPException(409, '原使用人尚未确认交出')
                request = transfer['request']
                binding = core['persist_binding'](conn, core['BindingRequest'](scan_id=request['recipient_scan_id'],
                    instrument_id=transfer['instrument_id'], operator=transfer['recipient_operator'],
                    wearer_id=request.get('recipient_wearer_id')), handoff_id=str(hid))
                transfer.update(status='completed', accepted_at=timestamp, new_binding_id=binding['binding_id'])
            else:
                if transfer['status'] not in {'requested', 'released'}:
                    raise HTTPException(409, '交接已经结束')
                # Cancel releases the reservation; never silently reopens an old binding.
                transfer.update(status='rejected' if action=='reject' else 'cancelled', cancelled_at=timestamp)
            transfer[action + '_decision'] = receipt
            return save(conn, transfer)

    core['handoff_actions'] = {'request':request,'transition':transition,'listing':listing}
