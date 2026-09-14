"""Read-only activity timeline from stored receipts, including unbound QR hits."""
import json
from datetime import datetime


def recent_activity(conn, camera_id=None, limit=50):
    events = []

    def rows(table, timestamp):
        where, params = ('', []) if camera_id is None else (
            "WHERE json_extract(document,'$.camera_id')=?", [camera_id])
        # Table/field names come only from the fixed calls below.
        return conn.execute(
            f"SELECT * FROM {table} {where} ORDER BY json_extract(document,'$.{timestamp}') DESC LIMIT ?",
            params + [limit]).fetchall()

    def add(doc, kind, timestamp, target, status, evidence=None, detail='', operator=None):
        events.append({'event_id': f'{kind}:{doc.get("scan_id") if kind in {"scan", "photo"} else doc.get("job_id") or doc.get("binding_id") or doc.get("visit_id")}',
                       'kind': kind, 'occurred_at': timestamp, 'camera_id': doc['camera_id'],
                       'operator': doc.get('operator') if operator is None else operator,
                       'target': target, 'status': status, 'detail': detail, 'image_url': evidence})

    for row in rows('scans', 'received_at'):
        doc = json.loads(row['document'])
        hits = doc.get('matches', []) + doc.get('scene_matches', [])
        linked = conn.execute("SELECT document FROM bindings WHERE json_extract(document,'$.scan_id')=? LIMIT 1",
                              (doc['scan_id'],)).fetchone()
        binding = json.loads(linked['document']) if linked else None
        if doc.get('matches'):
            detail = '已建立绑定' if binding else '未建立仪器绑定'
        elif doc.get('scene_matches'):
            visit = conn.execute("SELECT 1 FROM scene_visits WHERE json_extract(document,'$.scan_id')=? LIMIT 1",
                                 (doc['scan_id'],)).fetchone()
            detail = '已记录场景进入' if visit else '场景二维码已识别'
        else:
            detail = '照片已保存，无已登记二维码匹配'
        add(doc, 'photo' if doc.get('source') == 'agent_saved_photo' else 'scan',
            (doc.get('external_photo') or {}).get('captured_at') or doc['received_at'], '、'.join(hit['name'] for hit in hits) or '照片',
            '已识别' if hits else '已留存', doc['image_url'], detail)

    for row in rows('bindings', 'started_at'):
        doc = json.loads(row['document'])
        add(doc, 'binding_started', doc['started_at'], doc['instrument']['name'], '绑定成功', doc['image_url'])
    for row in rows('bindings', 'ended_at'):
        doc = json.loads(row['document'])
        if doc.get('ended_at'):
            add(doc, 'binding_ended', doc['ended_at'], doc['instrument']['name'], '绑定结束', doc['image_url'])

    for row in rows('scene_visits', 'started_at'):
        doc = json.loads(row['document'])
        add(doc, 'scene_entered', doc['started_at'], doc['scene']['name'], '已进入场景', doc['image_url'])
    for row in rows('scene_visits', 'ended_at'):
        doc = json.loads(row['document'])
        if doc.get('ended_at'):
            add(doc, 'scene_left', doc['ended_at'], doc['scene']['name'], '场景关系已结束', doc['image_url'])

    for row in rows('jobs', 'submitted_at'):
        doc = json.loads(row['document'])
        status = row['status']  # Includes startup interruption even if an old JSON snapshot says running.
        readings = '、'.join(str(line['text']) for line in doc.get('lines', []) if line.get('text'))
        label = {'queued': '等待识别', 'running': '正在识别', 'failed': '识别失败',
                 'interrupted': '识别中断', 'cancelled': '识别取消'}.get(status, status)
        if status == 'completed':
            label = '未读出完整数字' if doc.get('outcome') == 'no_numeric_readout' else '识别完成'
        add(doc, 'readout', doc.get('finished_at') or doc['submitted_at'],
            (doc.get('instrument') or {}).get('name') or '未绑定仪器 · 照片读数',
            label, doc.get('crop_image_url') or doc.get('image_url'), readings)
    return sorted(events, key=lambda event: (datetime.fromisoformat(event['occurred_at']), event['event_id']), reverse=True)[:limit]
