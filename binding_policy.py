"""Daily QR authorization in Beijing time; source-time history stays immutable."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
VERSION = 'daily-qr-binding/1'


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
