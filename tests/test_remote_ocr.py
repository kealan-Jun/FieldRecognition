import hashlib
import json

import httpx
import numpy as np
import pytest

from remote_ocr import RemoteOcr, InferenceUnavailable, InferenceRejected, inference_request
from database import Database, apply_migrations
from task_queue import TaskQueue, LeaseLost


@pytest.fixture
def remote(tmp_path, monkeypatch):
    token = tmp_path/'Token'; token.write_text('private-test-token')
    for key,value in {
        'URL':'http://127.0.0.1:8189', 'TOKEN_FILE':str(token), 'SCOPE':'test-scope',
        'STORAGE':'test-images', 'MODEL_REVISION':'a'*64, 'SPOOL':str(tmp_path),
    }.items():
        monkeypatch.setenv('FIELD_OCR_SERVICE_'+key, value)
    model = RemoteOcr()
    yield model
    model.client.close()


def test_remote_retry_reuses_job_and_checks_receipt(remote):
    inputs=[]; keys=[]; attempts=[0]; source={}
    def transport(req):
        assert req.headers['X-Execution-Scope']=='test-scope'
        if req.url.path=='/health/ready':
            return httpx.Response(200,json={'workers':{'ready':1}})
        if req.method=='POST':
            body=json.loads(req.content); inputs.append(body); keys.append(req.headers['Idempotency-Key'])
            source.update(body['asset']);attempts[0]+=1
            assert hashlib.sha256((remote.spool/source['object_key']).read_bytes()).hexdigest()==source['sha256']
            if attempts[0]==1:
                raise httpx.ReadError('connection lost after admission')
            return httpx.Response(202,json={'job_id':'same-job','state':'succeeded'})
        return httpx.Response(200,json={'source':source,'model':{'weights_revision':'a'*64},
            'execution':{'actual_model_invocation':True},'job_id':'same-job',
            'lines':[{'text':'390','confidence':.91,'polygon':[[0,0],[10,0],[10,8],[0,8]]}]})
    remote.client=httpx.Client(transport=httpx.MockTransport(transport))
    pixels=np.zeros((20,30,3),np.uint8)
    with inference_request('business-photo-1'), pytest.raises(InferenceUnavailable): remote.predict(pixels)
    assert len(list(remote.spool.glob('*.png')))==1
    with inference_request('business-photo-1'): result=remote.predict(pixels)
    assert keys[0]==keys[1] and inputs[0]==inputs[1]
    assert result[0]['rec_texts']==['390'] and remote.last_receipt['job_id']=='same-job'
    assert not list(remote.spool.glob('*.png'))
    with inference_request('business-photo-2'): remote.predict(pixels)
    assert keys[-1]!=keys[0]  # equal pixels do not merge different measurements


def test_offline_worker_does_not_spool_video_or_claim_gpu(remote,monkeypatch):
    remote.client=httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'workers':{'stopped':1}})))
    with pytest.raises(InferenceUnavailable): remote.predict(np.zeros((10,10,3),np.uint8))
    assert not list(remote.spool.glob('*.png'))
    assert not remote.health()['resident']
    import ocr_runtime
    monkeypatch.setattr(ocr_runtime,'local_model_options',lambda:pytest.fail('Local Paddle must not load'))
    assert isinstance(ocr_runtime.create_model('gpu:0'),RemoteOcr)


@pytest.mark.parametrize('code',[401,403,429,500,503])
def test_dependency_errors_preserve_wait_and_never_expose_response(remote,code):
    remote.client=httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(code,text='secret upstream diagnostic')))
    with pytest.raises(InferenceUnavailable) as error: remote.predict(np.zeros((10,10,3),np.uint8))
    assert 'secret' not in str(error.value) and not list(remote.spool.glob('*.png'))


def test_mismatched_result_is_not_accepted(remote):
    def transport(req):
        if req.url.path=='/health/ready':return httpx.Response(200,json={'workers':{'ready':1}})
        if req.method=='POST':return httpx.Response(202,json={'state':'succeeded','job_id':'wrong'})
        return httpx.Response(200,json={'source':{'sha256':'b'*64},'model':{'weights_revision':'a'*64},'execution':{'actual_model_invocation':True}})
    remote.client=httpx.Client(transport=httpx.MockTransport(transport))
    with pytest.raises(InferenceRejected):remote.predict(np.zeros((10,10,3),np.uint8))
    assert remote.last_receipt is None


def test_dependency_wait_survives_many_failures_restart_and_fences_old_worker(tmp_path):
    db=Database(tmp_path/'Queue.sqlite3');apply_migrations(db)
    q=TaskQueue(db);q.enqueue({'job_id':'photo','camera_id':'cam','source_time':'original'})
    first=None
    for i in range(8):
        q=TaskQueue(db)
        job=q.claim_task();first=first or job
        assert job['attempt_count']==1
        q.defer_task('photo',document=job,delay=0)
    newer=TaskQueue(db);job=newer.claim_task()
    with pytest.raises(LeaseLost):q.defer_task('photo',document=first)
    newer.complete_task('photo',{'lines':[{'text':'51'}]})
    with db.connection() as c:
        row=c.execute('select status,document from jobs').fetchone();doc=json.loads(row['document'])
        assert row['status']=='completed' and doc['dependency_wait_count']==8
        assert doc['source_time']=='original' and doc['lines'][0]['text']=='51'


def test_missing_ipc_returns_service_unavailable_json(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from runtime_rpc import call
    monkeypatch.setenv('FIELD_DEMO_DATA',str(tmp_path))
    app=FastAPI()
    @app.put('/start')
    def start():return call('missing-camera','automation.configure')
    with TestClient(app) as c:
        response=c.put('/start')
        assert response.status_code==503 and '后台服务暂不可用' in response.json()['detail']


def test_clean_shutdown_resumes_only_explicit_photo_work(tmp_path):
    db=Database(tmp_path/'Queue.sqlite3');apply_migrations(db);q=TaskQueue(db)
    for name,trigger,resume in [('photo','voice_photo_directory',True),('video','video_stream',True),('other','manual',False)]:
        q.enqueue({'job_id':name,'camera_id':'cam','request_trigger':trigger})
        doc=q.claim_task();q.publish(doc|{'status':'interrupted','resume_pending':resume})
    restarted=TaskQueue(db)
    assert restarted.recover_interrupted()==1
    assert restarted.recover_interrupted()==0
    job=restarted.claim_task();assert job['job_id']=='photo'
    assert job['phase']=='restart_recovery' and job['attempt_count']==1


def test_watchdog_is_not_notified_when_database_fails(tmp_path,monkeypatch):
    import socket
    from worker_support import heartbeat
    db=Database(tmp_path/'Queue.sqlite3');apply_migrations(db)
    # The deployment runtime table is created by the application, not a migration.
    with db.connection() as c:c.execute('CREATE TABLE IF NOT EXISTS runtime_status(name TEXT PRIMARY KEY,document TEXT,updated_at REAL)')
    path=str(tmp_path/'Notify.sock')
    with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as receiver:
        receiver.bind(path);receiver.settimeout(.05)
        monkeypatch.setenv('NOTIFY_SOCKET',path);monkeypatch.delenv('WATCHDOG_PID',raising=False)
        heartbeat(db,'test',{'status':'running'})
        assert b'WATCHDOG=1' in receiver.recv(200)
        with db.connection() as c:c.execute('DROP TABLE runtime_status')
        with pytest.raises(Exception):heartbeat(db,'test',{'status':'running'})
        with pytest.raises(TimeoutError):receiver.recv(200)


def test_supervisor_recovers_a_live_but_stalled_capture_child():
    from camera_supervisor import capture_stalled
    progress={'seen':0}
    def row(pid,stamp):return {'document':json.dumps({'pid':pid}),'updated_at':stamp}
    assert not capture_stalled(20,None,progress,100)
    assert not capture_stalled(20,row(20,1000),progress,110)
    # A cached heartbeat cannot keep a hung process alive.
    assert capture_stalled(20,row(20,1000),progress,231)
    # Another process's status must never renew this child's deadline.
    assert capture_stalled(20,row(19,2000),progress,232)
    assert not capture_stalled(20,row(20,1001),progress,233)
