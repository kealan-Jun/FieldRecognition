"""Regression tests for real transaction, lease, auth and offline storage boundaries."""
import importlib
import json
import sqlite3
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from database import Database,apply_migrations,get_migration_status
from task_queue import TaskQueue,LeaseLost,QueueFull,utc
from auth import AuthService

@pytest.fixture
def database(tmp_path):
    db=Database(tmp_path/'State.sqlite3');apply_migrations(db);return db


def test_commit_rollback_close_and_dry_run(tmp_path):
    db=Database(tmp_path/'Missing'/'State.sqlite3')
    assert get_migration_status(db)['applied_count']==0
    assert apply_migrations(db,dry_run=True)
    assert not db.path.parent.exists()
    apply_migrations(db)
    with db.connection() as conn:conn.execute('CREATE TABLE example(value INTEGER)')
    with pytest.raises(RuntimeError):
        with db.connection() as conn:conn.execute('INSERT INTO example VALUES(1)');raise RuntimeError()
    with db.connection() as conn:assert conn.execute('SELECT count(*) FROM example').fetchone()[0]==0
    with pytest.raises(sqlite3.ProgrammingError):conn.execute('SELECT 1')
    with db.connection() as conn:conn.execute('INSERT INTO example VALUES(2)')
    other=sqlite3.connect(db.path)
    try:assert other.execute('SELECT value FROM example').fetchone()[0]==2
    finally:other.close()


def test_old_schema_migrates_without_losing_jobs_or_receipts(tmp_path):
    db=Database(tmp_path/'State.sqlite3')
    with db.connection() as c:
        c.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT,document TEXT)')
        c.execute('INSERT INTO jobs VALUES(?,?,?)',('old','queued','{"job_id":"old"}'))
        c.execute('CREATE TABLE archive_outbox(entity TEXT,entity_id TEXT,archived_at TEXT,receipt_path TEXT)')
        c.execute("INSERT INTO archive_outbox VALUES('jobs','old','original-time','original-path')")
    apply_migrations(db)
    with db.connection() as c:
        assert c.execute('SELECT document FROM jobs').fetchone()[0]=='{"job_id":"old"}'
        assert tuple(c.execute('SELECT * FROM archive_outbox_prototype_backup').fetchone())==('jobs','old','original-time','original-path')
    assert TaskQueue(db).claim_task()['job_id']=='old'
    assert apply_migrations(db)==[]


def test_claim_is_atomic_and_old_worker_cannot_clear_new_lease(database):
    a,b=TaskQueue(database,'same-name'),TaskQueue(database,'same-name')
    a.enqueue({'job_id':'job','camera_id':'cam'})
    with ThreadPoolExecutor(2) as pool:claims=list(pool.map(lambda q:q.claim_task(),[a,b]))
    assert sum(c is not None for c in claims)==1
    old,new=(a,b) if claims[0] else (b,a)
    old_document=next(c for c in claims if c)
    with database.connection() as c:c.execute('UPDATE jobs SET lease_expires_at=?',(utc(time.time()-1),))
    fresh=new.claim_task()
    assert fresh['lease_holder']!=old_document['lease_holder']
    with pytest.raises(LeaseLost):old.fail_task('job','late error')
    with pytest.raises(LeaseLost):old.publish(old_document|{'status':'completed'})
    assert new.renew_lease('job')
    new.complete_task('job',{'value':12})
    with database.connection() as c:assert c.execute('SELECT status FROM jobs').fetchone()[0]=='completed'


def test_backoff_survives_restart_and_exhaustion_is_terminal(database):
    q=TaskQueue(database);q.enqueue({'job_id':'job','camera_id':'cam'})
    with database.connection() as c:c.execute('UPDATE jobs SET max_retries=1')
    q.claim_task();q.fail_task('job','retryable')
    restarted=TaskQueue(database)
    assert restarted.claim_task() is None
    with database.connection() as c:c.execute('UPDATE jobs SET next_attempt_at=0')
    assert restarted.claim_task()['attempt_count']==2
    restarted.fail_task('job','again')
    assert restarted.claim_task() is None
    with database.connection() as c:
        row=c.execute('SELECT status,document FROM jobs').fetchone()
        assert row['status']=='failed' and json.loads(row['document'])['phase']=='dead_letter'


def test_queue_backpressure_and_camera_fairness(database):
    q=TaskQueue(database)
    for ident,cam in [('a1','a'),('a2','a'),('b1','b')]:q.enqueue({'job_id':ident,'camera_id':cam})
    with pytest.raises(QueueFull):q.enqueue({'job_id':'a3','camera_id':'a'},per_camera=2)
    first=q.claim_task();q.complete_task(first['job_id'],{})
    assert q.claim_task()['camera_id']!=first['camera_id']

@pytest.fixture
def managed(tmp_path,monkeypatch):
    for key,value in {'FIELD_DEMO_DATA':str(tmp_path),'FIELD_DATABASE_PATH':str(tmp_path/'Demo.sqlite3'),
        'FIELD_PRODUCTION_ENABLED':'1','FIELD_SERVICE_ROLE':'api','FIELD_RECORD_MODE':'test','FIELD_CAMERA_ID':'cam-a',
        'FIELD_ALIYUN_FALLBACK_ENABLED':'0','FIELD_ARCHIVE_ENABLED':'0','FIELD_SAVED_PHOTO_WATCH_ENABLED':'0','FIELD_VIDEO_OCR_ENABLED':'0','FIELD_PANEL_DETECTOR_ENABLED':'0'}.items():monkeypatch.setenv(key,value)
    monkeypatch.delenv('FIELD_RECEIVER_URL',raising=False)
    db=Database(tmp_path/'Demo.sqlite3');apply_migrations(db)
    auth=AuthService(db);admin=auth.create_initial_admin('admin','管理员','correct horse battery')
    operator=auth.create_user('alice','甲','operator','test-password',admin.id)
    with db.connection() as c:c.execute('INSERT INTO camera_users VALUES(?,?)',('cam-a',operator.id))
    sys.modules.pop('app',None);module=importlib.import_module('app')
    with TestClient(module.app) as client:yield module,client,auth,admin,operator
    sys.modules.pop('app',None)


def test_real_routes_and_agent_aliases_require_auth(managed):
    app,client,auth,admin,operator=managed
    for path in ('/api/state','/api/v1/state','/api/tools','/api/admin/status'):
        assert client.get(path).status_code==401
    token,_=auth.login('alice','test-password');headers={'Authorization':'Bearer '+token.id}
    assert client.get('/api/state',headers=headers).status_code==200
    assert client.get('/api/v1/state',headers=headers).status_code==200
    assert client.get('/api/state?camera_id=cam-b',headers=headers).status_code==403
    assert client.get('/api/admin/status',headers=headers).status_code==403
    assert client.post('/api/tools/get_field_state',json={},headers=headers).status_code==200
    assert client.post('/api/tools/bind_instrument',json={'scan_id':str(uuid.uuid4()),'instrument_id':str(uuid.uuid4()),'operator':'伪造姓名'},headers=headers).status_code==403
    assert app.ocr_model is None


def test_cookie_csrf_and_foreign_image_access(managed):
    app,client,auth,admin,operator=managed
    response=client.post('/api/auth/login',json={'username':'alice','password':'test-password'})
    assert response.status_code==200
    assert 'httponly' in response.headers['set-cookie'].lower()
    assert client.post('/api/auth/logout',json={}).status_code==403
    foreign=str(uuid.uuid4())
    with app.db() as c:c.execute('INSERT INTO scans(id,document) VALUES(?,?)',(foreign,json.dumps({'capture_id':foreign,'camera_id':'cam-b'})))
    assert client.get('/api/images/'+foreign).status_code==403
    assert client.get('/api/state').json()['last_camera_scan'] is None
    csrf=response.json()['csrf_token']
    assert client.post('/api/auth/logout',json={},headers={'x-csrf-token':csrf}).status_code==200


def test_configured_display_classes_and_ambiguous_type_do_not_guess():
    from panel_layout import normalize_classes,resolve_type_boxes,roles_for_boxes
    config=normalize_classes({'7':{'name':'temperature_speed','layout':'horizontal_pair','measurements':['温度','转速'],'type_id':'stirrer'}})
    boxes=[{'class_id':7,'xyxy':b,**config['7']} for b in ([0,0,20,10],[25,0,45,10])]
    ambiguous=resolve_type_boxes(boxes,['a','b'],lambda _: {'type_id':'stirrer'})
    assert all(b['instrument_id'] is None for b in ambiguous)
    unique=resolve_type_boxes(boxes,['a'],lambda _: {'type_id':'stirrer'})
    assert set(roles_for_boxes(unique).values())=={'温度','转速'}


def test_ipc_roundtrip_is_scoped_and_has_no_network_listener(tmp_path,monkeypatch):
    from runtime_rpc import RpcServer,call
    import numpy as np
    monkeypatch.setenv('FIELD_DEMO_DATA',str(tmp_path))
    image=np.zeros((20,30,3),np.uint8);image[2,3]=[1,2,3]
    server=RpcServer('test',{'identity':lambda frame:frame});server.start()
    try:
        assert np.array_equal(call('test','identity',image),image)
        assert server.path.stat().st_mode & 0o777==0o600
    finally:server.close()
    assert not server.path.exists()


def test_managed_worker_runs_actual_photo_pipeline_and_commits_result(managed,monkeypatch):
    app,client,auth,admin,operator=managed
    from test_unbound_readout import photo
    from ocr_worker import OCRWorker
    request=photo(app);request['camera_id']='cam-a'
    session,_=auth.login('alice','test-password');headers={'Authorization':'Bearer '+session.id}
    job=client.post('/api/ocr/photo-result',json={'photo':request},headers=headers).json()
    assert job['status']=='queued'
    monkeypatch.setattr(app,'predict_readout',lambda *a,**kw:{'status':'completed','lines':[{'text':'12.3 g','confidence':.9}],'device':'cpu'})
    worker=OCRWorker('test',None,vars(app))
    task=worker.queue.claim_task('ocr');worker._process_ocr_task(task)
    result=client.get('/api/jobs/'+job['job_id'],headers=headers).json()
    assert result['status']=='completed' and result['lines'][0]['text']=='12.3 g'
    assert client.post('/api/ocr/photo-result',json={'photo':request},headers=headers).json()['job_id']==job['job_id']
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM imported_photos').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0]==1


def test_capture_process_and_api_share_state_without_api_camera_threads(managed,monkeypatch,tmp_path):
    import os,subprocess
    from runtime_rpc import socket_path,call
    app,client,auth,admin,operator=managed
    env=dict(os.environ,FIELD_SERVICE_ROLE='capture',FIELD_RECEIVER_URL='http://127.0.0.1:9',FIELD_AUTO_RUN_ENABLED='0')
    log=(tmp_path/'capture.log').open('w+')
    proc=subprocess.Popen([sys.executable,'-m','capture_worker'],env=env,stdout=log,stderr=log)
    try:
        deadline=time.monotonic()+10
        while not socket_path('camera:cam-a').exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.05)
        if proc.poll() is not None:
            log.seek(0);pytest.fail(log.read()[-3000:])
        session,_=auth.login('alice','test-password');headers={'Authorization':'Bearer '+session.id}
        result=client.get('/api/state',headers=headers)
        assert result.status_code==200
        assert result.json()['automation']['camera_id']=='cam-a'
        assert client.put('/api/video-ocr',json={'enabled':False},headers=headers).status_code==200
        assert client.put('/api/automation',json={'enabled':False,'operator':'甲'},headers=headers).status_code==200
        from runtime_rpc import CameraProxy
        assert isinstance(app.receiver_camera,CameraProxy)
    finally:
        proc.terminate()
        try:proc.wait(timeout=8)
        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=3)
        log.close()


def test_region_images_and_validation_do_not_leak_foreign_data(managed):
    app,client,auth,admin,operator=managed
    token,_=auth.login('alice','test-password');headers={'Authorization':'Bearer '+token.id}
    job_id=str(uuid.uuid4());region=str(uuid.uuid4())
    with app.db() as conn:
        conn.execute('INSERT INTO jobs(id,status,document) VALUES(?,?,?)',(job_id,'completed',json.dumps({'job_id':job_id,'camera_id':'cam-b','panel_regions':[{'image_url':'/api/images/'+region}]})))
    assert client.get('/api/images/'+region,headers=headers).status_code==403
    reply=client.post('/api/capture-events',headers=headers,json={'metadata':{'secret':'must-not-echo'}})
    assert reply.status_code==422 and 'must-not-echo' not in reply.text
    token,_=auth.login('admin','correct horse battery');headers={'Authorization':'Bearer '+token.id}
    assert client.post('/api/admin/users',headers=headers,json={'username':'oops'}).status_code==422


def test_temporary_ocr_failure_uses_persistent_backoff(database):
    from panel_readout import save
    q=TaskQueue(database);q.enqueue({'job_id':'transient','camera_id':'a','submitted_at':'2026-09-15T00:00:00+00:00'})
    document=q.claim_task();document.update(status='failed',error='local_ocr_timeout')
    save({'task_queue':q},document)
    with database.connection() as conn:row=conn.execute('SELECT * FROM jobs WHERE id=?',('transient',)).fetchone()
    assert row['status']=='queued' and row['next_attempt_at']>time.time()
    assert json.loads(row['document'])['failure_history'][0]['error']=='local_ocr_timeout'


def test_recognized_legacy_views_retire_without_deleting_evidence(tmp_path):
    from archive_layout import retire_generated_views
    old=tmp_path/'Browse/InstrumentReadings/asset/day/job/Record.json'
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({'schema':'field-recognition-browse/1','derived_view':True,'entity':'jobs','entity_id':'job'}))
    (old.parent/'Readme.html').write_text('完整证据与修订历史')
    note=old.parent/'UserNote.txt';note.write_text('keep')
    target='Records/Jobs/job/Record.json';path=tmp_path/target;path.parent.mkdir(parents=True);path.write_text('{}')
    receipt=tmp_path/'Receipts/Old.json';receipt.parent.mkdir();receipt.write_text('immutable')
    mapping=retire_generated_views(tmp_path,{target},[old.relative_to(tmp_path).as_posix()])
    assert mapping[old.relative_to(tmp_path).as_posix()]==target
    assert not old.exists() and note.read_text()=='keep' and receipt.read_text()=='immutable'
    assert retire_generated_views(tmp_path,{target},[])=={}


def test_scoped_receiver_channel_uses_composite_camera_identity():
    from security import Principal,identity,filter_payload
    token=identity.set(Principal('u','甲','operator',frozenset(['sender_cam01'])))
    try:
        result=filter_payload({'camera':{'configured':True,'mode':'gwhp_main','id':'sender_cam01','camera_id':'cam01',
            'online_cameras':[{'sender_id':'sender','camera_id':'cam01','camera_key':'sender_cam01'},
                              {'sender_id':'other','camera_id':'cam01','camera_key':'other_cam01'}]}})
        assert result['camera']['id']=='sender_cam01'
        assert len(result['camera']['online_cameras'])==1
    finally:identity.reset(token)


def test_bootstrap_creates_private_credentials_once(tmp_path):
    from prepare_runtime import prepare
    from pathlib import Path
    result=prepare(tmp_path,'admin','甲','cam-a')
    path=tmp_path/'Access/InitialAccess.json'
    assert path.stat().st_mode & 0o777 == 0o600
    original=path.read_bytes();details=json.loads(original)
    AuthService(Database(result['database'])).login(details['username'],details['password'])
    again=prepare(tmp_path,'admin','甲','cam-a')
    assert result['user_id']==again['user_id'] and path.read_bytes()==original


def test_session_poll_does_not_write_or_wait_for_camera_writer(database):
    auth=AuthService(database);auth.create_initial_admin('admin','甲','test-password')
    session,user=auth.login('admin','test-password')
    with database.transaction('IMMEDIATE') as writer:
        # WAL permits independent session reads while a capture transaction writes.
        writer.execute("INSERT INTO automation_settings VALUES('cam','{}')")
        assert auth.verify_session(session.id).id==user.id


def test_same_worker_late_exception_cannot_fail_reclaimed_attempt(database):
    from ocr_worker import OCRWorker
    def broken(task):raise RuntimeError('old attempt finished late')
    worker=OCRWorker('same',None,{'database':database,'run_ocr':broken})
    worker.queue.enqueue({'job_id':'race','camera_id':'cam'})
    old=worker.queue.claim_task()
    with database.connection() as c:c.execute('UPDATE jobs SET lease_expires_at=?',(utc(time.time()-1),))
    new=worker.queue.claim_task()
    worker._process_ocr_task(old)
    with database.connection() as c:
        row=c.execute('SELECT * FROM jobs WHERE id=?',('race',)).fetchone()
        assert row['status']=='running' and row['lease_holder']==new['lease_holder']
    worker.queue.complete_task('race',{})
    assert not worker.queue.claims
