"""Isolated routing/parser checks; synthetic detections are not real OCR accuracy."""
import copy

import numpy as np
import pytest

from measurement_records import build_records, display_fields
from photo_measurements import candidates
from reading_results import build_readings
import panel_regions


class Detector:
    def __init__(self, boxes, status='completed'):
        self.boxes, self.status = boxes, status
        self.calls = 0

    def enabled(self):
        return True

    def predict(self, image, **kwargs):
        self.calls += 1
        return {'status':self.status, 'boxes':copy.deepcopy(self.boxes), 'error':'RuntimeError',
                'weights_sha256':'f' * 64, 'model_version':'synthetic-test'}


def model_core(monkeypatch, boxes, texts=None, status='completed'):
    monkeypatch.setattr(panel_regions, 'refine_digits', lambda predict, image, initial, *args, **kw: initial)
    import led_digits
    import lcd_digits
    monkeypatch.setattr(led_digits, 'verify_digits', lambda image, initial, *args:initial)
    monkeypatch.setattr(lcd_digits, 'refine_lcd', lambda predict, image, initial, *args, **kw:initial)
    calls = []
    def predict(image, x, y):
        calls.append((x, y))
        text = (texts or ['0.0010 g'])[min(len(calls)-1, len(texts or [0])-1)]
        return {'lines':[{'text':text, 'confidence':.9}], 'actual_model_invocation':True}
    return {'panel_detector':Detector(boxes, status), 'predict_panel':predict, 'now':lambda:'2026-09-20T01:00:00+00:00',
            'ocr_state':{'engine':'synthetic'}, 'OCR_DEVICE':'cpu',
            'get_instrument':lambda iid:{'id':iid,'type_id':'stirrer' if iid=='stirrer' else 'balance'}}, calls


def box(iid='balance', cls=1, xyxy=None):
    return {'instrument_id':iid, 'class_id':cls, 'xyxy':xyxy or [10,10,80,40], 'confidence':.9}


def bound_document():
    instrument = {'id':'balance', 'measurement_ranges':{'质量':{'unit':'g'}}}
    return {'job_id':'task', 'capture_id':'frame', 'camera_id':'camera', 'status':'completed',
        'external_photo':{'captured_at':'2026-09-20T01:00:00+00:00'},
        'all_binding_snapshots':[{'binding_id':'session-a', 'instrument':instrument, 'camera_id':'camera',
            'operator':'甲', 'wearer_id':'wearer-a', 'started_at':'2026-09-20T00:00:00+00:00',
            'ended_at':'2026-09-20T02:00:00+00:00'}],
        'panel_detection':{'status':'completed'},
        'panel_regions':[{'panel_id':'mass', 'class_id':1, 'instrument_id':'balance', 'instrument':instrument,
            'binding_id':'session-a', 'association_basis':'panel_detector_session_binding',
            'measurement_name':'质量', 'bbox':[10,10,80,40]}],
        'lines':[{'panel_id':'mass', 'text':'0.0010 g', 'confidence':.8}]}


def test_laptop_and_unbound_stirrer_are_excluded_before_ocr(monkeypatch):
    core, calls = model_core(monkeypatch, [box(), box('laptop', 9), box('stirrer', 0)])
    result = panel_regions.predict(core, np.zeros((100,150,3),np.uint8), allowed_instrument_ids=['balance'])
    assert len(calls) == 1
    stages = result['panel_detection']['diagnostics']
    assert stages['raw_candidate_count'] == stages['quality_filtered_count'] == 3
    assert stages['binding_matched_count'] == stages['ocr_input_region_count'] == 1
    assert {r['instrument_id'] for r in stages['excluded_candidates']} == {'laptop','stirrer'}
    assert {r['reason'] for r in stages['excluded_candidates']} == {'target_not_allowed'}
    assert result['panel_regions'][0]['identity_evidence']['weights_sha256'] == 'f'*64
    assert result['panel_detection']['duration_ms'] >= 0 and result['ocr_elapsed_ms'] >= 0


@pytest.mark.parametrize('allowed,boxes,expected,raw', [
    ([], [box('stirrer',0)], 'no_active_instrument_binding', None),
    (['balance'], [box('stirrer',0)], 'target_not_allowed', 1),
    (['balance'], [], 'panel_not_detected', 0),
    (['stirrer'], [dict(box(None,0),type_id='stirrer')], 'instrument_identity_unconfirmed', 1),
])
def test_diagnostics_distinguish_not_run_miss_disallowed_and_unknown_identity(monkeypatch, allowed, boxes, expected, raw):
    core, calls = model_core(monkeypatch, boxes)
    result = panel_regions.predict(core,np.zeros((100,150,3),np.uint8),allowed_instrument_ids=allowed)
    assert result['failure_reason'] == expected and result['recognition_skipped']
    assert result['panel_detection']['diagnostics']['raw_candidate_count'] == raw
    assert result['panel_detection']['diagnostics']['ocr_input_region_count'] == 0
    assert not calls and result['ocr_elapsed_ms'] is None
    assert core['panel_detector'].calls == bool(allowed)


def test_detector_exception_is_failed_not_successful_empty_result(monkeypatch):
    core, _ = model_core(monkeypatch, [], status='failed')
    result = panel_regions.predict(core,np.zeros((100,150,3),np.uint8),allowed_instrument_ids=['balance'])
    assert result['status']=='failed' and result['failure_reason']=='processing_exception'
    assert result['error']=='panel_detection_failed'
    assert result['panel_detection']['diagnostics']['raw_candidate_count'] is None


def test_stirrer_binding_enters_both_windows_without_inventing_values(monkeypatch):
    boxes = [box('stirrer',0,[10,10,60,40]),box('stirrer',0,[75,10,125,40])]
    core, calls = model_core(monkeypatch,boxes,['63 °C','1140 rpm'])
    result = panel_regions.predict(core,np.zeros((100,150,3),np.uint8),allowed_instrument_ids=['stirrer'])
    assert len(calls)==2
    assert [r['measurement_name'] for r in result['panel_regions']]==['温度','转速']
    assert [r['text'] for r in result['lines']]==['63 °C','1140 rpm']
    assert result['panel_detection']['diagnostics']['binding_matched_count']==2


def test_snapshot_filter_keeps_background_in_raw_evidence_only():
    doc = bound_document()
    rogue = copy.deepcopy(doc['panel_regions'][0])
    rogue.update(panel_id='computer',instrument={'id':'laptop'},instrument_id='laptop',binding_id='forged')
    doc['panel_regions'].append(rogue)
    doc['lines'].append({'panel_id':'computer','text':'999.9999 g'})
    doc['readings'] = build_readings(doc)
    assert len(doc['lines'])==2 and len(doc['readings'])==1
    assert [r['instrument_id'] for r in build_records(doc)]==['balance']
    assert [f['instrument']['id'] for f in candidates([doc])]==['balance']
    # Previously computed/externally supplied candidates cannot bypass export checks.
    doc['readings'].append(dict(doc['readings'][0],panel_id='computer',instrument={'id':'laptop'},binding_id='forged'))
    assert len(candidates([doc]))==1
    assert len(build_records(doc))==1


@pytest.mark.parametrize('change', ['wrong_camera','outside_interval','missing_binding'])
def test_invalid_capture_authorization_cannot_create_formal_fields(change):
    doc = bound_document()
    if change=='wrong_camera':doc['camera_id']='different'
    elif change=='outside_interval':doc['external_photo']['captured_at']='2026-09-20T03:00:00+00:00'
    else:doc['all_binding_snapshots']=[]
    doc['readings']=build_readings(doc)
    assert doc['readings']==build_records(doc)==candidates([doc])==[]


def test_same_value_clarity_difference_preserves_precision_but_real_conflict_stays_null():
    doc=bound_document()
    doc['lines']=[dict(doc['lines'][0],clarity='clear'),dict(doc['lines'][0],clarity='medium',confidence=.7)]
    doc['readings']=build_readings(doc)
    result=build_records(doc)[0]
    assert result['record']['values'][0]['value']==.001
    assert display_fields(doc)[0]['display_value']=='0.0010'
    assert [r['text'] for r in result['evidence']['candidate_readings']]==['0.0010 g','0.0010 g']
    assert result['evidence']['field_statuses']['质量']=='recognized'
    doc['lines'][1]['text']='0.0020 g';doc['readings']=build_readings(doc)
    result=build_records(doc)[0]
    assert result['record']['values'][0]['value'] is None
    assert result['evidence']['field_statuses']['质量']=='readout_conflict'
    assert len(result['evidence']['candidate_readings'])==2


def test_off_is_per_field_with_source_and_missing_speed_remains_explicit():
    doc=bound_document();instrument={'id':'stirrer','measurement_ranges':{'温度':{'unit':'°C'}}}
    doc['all_binding_snapshots'][0]['instrument']=instrument
    region=doc['panel_regions'][0]
    region.update(class_id=0,instrument=instrument,instrument_id='stirrer',panel_id='temp',measurement_name='温度',
                  display_state={'text':'OFF','raw_text':'OFF','basis':'recognized_display_text','source':'local_ocr.panel_ocr'})
    doc['lines']=[];doc['readings']=[]
    record=build_records(doc)[0]
    assert record['record']['values'][0]['display_state']=='OFF'
    assert record['record']['values'][0]['value'] is None
    assert record['record']['values'][1]['value'] is None and 'display_state' not in record['record']['values'][1]
    assert record['evidence']['field_statuses']=={'温度':'display_state','转速':'field_not_detected'}
    assert record['evidence']['field_completeness']=='incomplete'
    field=candidates([doc])[0]
    assert field['display_state']=='OFF' and field['original_candidates'][0]['quality_issue'] is None
    assert field['original_candidates'][0]['raw_text']=='OFF'
    assert field['original_candidates'][0]['display_state_source']['source']=='local_ocr.panel_ocr'
    numeric=copy.deepcopy(doc);numeric['job_id']='later';numeric['capture_id']='later-frame'
    numeric['panel_regions'][0].pop('display_state')
    numeric['lines']=[{'panel_id':'temp','text':'63 °C'}];numeric['readings']=build_readings(numeric)
    conflict=candidates([doc,numeric])[0]
    assert conflict['value'] is conflict['display_state'] is None
    assert '多图读数或单位冲突' in conflict['recognition_issues']


def test_unknown_unit_and_precision_are_reported_without_registry_guess():
    doc=bound_document();doc['lines'][0]['text']='0.0010'
    doc['panel_regions'][0]['instrument'].pop('measurement_ranges')
    doc['readings']=build_readings(doc)
    field=display_fields(doc)[0]
    assert field['value']==.001 and field['display_value']=='0.0010'
    assert field['unit'] is None and field['unit_basis']=='unknown'
    assert field['status']=='needs_correction' and field['issues']==['unit_unknown']


def test_recovery_counts_raw_candidates_and_explains_quality_rejection():
    from panel_recovery import detect
    responses=iter([[],[box('ignored',0)],[box('ignored',0)],[box('ignored',0)]])
    result=detect(np.full((100,150,3),220,np.uint8),lambda *args:{'boxes':next(responses)})
    stages=result['diagnostics']
    assert stages['raw_candidate_count']==3 and stages['quality_filtered_count']==0
    assert sum(p['candidate_count'] for p in stages['passes'])==3
    assert 'display_color_mismatch' in {r['reason'] for r in stages['excluded_candidates']}


def test_task_and_binding_pending_attribution_never_enter_official_results():
    for scope in ['task','binding']:
        doc=bound_document()
        (doc if scope=='task' else doc['all_binding_snapshots'][0])['attribution_status']='needs_review'
        doc['readings']=build_readings(doc)
        assert doc['readings']==build_records(doc)==candidates([doc])==[]


def test_review_cannot_grant_an_unbound_instrument_historical_occupancy():
    from photo_measurements import field_binding_issue, blockers
    doc=bound_document();doc['readings']=build_readings(doc)
    fields=candidates([doc])
    group={'status':'draft','camera_id':'camera','context':{'experiment_context_ref':'experiment://isolation','instrument_ids':[]},
        'fields':fields,'sources':[{'capture_id':'frame','job_id':'task','captured_at':doc['external_photo']['captured_at'],
            'status':'completed','binding_snapshots':doc['all_binding_snapshots']}]}
    assert field_binding_issue(group,fields[0]) is None
    fields[0].update(instrument={'id':'unbound'},corrected=True)
    assert field_binding_issue(group,fields[0])=='historical_binding_missing'
    assert any('采集时有效绑定' in issue for issue in blockers(group))


def test_partial_stirrer_review_does_not_claim_complete_panel():
    from photo_measurements import instrument_completeness
    doc=bound_document();instrument={'id':'stirrer'}
    doc['all_binding_snapshots'][0]['instrument']=instrument
    region=doc['panel_regions'][0]
    region.update(instrument_id='stirrer',instrument=instrument,class_id=0,measurement_name='温度')
    doc['lines']=[{'panel_id':'mass','text':'63 °C'}];doc['readings']=build_readings(doc)
    group={'camera_id':'camera','fields':candidates([doc]),'sources':[{'capture_id':'frame',
        'captured_at':doc['external_photo']['captured_at'],'binding_snapshots':doc['all_binding_snapshots'],
        'panel_regions':doc['panel_regions']}]}
    assert instrument_completeness(group)==[{'instrument_id':'stirrer','expected_fields':['温度','转速'],
        'missing_fields':['转速'],'status':'incomplete'}]


def test_same_burst_across_handoff_keeps_candidates_without_voting_for_first_person():
    before=bound_document();before['readings']=build_readings(before)
    after=copy.deepcopy(before)
    after.update(job_id='task-after',capture_id='frame-after')
    after['external_photo']['captured_at']='2026-09-20T03:00:00+00:00'
    after['all_binding_snapshots'][0].update(binding_id='session-b',operator='乙',wearer_id='wearer-b',
        started_at='2026-09-20T02:00:00+00:00',ended_at='2026-09-20T04:00:00+00:00')
    after['panel_regions'][0]['binding_id']='session-b';after['readings']=build_readings(after)
    field=candidates([before,after])[0]
    assert field['value'] is None and field['agreement']['matching_photos']==0
    assert '跨使用时段或实际人员的读数不能合并' in field['recognition_issues']
    assert [r['operator'] for r in field['original_candidates']]==['甲','乙']


def test_single_active_binding_cannot_claim_whole_photo_background_digits():
    doc=bound_document()
    doc.pop('panel_detection');doc.pop('panel_regions')
    doc['instrument']=doc['all_binding_snapshots'][0]['instrument'];doc['binding_id']='session-a'
    doc['lines']=[{'text':'999.9999 g'}]
    reading=build_readings(doc)[0]
    assert reading['instrument'] is reading['binding_id'] is None
    assert reading['association_basis']=='unlocalized'
    assert build_records(doc)==[]


def test_read_only_receipt_audit_reports_allowed_mismatch_without_claiming_accuracy():
    from scripts.audit_recognition_evidence import audit
    doc=bound_document();doc['panel_detection']['allowed_instrument_ids']=['stirrer']
    report=audit({'group_id':'group','observations':[doc]})
    assert report['task_count']==1 and report['group_id']=='group'
    assert report['tasks'][0]['allowed_matches_snapshot'] is False
    assert report['real_image_accuracy']=='NOT_PROVEN'
