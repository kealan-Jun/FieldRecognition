import copy
import hashlib
import json

import numpy as np
import pytest

from measurement_records import build_records, photo_time
from reading_results import build_readings
from test_demo import app_client, scan, register  # noqa: F401


def test_explicit_fixed_decimal_rule_keeps_raw_digits_and_normalization_evidence():
    from panel_regions import validate_lines, reading_fallback_reason
    from measurement_records import apply_fixed_decimal
    instrument={'id':'b','measurement_ranges':{'质量':{'decimal_places':4,'fixed_decimal_display':True}}}
    for raw,expected in [('02181','0.2181'),('02183','0.2183'),('-02181','-0.2181'),('1115071','111.5071'),('130300','13.0300'),('139330','13.9330')]:
        line=validate_lines([{'text':raw,'confidence':.9}],'质量',instrument)[0]
        assert line['text']==raw and line['value']==expected and line['quality_issue'] is None
        assert line['normalized_value']['value']==float(expected)
        assert line['normalization']['raw_text']==raw and line['normalization']['instrument_id']=='b'
        assert line['normalized_value']['unit'] is None  # Precision never supplies a unit.
        assert reading_fallback_reason([line]) is None
    for raw in ['0218','002181','2181','0.2181','02181 g','0218I']:
        assert apply_fixed_decimal('质量',{'text':raw},instrument)=={'text':raw}
    instrument['measurement_ranges']['质量'].pop('fixed_decimal_display')
    line=validate_lines([{'text':'02181'}],'质量',instrument)[0]
    assert line['normalized_value']['value'] is None
    assert reading_fallback_reason([line]) == 'decimal_uncertain'


def test_fixed_decimal_provenance_survives_reading_and_six_field_export():
    from panel_regions import validate_lines
    doc=document();region=doc['panel_regions'][-1]
    region['instrument']['measurement_ranges']={'质量':{'decimal_places':4,'fixed_decimal_display':True}}
    doc['lines']=validate_lines([{'text':'02181','panel_id':region['panel_id']}],'质量',region['instrument'])
    doc['readings']=build_readings(doc)
    result=next(r for r in build_records(doc) if r['instrument_id']=='b')
    assert result['record']['values'][0]['value']==.2181
    assert result['evidence']['candidate_readings'][0]['normalization']['method']=='registered_fixed_decimal_v2'


def test_fixed_decimal_registration_requires_positive_explicit_precision(app_client):
    app,_=app_client
    with pytest.raises(ValueError):app.MeasurementRange(fixed_decimal_display=True)
    with pytest.raises(ValueError):app.MeasurementRange(fixed_decimal_display=True,decimal_places=0)
    assert app.MeasurementRange(fixed_decimal_display=True,decimal_places=4).decimal_places==4


def document():
    a = {'id':'a','model':'MS-H280-Pro','device_no':'0007',
         'measurement_ranges':{'温度':{'unit':'°C','range':[None,280]}}}
    b = {'id':'b','model':'','device_no':None}
    doc = {'job_id':'job','capture_id':'photo','status':'completed','image_url':'/api/images/photo',
        'image_sha256':'a'*64,'operator':'徐荣炜','device':'gpu:0',
        'external_photo':{'captured_at':'2026-09-14T05:43:10.123456+00:00'},
        'all_binding_snapshots':[{'binding_id':'ba','instrument':a,'wearer_id':'wearer-7','qr_hash':'b'*64,'scan_id':'scan-a'},
                                 {'binding_id':'bb','instrument':b}],
        'panel_regions':[], 'lines':[],'panel_detection':{'status':'completed'}}
    for instrument, cls, binding, names, texts in [(a,0,'ba',['温度','转速'],['179','250']), (b,1,'bb',['质量'],['-32.4223 g'])]:
        for index,(name,text) in enumerate(zip(names,texts)):
            pid=f'panel-{cls}-{index}'
            doc['panel_regions'].append({'panel_id':pid,'class_id':cls,'instrument':instrument,'instrument_id':instrument['id'],
                'binding_id':binding,'association_basis':'panel_detector_session_binding','measurement_name':name})
            doc['lines'].append({'panel_id':pid,'text':text})
    doc['readings']=build_readings(doc)
    return doc


def test_exact_user_schema_separates_two_devices_without_fabricating_metadata():
    doc=document(); original=copy.deepcopy(doc)
    a,b=build_records(doc)
    assert a['record']=={'wearer_id':'wearer-7','device_model':'MS-H280-Pro','device_no':'0007','qr_hash':'b'*64,
        'photo_time':{'timestamp_ms':1789364590123,'time':'2026-09-14 13:43:10.123'},
        'values':[{'name':'温度','value':179,'unit':'°C','range':[None,280]},
                  {'name':'转速','value':250,'unit':None,'range':[None,None]}]}
    assert b['record']['wearer_id'] is b['record']['device_model'] is b['record']['device_no'] is b['record']['qr_hash'] is None
    assert b['record']['values']==[{'name':'质量','value':-32.4223,'unit':'g','range':[None,None]}]
    assert a['evidence']['capture_id']==b['evidence']['capture_id']=='photo'
    assert a['evidence']['qr_basis']=='binding_decode'
    assert doc==original


@pytest.mark.parametrize('captured_at',[None,'2026-09-14T13:43:10','bad'])
def test_unknown_or_naive_capture_time_is_not_replaced_by_receipt_time(captured_at):
    assert photo_time({'external_photo':{'captured_at':captured_at},'submitted_at':'2026-09-14T05:43:10Z',
                       'video_observation':{'observed_at':'2026-09-14T05:43:10Z'}})=={'timestamp_ms':None,'time':None}


def test_beijing_and_utc_are_same_epoch_without_extra_eight_hours():
    assert photo_time({'external_photo':{'captured_at':'2026-09-14T13:43:10.123+08:00'}})==photo_time(document())


def test_video_clock_requires_synchronization_and_retains_millisecond_precision():
    from measurement_records import video_clock
    metadata = {'global_timestamp_us':1789364590123456, 'clock_sync_valid':True}
    assert photo_time({'video_observation':video_clock(metadata)}) == photo_time(document())
    assert photo_time({'video_observation':{'observed_at':'2026-09-14T13:44:00+08:00'},
                       'frame_metadata':metadata}) == photo_time(document())
    for valid in (False, None, 1):
        value = video_clock(metadata | {'clock_sync_valid':valid})
        assert photo_time({'video_observation':value})['timestamp_ms'] is None
    assert video_clock(metadata | {'global_timestamp_us':10**30})['captured_at'] is None


def test_explicit_unit_conflict_is_not_silently_replaced_and_registry_is_evidence():
    doc = document()
    doc['readings'][0]['unit'] = 'rpm'
    entry = build_records(doc)[0]
    assert entry['record']['values'][0] == {'name':'温度','value':None,'unit':None,'range':[None,None]}
    assert entry['evidence']['unit_basis']['温度'] == 'unit_conflict'
    doc['readings'][0]['unit'] = None
    assert build_records(doc)[0]['evidence']['unit_basis']['温度'] == 'instrument_registry'
    doc['readings'][-1]['unit'] = 'kg'
    doc['panel_regions'][-1]['instrument']['measurement_ranges'] = {'质量':{'unit':'g','range':[0,100]}}
    assert build_records(doc)[1]['record']['values'][0]['value'] is None


@pytest.mark.parametrize('text',['00000','02287','888888.8'])
def test_ambiguous_decimal_and_self_test_are_null_and_raw_evidence_survives(text):
    doc=document();doc['lines'][-1]['text']=text;doc['readings']=build_readings(doc)
    record=build_records(doc)[1]
    assert record['record']['values'][0]['value'] is None
    assert record['evidence']['candidate_readings'][0]['text']==text
    assert record['record']['values'][0]['unit'] is None


def test_single_unknown_stirrer_window_does_not_guess_temperature_or_speed():
    doc=document(); doc['panel_regions']=doc['panel_regions'][:1]
    doc['panel_regions'][0]['measurement_name']=None
    doc['lines']=doc['lines'][:1];doc['readings']=build_readings(doc)
    assert [v['value'] for v in build_records(doc)[0]['record']['values']]==[None,None]
    doc['panel_regions'][0]['binding_id']=None
    assert build_records(doc)==[]


def test_stirrer_off_temperature_still_exports_temperature_and_speed():
    instrument={'id':'stirrer','model':'','measurement_ranges':{
        '温度':{'unit':'°C','range':[None,None]},
        '转速':{'unit':'rpm','range':[None,None]}}}
    doc={'job_id':'job-off','capture_id':'photo-off','status':'completed',
         'all_binding_snapshots':[{'binding_id':'binding','instrument':instrument}],
         'panel_regions':[{'panel_id':'temp','class_id':0,'instrument':instrument,
                           'instrument_id':'stirrer','binding_id':'binding','measurement_name':'温度',
                           'display_state':{'text':'OFF'}},
                          {'panel_id':'speed','class_id':0,'instrument':instrument,
                           'instrument_id':'stirrer','binding_id':'binding','measurement_name':'转速'}],
         'lines':[{'panel_id':'speed','text':'1140','value':'1140','unit':'rpm'}],
         'readings':[{'panel_id':'speed','measurement_name':'转速','text':'1140','value':'1140','unit':'rpm',
                      'instrument':instrument}],
         'qr_matches':[]}
    records=build_records(doc)
    assert len(records)==1
    assert records[0]['record']['values']==[
        {'name':'温度','value':None,'unit':'°C','range':[None,None],'display_state':'OFF'},
        {'name':'转速','value':1140,'unit':'rpm','range':[None,None]}]

    from archive_events import standard_records
    assert standard_records([{'seq': 1, 'doc': doc}], None)[0]['record'] == records[0]['record']
    other = copy.deepcopy(doc)
    other.update(job_id='job-on', capture_id='photo-on')
    other['panel_regions'][0].pop('display_state')
    # An unreadable frame cannot corroborate OFF; retain the conflicting evidence.
    combined = standard_records([{'seq': 1, 'doc': doc}, {'seq': 2, 'doc': other}], None)[0]
    assert combined['evidence']['conflicting_fields'] == ['温度']
    assert 'display_state' not in combined['record']['values'][0]
    corrected = {'fields': [{'instrument': instrument, 'name': '温度', 'value': '72', 'unit': '°C', 'corrected': True}]}
    revised = standard_records([{'seq': 1, 'doc': doc}], corrected)[0]
    assert revised['record']['values'][0]['value'] == 72
    assert 'display_state' not in revised['record']['values'][0]


def test_exact_decoded_payload_hash_and_optional_registration_survive_old_form_updates(app_client):
    app,client=app_client
    capture=scan(client);iid=capture['matches'][0]['id'];register(client,iid)
    raw=json.dumps({'type':'instrument','id':iid,'v':1},ensure_ascii=False,indent=2)
    hit=app.qr_matches([raw],np.zeros((1,4,2)))['matches'][0]
    assert hit['qr_hash']==hashlib.sha256(raw.encode()).hexdigest()
    assert hit['qr_hash']!=hashlib.sha256(json.dumps(json.loads(raw),separators=(',',':')).encode()).hexdigest()
    data={'name':'A','scene':'湿实验实验台','model':'MS-H280-Pro','device_no':'0007',
          'measurement_ranges':{'温度':{'unit':'°C','range':[None,280]}}}
    assert client.put('/api/instruments/'+iid,json=data).status_code==200
    updated=client.put('/api/instruments/'+iid,json={'name':'A renamed','scene':'湿实验实验台','model':'MS-H280-Pro'}).json()
    assert updated['device_no']=='0007' and updated['measurement_ranges']==data['measurement_ranges']
    binding=client.post('/api/bindings',json={'scan_id':capture['scan_id'],'instrument_id':iid,'operator':'徐荣炜','wearer_id':'wearer-7'}).json()
    assert binding['qr_hash']==capture['matches'][0]['qr_hash']
    assert binding['wearer_id']=='wearer-7' and binding['instrument']['device_no']=='0007'
    data['measurement_ranges']['温度']['range']=[10,1]
    assert client.put('/api/instruments/'+iid,json=data).status_code==422
    data['device_no']=7
    assert client.put('/api/instruments/'+iid,json=data).status_code==422


def test_archive_writes_one_exact_measurement_file_per_instrument_with_shared_photo():
    from archive_browse import build_views
    doc=document();doc.update(camera_id='cam',submitted_at='2026-09-14T05:43:10Z',
        measurement_records=build_records(doc))
    for region in doc['panel_regions']:
        region['instrument']['name']=region['instrument']['id']
    capture={'capture_id':'photo','camera_id':'cam','image_sha256':'a'*64,'received_at':doc['submitted_at']}
    rows=[{'entity':entity,'entity_id':ident,'seq':seq,'document':json.dumps(data),
           'recorded_at':doc['submitted_at'],'receipt_path':f'Receipts/{seq}.json'}
          for seq,(entity,ident,data) in enumerate([('scans','photo',capture),('jobs','job',doc)],1)]
    views=build_views(rows)
    index=json.loads(views['Records/Jobs/job/Record.json'])
    for iid in ('a','b'):
        record=index['instrument_measurements'][iid]
        assert set(record)=={'wearer_id','device_model','device_no','qr_hash','photo_time','values'}
    assert index['photos']['image']['sha256']=='a'*64
    assert len([p for p in views if p.endswith('/Record.json') and '/Jobs/' in p])==1


def test_fixed_decimal_cloud_output_keeps_unit_and_conflict_checks():
    from panel_regions import validate_lines
    instrument={'id':'b','measurement_ranges':{'质量':{'decimal_places':4,'fixed_decimal_display':True,'unit':'g'}}}
    line=validate_lines([{'text':'130300','value':'130300','unit':'g','reading_source':'vision'}],'质量',instrument)[0]
    assert line['value']=='13.0300' and line['text']=='130300'
    assert line['normalized_value']['value']==13.03 and line['normalized_value']['unit']=='g'
    conflict=validate_lines([{'text':'130300','value':'130300','unit':'kg'}],'质量',instrument)[0]
    assert conflict['normalized_value']['value'] is None and conflict['quality_issue']=='unit_conflict'
    explicit=validate_lines([{'text':'8.141','value':'8.141'}],'质量',instrument)[0]
    assert explicit['normalized_value']['value'] is None and explicit['quality_issue']=='display_precision_mismatch'
