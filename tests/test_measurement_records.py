import copy
import hashlib
import json

import numpy as np
import pytest

from measurement_records import build_records, photo_time
from reading_results import build_readings
from test_demo import app_client, scan, register  # noqa: F401


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
                  {'name':'转速','value':250,'unit':'rpm','range':[None,None]}]}
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
