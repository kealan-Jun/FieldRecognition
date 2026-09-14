"""Business history contains decoded QR events and attributable panel readings.

Raw capture/task receipts remain unchanged and available for diagnosis.
"""
import json
import re

from measurement_records import number
from reading_results import build_readings


def panel_readings(document, status=None):
    if (status or document.get('status')) != 'completed' or document.get('recognition_skipped'):
        return []
    regions = {r['panel_id']: r for r in document.get('panel_regions', [])}
    readings = document.get('readings')
    if readings is None:
        readings = build_readings(document) if regions else []
    related = []
    for reading in readings:
        asset = reading.get('instrument') or {}
        region = regions.get(reading.get('panel_id'), {})
        located = (asset.get('id') and (region.get('instrument') or {}).get('id') == asset['id']
                   and region.get('binding_id') and reading.get('binding_id') == region['binding_id']
                   and reading.get('association_basis') == 'panel_detector_session_binding')
        verified = reading.get('human_verified') is True and asset.get('id')
        if not (located or verified) or number(reading) is None:
            continue
        if re.fullmatch(r'[+-]?8{5,}(?:\.8+)?', str(reading.get('value', ''))):
            continue
        related.append(reading)
    return related


def job_rows(conn, camera=None, *, limit=20, before=None, recent=False):
    # Filter before LIMIT so empty/irrelevant tasks cannot hide older readings
    # or produce blank pages. The function is connection-local and read-only.
    conn.create_function('has_panel_reading', 2,
        lambda raw, status: bool(panel_readings(json.loads(raw), status)), deterministic=True)
    conditions, params = ['has_panel_reading(document,status)'], []
    if camera:
        conditions.append("json_extract(document,'$.camera_id')=?")
        params.append(camera)
    if before is not None:
        conditions.append('rowid<?')
        params.append(before)
    order = ("julianday(coalesce(json_extract(document,'$.finished_at'),json_extract(document,'$.submitted_at'))) DESC,rowid DESC"
             if recent else 'rowid DESC')
    return conn.execute('SELECT rowid AS cursor,* FROM jobs WHERE '+' AND '.join(conditions)+
                        ' ORDER BY '+order+' LIMIT ?',params+[limit]).fetchall()
