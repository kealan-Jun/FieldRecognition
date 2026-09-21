"""Daily QR authorization in Beijing time; source-time history stays immutable."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
VERSION = 'daily-qr-binding/1'


def observe_camera_service(conn, camera, observation, at):
    """End a failed camera session atomically; preserve its evidence and history."""
    expire(conn, at)
    row = conn.execute('SELECT document FROM camera_service_state WHERE camera=?', (camera,)).fetchone()
    previous = json.loads(row[0]) if row else {}
    observed_at = observation.get('observed_at') or at
    if observation.get('observed_at') and previous.get('observed_at') and moment(observed_at) <= moment(previous['observed_at']):
        return None  # A delayed snapshot cannot undo a newer service observation.
    online = observation.get('online') is True
    old_id, new_id = previous.get('media_session_id'), observation.get('media_session_id')
    changed = old_id not in (None, 0) and new_id not in (None, 0) and old_id != new_id
    old_boot, new_boot = previous.get('device_boot_id'), observation.get('device_boot_id')
    rebooted = bool(old_boot and new_boot and old_boot != new_boot)
    active = any(conn.execute(f'SELECT 1 FROM {table} WHERE camera=? AND ended IS NULL LIMIT 1',
                             (camera,)).fetchone() for table in ('bindings', 'scene_visits'))
    reason = None
    if not online and (not previous or previous.get('online') or active):
        reason = observation.get('failure_reason') or 'camera_service_offline'
    elif online:
        if rebooted:
            reason = 'device_boot_changed'
        elif changed:
            reason = 'camera_session_changed'
        elif previous and not previous.get('online'):
            reason = 'camera_service_recovered'
    if reason:
        for table in ('bindings', 'scene_visits'):
            for record in conn.execute(f'SELECT id,document FROM {table} WHERE camera=? AND ended IS NULL',
                                       (camera,)).fetchall():
                doc = json.loads(record['document']) | {'ended_at': at, 'end_reason': reason,
                    'service_observation': observation, 'session_lifecycle_policy': 'camera-service-session/1'}
                conn.execute(f'UPDATE {table} SET ended=?,document=? WHERE id=?', (at, json.dumps(doc), record['id']))
        # Reject old receipts, including scans taken while the camera was offline.
        conn.execute('INSERT OR REPLACE INTO camera_resets VALUES(?,?)', (camera, at))
        # Historical manual transfers must not leave reservations after a session ends.
        for row in conn.execute("SELECT id,document FROM binding_handoffs WHERE status IN ('requested','released')").fetchall():
            doc = json.loads(row['document'])
            if camera in (doc['source_camera_id'], doc['recipient_camera_id']):
                doc.update(status='expired', expired_at=at, end_reason=reason, revision=doc['revision'] + 1)
                conn.execute('UPDATE binding_handoffs SET status=?,document=? WHERE id=?',
                             ('expired', json.dumps(doc), row['id']))
    stored = observation | {'online': online, 'media_session_id': new_id or old_id,
                            'observed_at': observed_at, 'session_lifecycle_policy': 'camera-service-session/1'}
    if new_boot or old_boot:
        stored['device_boot_id'] = new_boot or old_boot
    if not online:
        stored['offline_since'] = previous.get('offline_since') or at
    if changed:
        stored['last_transport_change'] = {'observed_at': at, 'previous_id': old_id,
            'current_id': new_id, 'binding_action': 'ended_service_session'}
    elif previous.get('last_transport_change'):
        stored['last_transport_change'] = previous['last_transport_change']
    conn.execute('INSERT OR REPLACE INTO camera_service_state VALUES(?,?)', (camera, json.dumps(stored)))
    return reason


def moment(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError('Binding time must include a timezone')
    return result


def next_midnight(started_at):
    local = moment(started_at).astimezone(ZONE)
    return (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)


def fields(started_at):
    return {'binding_policy_version': VERSION, 'binding_date': moment(started_at).astimezone(ZONE).date().isoformat(),
            'binding_timezone': 'Asia/Shanghai', 'valid_until': next_midnight(started_at).isoformat()}


def contains(document, at):
    at = moment(at)
    if moment(document['started_at']) > at:
        return False
    # Closed legacy records retain their historical interval. New/current daily
    # relationships carry an explicit expiry even if maintenance has not run yet.
    until = document.get('valid_until')
    if not until and not document.get('ended_at'):
        until = next_midnight(document['started_at']).isoformat()
    return all(at < moment(end) for end in (document.get('ended_at'), until) if end)


def expire(conn, at):
    at = moment(at)
    expired = []
    for table in ('bindings', 'scene_visits'):
        for row in conn.execute(f'SELECT id,document FROM {table} WHERE ended IS NULL').fetchall():
            doc = json.loads(row['document'])
            deadline = moment(doc.get('valid_until') or next_midnight(doc['started_at']).isoformat())
            if deadline > at:
                continue
            doc.update(fields(doc['started_at']), ended_at=deadline.isoformat(), end_reason='daily_qr_expired',
                       expiry_observed_at=at.isoformat())
            changed = conn.execute(f'UPDATE {table} SET ended=?,document=? WHERE id=? AND ended IS NULL',
                (doc['ended_at'], json.dumps(doc), row['id'])).rowcount
            if changed:
                expired.append(row['id'])
    # A pending cross-day handoff cannot authorize use from yesterday's scan.
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='binding_handoffs'").fetchone()
    if exists:
        for row in conn.execute("SELECT id,document FROM binding_handoffs WHERE status IN ('requested','released')").fetchall():
            doc = json.loads(row['document'])
            source = conn.execute('SELECT document FROM bindings WHERE id=?', (doc['binding_id'],)).fetchone()
            if source and next_midnight(json.loads(source[0])['started_at']) <= at:
                doc.update(status='expired', expired_at=at.isoformat(), end_reason='daily_qr_expired', revision=doc['revision']+1)
                conn.execute('UPDATE binding_handoffs SET status=?,document=? WHERE id=?', ('expired',json.dumps(doc),row['id']))
    return expired


def sweep(core):
    with core['db']() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return expire(conn, core['now']())
