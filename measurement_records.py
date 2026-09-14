"""One instrument per photograph, with nulls for facts the evidence cannot supply."""
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def photo_time(document):
    value = (document.get('external_photo') or {}).get('captured_at')
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
    value = float(raw) if '.' in raw else int(raw)
    return value if math.isfinite(value) else None


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
        values = []
        for name in names:
            matches = [r for r in candidates if r.get('measurement_name')==name]
            reading = matches[0] if len(matches)==1 and document.get('status')=='completed' else {}
            value = number(reading)
            unit = {'温度':'°C','转速':'rpm'}.get(name) or string(reading.get('unit'))
            # An explicitly conflicting unit cannot silently become a valid value.
            if reading.get('unit') and name!='质量' and reading['unit']!=unit:
                value = None
            bounds = (instrument.get('measurement_ranges') or {}).get(name,{})
            limits = bounds.get('range',[None,None]) if unit is not None and bounds.get('unit')==unit else [None,None]
            values.append({'name':name,'value':value,'unit':unit,'range':limits})
        record = {'wearer_id':string(binding.get('wearer_id')) or string((document.get('operator_registration') or {}).get('wearer_id')),
            'device_model':string(instrument.get('model')), 'device_no':string(instrument.get('device_no')),
            'qr_hash':digest,'photo_time':photo_time(document),'values':values}
        results.append({'instrument_id':iid,'record':record,'evidence':{
            'job_id':document['job_id'],'capture_id':document['capture_id'],
            'image_url':document.get('image_url'),'image_sha256':document.get('image_sha256'),
            'source_ref':(document.get('external_photo') or {}).get('source_ref'),
            'binding_id':regions[0]['binding_id'],'qr_basis':'same_photo_decode' if qr else 'binding_decode' if digest else None,
            'qr_scan_id':document['capture_id'] if qr else binding.get('scan_id') if digest else None,
            'photo_time_basis':'source_capture_time' if record['photo_time']['timestamp_ms'] is not None else None,
            'panel_ids':[r['panel_id'] for r in regions], 'candidate_readings':candidates,
            'human_verified':False,'value_meaning':'displayed_value_setpoint_or_actual_unknown'}})
    return results
