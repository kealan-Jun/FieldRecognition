"""Per-panel OCR, immutable crop evidence and capture-time instrument attribution."""
import copy
import hashlib
import time
import uuid
import re

import cv2
import aliyun_vision as vision
from digit_regions import refine_digits, extract_digits


def predict(core, image, x=0, y=0, *, video=False, allowed_instrument_ids=()):
    detector = core['panel_detector']
    if not detector.enabled():
        return core['predict_panel'](image, x, y)
    started = core['now']()
    begin = time.monotonic()
    allowed = set(allowed_instrument_ids)
    detection = detector.predict(image) if allowed else {'status':'waiting_binding', 'boxes':[], 'model':'YOLO11n'}
    from panel_layout import resolve_type_boxes,roles_for_boxes
    detection['boxes']=resolve_type_boxes(detection['boxes'],allowed,core.get('get_instrument',lambda _:{}))
    detection.update(started_at=started, finished_at=core['now']())
    detection['allowed_instrument_ids'] = sorted(allowed)
    detection['skipped_unbound_panels'] = [b for b in detection['boxes'] if b['instrument_id'] not in allowed]
    roles = roles_for_boxes(detection['boxes'])
    regions, lines, ordinals = [], [], {}
    for box in detection['boxes']:
        if box['instrument_id'] not in allowed:
            continue
        ordinal = ordinals.get(box['class_id'], 0)
        ordinals[box['class_id']] = ordinal + 1
        x1,y1,x2,y2 = box['xyxy']
        # Include a small border for text detection, using original pixels for OCR.
        pad = max(2, round(min(x2-x1,y2-y1)*.08))
        cx,cy = max(0,x1-pad),max(0,y1-pad)
        ex,ey = min(image.shape[1],x2+pad),min(image.shape[0],y2+pad)
        panel = image[cy:ey,cx:ex].copy()
        local = core['predict_panel'](panel, x+cx, y+cy)
        local = refine_digits(core['predict_panel'], panel, local, x+cx, y+cy,
                              source_image=image,source_offset=(x,y))
        if box['class_id'] == 0:
            from led_digits import verify_digits
            local = verify_digits(panel, local, x+cx, y+cy)
            raw_lines = local.get('panel_ocr', {}).get('lines', [])
            if not local.get('lines') and len(raw_lines) == 1 and raw_lines[0].get('text', '').strip().upper() == 'OFF' and (raw_lines[0].get('confidence') or 0) >= .6:
                local['display_state'] = {'text': 'OFF', 'basis': 'recognized_display_text',
                                          'confidence': raw_lines[0]['confidence']}
        region = {'panel_id':f"panel-{box['class_id']}-{ordinal}", 'class_id':box['class_id'],
            'measurement_name':roles.get(((str(box['class_id']),box.get('instrument_id')),tuple(box['xyxy']))),
            'instrument_id':box['instrument_id'], 'detector_confidence':box['confidence'],
            'localization_method':box.get('localization_method','original'),
            'localization_supporting_views':box.get('supporting_views',[]),
            'bbox':[x+x1,y+y1,x+x2,y+y2], 'crop':[x+cx,y+cy,ex-cx,ey-cy], 'local_ocr':local,
            'digit_region':local.get('digit_region'), 'display_state':local.get('display_state')}
        local['lines'] = validate_lines([dict(line, panel_id=region['panel_id']) for line in local.get('lines', [])],
                                       region['measurement_name'], core.get('get_instrument',lambda _:{})(box['instrument_id']) or {})
        regions.append(region); lines.extend(local['lines'])
    result = {'status':'completed', 'lines':lines, 'panel_regions':regions, 'panel_detection':detection,
        'model':core['ocr_state']['engine'], 'device':core['OCR_DEVICE'],
        'actual_model_invocation':any(r['local_ocr'].get('actual_model_invocation') for r in regions),
        'ocr_started_at':regions[0]['local_ocr'].get('ocr_started_at', detection['finished_at']) if regions else detection['finished_at'],
        'ocr_finished_at':core['now'](), 'wall_seconds':round(time.monotonic()-begin,3), 'panel_selection':'detected_display_regions'}
    if not regions:
        result.update(panel_selection='no_bound_panel', recognition_skipped=True,
            skip_reason='no_active_instrument_binding' if not allowed else 'no_visible_bound_panel')
    if detection['status'] == 'failed':
        result['detector_error'] = detection['error']
    return result


def needs_fallback(local):
    if local.get('panel_regions'):
        if any(l.get('quality_issue') for r in local['panel_regions'] for l in r['local_ocr'].get('lines',[])):
            return 'field_rule_conflict'
        return next((reason for r in local['panel_regions'] if not r.get('display_state')
                     if (reason := vision.fallback_reason(r['local_ocr'].get('lines', []),
                                                         r['local_ocr'].get('error')))), None)
    return vision.fallback_reason(local.get('lines', []), local.get('error'))


def finish(core, document, local, image, *, valid, clock=time.monotonic, started=0):
    from panel_readout import save
    document.update(copy.deepcopy(local))
    document['local_ocr'] = copy.deepcopy(local)
    document['capture_association'] = {k:document.get(k) for k in ('instrument', 'binding_id', 'instrument_candidates', 'workbench')}
    snapshots = document.get('all_binding_snapshots', document.get('binding_snapshots', []))
    candidates, benches, combined, attempts = {}, {}, [], []
    for region in document['panel_regions']:
        binding = next((b for b in snapshots if b['instrument']['id'] == region['instrument_id']), None)
        if binding is None:
            raise ValueError('panel has no capture-time instrument binding')
        region.update(instrument=binding['instrument'], binding_id=binding['binding_id'],
            association_basis='panel_detector_session_binding')
        if region['instrument_id'] in document.get('field_rules',{}):
            region['instrument'] = dict(region['instrument'],measurement_ranges=document['field_rules'][region['instrument_id']])
        instrument = region['instrument']
        if instrument:
            with core['db']() as conn:
                scene = conn.execute('SELECT * FROM scenes WHERE name=?', (instrument.get('scene'),)).fetchone()
            bench = {'id':scene['id'] if scene else None, 'name':instrument.get('scene'), 'basis':'instrument_registration'}
            region['workbench'] = bench
            if bench['name']:
                benches[bench['name']] = bench
            candidates[instrument['id']] = {'instrument':instrument,'binding_id':region['binding_id'],'basis':region['association_basis']}
        x,y,w,h = region['crop']
        crop = image[y:y+h,x:x+w].copy()
        if (region.get('digit_region') or {}).get('status') == 'located':
            crop, _ = extract_digits(image,region['digit_region'])
        ident = str(uuid.uuid4()); path = core['DATA']/'Images'/f'{ident}.png'
        if not cv2.imwrite(str(path), crop):
            raise ValueError('panel evidence encoding failed')
        region.update(evidence_id=ident, image_url=f'/api/images/{ident}', image_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        result = region['local_ocr']
        lines = copy.deepcopy(result.get('lines', []))
        lines = validate_lines(lines, region.get('measurement_name'), region['instrument'])
        reason = None if region.get('display_state') else 'field_rule_conflict' if any(l.get('quality_issue') for l in lines) else vision.fallback_reason(lines, result.get('error'))
        if reason and vision.public_config()['available'] and valid():
            attempt = {'panel_id':region['panel_id'], 'trigger':reason}
            if not core['vision_call_lock'].acquire(blocking=False):
                attempt.update(status='skipped', reason='busy')
            else:
                try:
                    scope = f"panel:{document['camera_id']}:{region['instrument_id']}:{region['panel_id']}"
                    if not core['reserve_fallback'](scope):
                        attempt.update(status='skipped', reason='cooldown')
                    elif valid():
                        attempt.update(status='running', attempted_at=core['now'](), trigger_elapsed_seconds=round(clock()-started,3))
                        region['fallback'] = attempt
                        document.update(phase='extended_reading')
                        save(core, document)
                        cloud = vision.read_panel(crop)
                        attempt.update(cloud, finished_at=core['now']())
                        if cloud.get('answer', {}).get('readings'):
                            lines = [{'text':r['text'], 'value':r['value'], 'unit':r.get('unit'),
                                'label':r.get('label'), 'numeric_candidates':[r['value']], 'confidence':None,
                                'polygon':None, 'reading_source':'vision'} for r in cloud['answer']['readings']]
                finally:
                    core['vision_call_lock'].release()
            region['fallback'] = attempt; attempts.append(attempt)
        region['lines'] = [dict(line, panel_id=region['panel_id']) for line in lines]
        combined.extend(region['lines'])
    if not valid():
        document.update(status='cancelled', error='binding_ended_or_changed', lines=[])
        return
    values = list(candidates.values()); unique = values[0] if len(values)==1 else None
    document.update(lines=combined, status='completed', phase='completed', instrument=unique['instrument'] if unique else None,
        binding_id=unique['binding_id'] if unique else None, instrument_candidates=values,
        binding_ids=[v['binding_id'] for v in values if v['binding_id']],
        instrument_association='panel_detector', instrument_identity_basis='panel_model_class',
        association_status='localized_panels', workbenches=list(benches.values()),
        workbench=next(iter(benches.values())) if len(benches)==1 else None,
        outcome='numeric_readout' if any(vision.READOUT.fullmatch(l.get('text','')) or l.get('value') for l in combined) else 'no_numeric_readout')
    if all(r['local_ocr'].get('status') == 'failed' and not r.get('fallback', {}).get('answer', {}).get('readings') for r in document['panel_regions']):
        document.update(status='failed', error='panel_ocr_failed')
    if attempts:
        document['fallback'] = dict(attempts[-1], regions=attempts,
            attempted_at=next((a['attempted_at'] for a in attempts if a.get('attempted_at')), None),
            finished_at=next((a['finished_at'] for a in reversed(attempts) if a.get('finished_at')), None))


def validate_lines(lines, name, instrument):
    from measurement_records import field_issues, measurement_value
    if not name:
        return lines
    result = []
    for line in lines:
        match = re.match(r'\s*('+vision.NUMBER+')',line.get('text',''))
        if not match and line.get('value') is None:
            result.append(line)
            continue
        raw = dict(line,value=str(line['value']) if line.get('value') is not None else match[1],
                   unit=line.get('unit') or (line['text'][match.end():].strip() if match else None) or None)
        issues = field_issues(name,raw,instrument)
        result.append(dict(line,quality_issue=next(iter(issues),None),measurement_name=name,
                           normalized_value=measurement_value(name,raw,instrument)[0]))
    return result
