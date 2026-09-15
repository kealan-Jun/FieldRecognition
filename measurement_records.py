"""One instrument per photograph, with nulls for facts the evidence cannot supply."""
import math
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


UNITS = {'温度': {'°C'}, '转速': {'rpm'}, '质量': {'g', 'mg', 'kg'}}
SCHEMA_VERSION = 'instrument-measurement/1'
RULE_VERSION = 'instrument-measurement/2'


def capture_clock(document):
    """Never treat a receive/processing clock or unsynchronized device tick as UTC."""
    if document.get('external_photo') is not None:
        return (document.get('external_photo') or {}).get('captured_at'), 'source_capture_time'
    video = document.get('video_observation') or {}
    if video.get('clock_sync_valid') is True and video.get('capture_time_basis') == 'synchronized_frame_timestamp':
        return video.get('captured_at'), video.get('capture_time_basis')
    if video or document.get('request_trigger') == 'video_stream':
        clock = video_clock(document.get('frame_metadata') or {})
        return clock['captured_at'], clock['capture_time_basis']
    return None, None


def video_clock(metadata):
    value = metadata.get('global_timestamp_us')
    if metadata.get('clock_sync_valid') is True and type(value) is int and value > 0:
        try:
            moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=value)
            return {'captured_at': moment.isoformat(), 'capture_time_basis': 'synchronized_frame_timestamp',
                    'global_timestamp_us': value, 'clock_sync_valid': True}
        except (OverflowError, ValueError):
            pass
    return {'captured_at': None, 'capture_time_basis': None,
            'global_timestamp_us': value, 'clock_sync_valid': metadata.get('clock_sync_valid') is True}


def photo_time(document):
    value, _ = capture_clock(document)
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            raise ValueError('timezone unknown')
        delta = moment.astimezone(timezone.utc)-datetime(1970,1,1,tzinfo=timezone.utc)
        milliseconds = (delta.days*86400+delta.seconds)*1000+delta.microseconds//1000
        return {'timestamp_ms':milliseconds,
                'time':moment.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}
    except (TypeError, ValueError, OverflowError):
        return {'timestamp_ms':None,'time':None}


def string(value):
    return value.strip() or None if isinstance(value,str) else None


def number(reading):
    raw = str(reading.get('value',''))
    if reading.get('quality_issue') or not re.fullmatch(r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)',raw):
        return None
    # Never turn a missing decimal such as 02287 into 2287.
    if re.fullmatch(r'[+-]?0\d+(?:\.\d+)?',raw):
        return None
    try:
        value = float(raw) if '.' in raw else int(raw)
        return value if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def unit_evidence(name, reading, instrument):
    observed = string(reading.get('unit'))
    registered = string(((instrument.get('measurement_ranges') or {}).get(name) or {}).get('unit'))
    allowed = UNITS.get(name, set())
    conflict = bool((observed and observed not in allowed) or (registered and registered not in allowed)
                    or (observed and registered and observed != registered))
    if conflict:
        return None, 'unit_conflict'
    return (observed, 'recognition_text') if observed else (registered, 'instrument_registry') if registered else (None, 'unknown')


def measurement_value(name, reading, instrument):
    unit, basis = unit_evidence(name, reading, instrument)
    bounds = ((instrument.get('measurement_ranges') or {}).get(name) or {})
    limits = bounds.get('range') if unit and bounds.get('unit') == unit else None
    if (not isinstance(limits, list) or len(limits) != 2
            or any(v is not None and (type(v) not in (float, int) or not math.isfinite(v)) for v in limits)
            or all(v is not None for v in limits) and limits[0] > limits[1]):
        limits = [None, None]
    return {'name': name, 'value': None if basis == 'unit_conflict' else number(reading),
            'unit': unit, 'range': limits}, basis


def build_records(document):
    """The record follows the user's schema; provenance lives outside it."""
    groups = {}
    for region in document.get('panel_regions',[]):
        if region.get('instrument') and region.get('binding_id'):
            groups.setdefault(region['instrument']['id'],[]).append(region)
    results = []
    for iid, regions in groups.items():
        instrument = regions[0]['instrument']
        classes = {r.get('class_id') for r in regions}
        names = ['温度','转速'] if classes=={0} else ['质量'] if classes=={1} else []
        if not names:
            continue
        binding = next((b for b in document.get('all_binding_snapshots',document.get('binding_snapshots',[]))
                        if b.get('binding_id')==regions[0]['binding_id']),{})
        qr = next((q for q in document.get('qr_matches',[]) if q['id']==iid and q.get('qr_hash')),None)
        digest = qr['qr_hash'] if qr else binding.get('qr_hash')
        digest = digest if isinstance(digest,str) and re.fullmatch('[a-f0-9]{64}',digest) else None
        candidates = [r for r in document.get('readings',[]) if (r.get('instrument') or {}).get('id')==iid]
        values, unit_bases = [], {}
        for name in names:
            matches = [r for r in candidates if r.get('measurement_name')==name]
            reading = matches[0] if len(matches)==1 and document.get('status')=='completed' else {}
            value, unit_bases[name] = measurement_value(name, reading, instrument)
            values.append(value)
        record = {'wearer_id':string(binding.get('wearer_id')) or string((document.get('operator_registration') or {}).get('wearer_id')),
            'device_model':string(instrument.get('model')), 'device_no':string(instrument.get('device_no')),
            'qr_hash':digest,'photo_time':photo_time(document),'values':values}
        results.append({'instrument_id':iid,'record':record,'evidence':{
            'job_id':document['job_id'],'capture_id':document['capture_id'],
            'image_url':document.get('image_url'),'image_sha256':document.get('image_sha256'),
            'source_ref':(document.get('external_photo') or {}).get('source_ref'),
            'binding_id':regions[0]['binding_id'],'qr_basis':'same_photo_decode' if qr else 'binding_decode' if digest else None,
            'qr_scan_id':document['capture_id'] if qr else binding.get('scan_id') if digest else None,
            'photo_time_basis':capture_clock(document)[1] if record['photo_time']['timestamp_ms'] is not None else None,
            'schema_version':SCHEMA_VERSION, 'rule_version':RULE_VERSION, 'unit_basis':unit_bases,
            'panel_ids':[r['panel_id'] for r in regions], 'candidate_readings':candidates,
            'human_verified':False,'value_meaning':'displayed_value_setpoint_or_actual_unknown'}})
    return results
