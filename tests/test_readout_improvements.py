import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
import subprocess
import time
import uuid

import pytest
from fastapi import HTTPException
from PIL import Image

from test_demo import app_client, register, scan  # noqa: F401
from test_photo_measurements import setup, enqueue, complete, get_draft  # noqa: F401
from test_archive_store import archive  # noqa: F401
from test_measurement_records import document
from measurement_records import build_records, display_fields
from photo_watch import SavedPhotoWatcher
from reading_results import build_readings
from panel_readout import save


def test_registered_limits_precision_and_ui_export_share_values_without_guessing():
    doc = document()
    spec = doc['panel_regions'][-1]['instrument']
    spec['measurement_ranges'] = {'质量':{'unit':'g','range':[0,220],'decimal_places':4}}
    doc['lines'][-1]['text']='115071 g'
    doc['readings']=build_readings(doc)
    assert set(doc['readings'][-1]['quality_issues'])=={'outside_registered_range','display_precision_mismatch'}
    assert build_records(doc)[1]['record']['values'][0]['value'] is None
    field=display_fields(doc)[-1]
    assert field['value'] is None and field['raw_text']=='115071 g'
    doc['lines'][-1]['text']='115.0710 g'
    doc['readings']=build_readings(doc)
    assert build_records(doc)[1]['record']['values'][0]['value']==115.071
    assert display_fields(doc)[-1]['value']==115.071
    doc['lines'][0]['text']='281'
    doc['readings']=build_readings(doc)
    assert display_fields(doc)[0]['value'] is None
    assert doc['lines'][0]['text']=='281'  # original remains intact


def test_zero_and_off_remain_distinct():
    doc=document();doc['lines'][0].update(text='0',value=0)
    doc['panel_regions'][1]['display_state']={'text':'OFF'}
    doc['lines'].pop(1);doc['readings']=build_readings(doc)
    fields=display_fields(doc)
    assert fields[0]['value']==0
    assert fields[1]['value'] is None and fields[1]['display_state']=='OFF'


def make_watch(app, client, tmp_path, monkeypatch):
    root=tmp_path/'voice_photos';root.mkdir()
    monkeypatch.setenv('FIELD_SAVED_PHOTO_ROOT',str(root))
    monkeypatch.setenv('FIELD_CAMERA_ID','TestCamera')
    monkeypatch.setenv('FIELD_SAVED_PHOTO_WATCH_ENABLED','1')
    client.put('/api/automation',json={'enabled':False,'operator':'Reader'})
    queued=[];monkeypatch.setattr(app.readout_pool,'submit',lambda fn,job:queued.append(job))
    clock=[0.0];watcher=SavedPhotoWatcher(vars(app),clock=lambda:clock[0]);watcher.step()
    stamp=datetime.now(timezone(timedelta(hours=8)))+timedelta(seconds=1)
    folder=root/'TestCamera'/stamp.strftime('%Y-%m-%d/%H-%M-%S');folder.mkdir(parents=True)
    return root,folder,stamp,queued,clock,watcher


def write_photo(folder,stamp,suffix='',color='gray'):
    path=folder/(stamp.strftime('%Y%m%d_%H%M%S')+suffix+'.jpg')
    Image.new('RGB',(400,300),color).save(path)
    return path


def test_nas_receipt_real_watch_groups_three_photos_and_retransmission(app_client,tmp_path,monkeypatch):
    app,client=app_client
    root,folder,stamp,queued,clock,watcher=make_watch(app,client,tmp_path,monkeypatch)
    files=[write_photo(folder,stamp,'_'+str(i),color) for i,color in enumerate(('red','blue','green'))]
    receipt={'schema_version':'field-photo-receipt/1','camera_id':'TestCamera',
        'measurement':{'burst_id':'actual-source-id','expected_photos':3,'experiment_context_ref':'experiment://1'},
        'photos':[{'filename':p.name,'capture_id':'camera-photo-'+str(i),'captured_at':stamp.replace(microsecond=0).isoformat(),
                   'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for i,p in enumerate(files)]}
    (folder/'PhotoReceipt.json').write_text(json.dumps(receipt))
    watcher.step();clock[0]=2
    for _ in files:watcher.step()
    assert len(queued)==3 and len({j['measurement_id'] for j in queued})==1
    mid=queued[0]['measurement_id']
    assert len(get_draft(client,mid)['job_ids'])==3
    assert all(j['timing']['source_receipt']['measurement']['burst_id']=='actual-source-id' for j in queued)
    watcher.close()
    restarted=SavedPhotoWatcher(vars(app),clock=lambda:clock[0]);restarted.step();clock[0]=4;restarted.step()
    assert len(queued)==3
    body={'image_path':str(files[0])}
    assert client.post('/api/ocr/photo-result',json=body).json()['measurement_id']==mid
    # The raw image and the exact manifest remain unchanged.
    assert json.loads((folder/'PhotoReceipt.json').read_text())==receipt
    broken=receipt|{'camera_id':'AnotherCamera'};(folder/'PhotoReceipt.json').write_text(json.dumps(broken))
    assert client.post('/api/ocr/photo-result',json=body).status_code==409


def test_new_photos_prioritized_backlog_gets_a_slot_bad_file_is_isolated(app_client,tmp_path,monkeypatch):
    app,client=app_client
    root,folder,stamp,queued,clock,watcher=make_watch(app,client,tmp_path,monkeypatch)
    old=write_photo(folder,stamp,'_old')  # rename to a valid source filename
    old.rename(old.with_name(stamp.strftime('%Y%m%d_%H%M%S_0.jpg')));old=old.with_name(stamp.strftime('%Y%m%d_%H%M%S_0.jpg'))
    os.utime(old,(time.time()-100,time.time()-100))
    fresh=[write_photo(folder,stamp,'_'+str(i)) for i in range(1,6)]
    watcher.discover(recent=False);clock[0]=2;watcher.discover(recent=False)
    submitted=[]
    def read(body,**kwargs):
        submitted.append(body.image_path)
        if body.image_path.endswith('_0.jpg'):raise HTTPException(503,'temporary I/O error')
        return {'job_id':str(uuid.uuid4())}
    monkeypatch.setitem(watcher.core,'read_saved_panel',read)
    for _ in range(5):watcher.ingest()
    assert all(not p.endswith('_0.jpg') for p in submitted[:3])
    assert submitted[3].endswith('_0.jpg') and not submitted[4].endswith('_0.jpg')
    assert watcher.snapshot()['retrying_files']==1
    again=SavedPhotoWatcher(vars(app),clock=lambda:clock[0])
    assert again.snapshot()['retrying_files']==1
    with app.db() as conn:
        pending=json.loads(conn.execute('SELECT document FROM photo_ingest_queue WHERE path=?',(submitted[3],)).fetchone()[0])
    assert pending['attempts']==1 and pending['last_error']=='temporary I/O error'


def test_nas_read_timeout_is_bounded_and_following_file_can_be_read(tmp_path):
    from source_io import call,SourceError
    # FIFO forces a blocked metadata read in the isolated process.
    root=tmp_path/'voice_photos';folder=root/'Cam/2026-09-15/10-00-00';folder.mkdir(parents=True)
    path=folder/'20260915_100000.jpg';Image.new('RGB',(32,32)).save(path)
    os.mkfifo(folder/'PhotoReceipt.json')
    # FIFO is rejected as non-regular before any blocking read.
    with pytest.raises(SourceError):
        call({'action':'read','root':str(root),'path':str(path),'camera':'Cam','max_bytes':10000},timeout=.5)
    (folder/'PhotoReceipt.json').unlink()
    assert call({'action':'read','root':str(root),'path':str(path),'camera':'Cam','max_bytes':10000})['receipt'] is None


def reprocess(client,jid,**overrides):
    body={'request_id':str(uuid.uuid4()),'actor':'Reader','reason':'更新数字区域规则'}|overrides
    response=client.post('/api/jobs/'+jid+'/reprocess',json=body)
    assert response.status_code==202,response.text
    return response.json(),body


def test_completed_reprocess_keeps_raw_identity_idempotence_and_one_archive(setup,archive):
    app,client,binding,queued=setup
    root=archive[2]
    reply,_=enqueue(setup,1);original=complete(setup,0,'12');jid=original['job_id'];mid=reply['measurement_id']
    before=app.get_job(jid)
    app.archive_store.step(100)
    new,body=reprocess(client,jid)
    assert new['capture_id']==original['capture_id'] and new['all_binding_snapshots']==original['all_binding_snapshots']
    assert new['measurement_id']==mid and new['recognition_revision']==2 and new['timing']['is_reprocessing']
    assert 'lines' not in new and 'local_ocr' not in new
    assert client.post('/api/jobs/'+jid+'/reprocess',json=body).json()['job_id']==new['job_id']
    assert client.post('/api/jobs/'+jid+'/reprocess',json=body|{'reason':'different'}).status_code==409
    complete(setup,1,'72')
    after=app.get_job(jid)
    assert before['lines']==after['lines'] and after['lines'][0]['text']=='12'
    assert get_draft(client,mid)['fields'][0]['value']==72
    versions=client.get('/api/jobs/'+jid+'/versions').json()
    assert versions['current_job_id']==new['job_id'] and len(versions['items'])==2
    app.archive_store.step(100)
    bundles=list(root.glob('*/VoicePhotoReadings/*/Evidence.json'));assert len(bundles)==1
    result=json.loads(bundles[0].read_text())
    assert len(result['sources'])==1 and len(result['observations'])==2
    assert json.loads((root/result['measurement_files'][0]['result']).read_text())['values'][0]['value']==72
    assert app.archive_store.integrity.run_once()['issue_count']==0


def test_confirmed_reprocessing_requires_reconfirmation_retains_old_record(setup,archive):
    app,client,binding,queued=setup
    reply,_=enqueue(setup,1);old=complete(setup,0,'12');mid=reply['measurement_id']
    draft=get_draft(client,mid)
    first=client.post(f'/api/photo-measurements/{mid}/confirm',json={'actor':'Reader','revision':draft['revision']}).json()
    new,_=reprocess(client,old['job_id'])
    assert get_draft(client,mid)['status']=='collecting'
    assert client.get('/api/experiment-records/'+first['record_id']).json()==first
    assert client.get('/api/experiment-records').json()['items']==[first]
    complete(setup,1,'72');draft=get_draft(client,mid)
    assert draft['status']=='draft' and not draft['blockers']
    body={'actor':'Reader','revision':draft['revision']}
    response=client.post(f'/api/photo-measurements/{mid}/confirm',json=body)
    assert response.status_code==200,response.text
    second=response.json()
    assert second['record_id']!=first['record_id'] and second['supersedes_record_id']==first['record_id']
    assert client.get('/api/experiment-records/'+first['record_id']).json()==first
    assert client.post(f'/api/photo-measurements/{mid}/confirm',json=body).json()==second
    assert client.get('/api/experiment-records').json()['items']==[second]
    app.archive_store.step(200)
    assert len(list(archive[2].glob('*/VoicePhotoReadings/*/Evidence.json')))==1


def test_child_timeout_returns_promptly_and_does_not_block_next_request(tmp_path,monkeypatch):
    import source_io,sys
    real_popen=source_io.subprocess.Popen
    def stuck(*args,**kwargs):
        return real_popen([sys.executable,'-c','import time; time.sleep(30)'],**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(source_io.subprocess,'Popen',stuck)
        began=time.monotonic()
        with pytest.raises(source_io.SourceError,match='超时'):
            source_io.call({'action':'discover'},timeout=.1)
        assert time.monotonic()-began < 2
    root=tmp_path/'voice_photos';root.mkdir()
    result=source_io.call({'action':'discover','root':str(root),'camera':'Cam','zone':'Asia/Shanghai','since':'2026-09-15T00:00:00+08:00'})
    assert result['files']==[]


def test_failed_revision_preserves_completed_result_and_can_be_replaced(setup):
    app,client,_,queued=setup
    reply,_=enqueue(setup,1);old=complete(setup,0,'51')
    new,_=reprocess(client,old['job_id'])
    failed=copy.deepcopy(queued[1]);failed.update(status='failed',error='fixture-error',finished_at=app.now())
    save(vars(app),failed)
    assert client.get('/api/jobs/'+old['job_id']+'/versions').json()['current_job_id']==old['job_id']
    replacement,_=reprocess(client,old['job_id'])
    assert replacement['recognition_revision']==3
    assert get_draft(client,reply['measurement_id'])['job_ids']==[replacement['job_id']]
    complete(setup,2,'72')
    assert client.get('/api/jobs/'+old['job_id']+'/versions').json()['current_job_id']==replacement['job_id']


def test_failed_original_gets_new_version_without_losing_failure_or_attribution(setup):
    app,client,_,queued=setup
    reply,_=enqueue(setup,1)
    failed=copy.deepcopy(queued[0])
    failed.update(status='failed',error='ImportError',finished_at=app.now(),
                  attribution_status='needs_review',attribution_reason='capture_time_unverified',
                  allowed_instrument_ids=[],binding_snapshot_version='binding-snapshot/2')
    save(vars(app),failed)
    original=client.get('/api/jobs/'+failed['job_id']).json()
    new,body=reprocess(client,failed['job_id'])
    assert new['recognition_revision']==2 and new['job_id']!=failed['job_id']
    assert new['attribution_status']=='needs_review'
    assert new['attribution_reason']=='capture_time_unverified'
    assert new['allowed_instrument_ids']==[]
    assert new['binding_snapshot_version']=='binding-snapshot/2'
    assert 'error' not in new and 'finished_at' not in new
    assert client.get('/api/jobs/'+failed['job_id']).json()==original
    assert client.post('/api/jobs/'+failed['job_id']+'/reprocess',json=body).json()['job_id']==new['job_id']
