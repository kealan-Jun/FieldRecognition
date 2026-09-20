"""Session regressions use isolated SQLite, decoded QR fixtures and OCR doubles."""
import copy
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from test_demo import app_client, scan, register  # noqa: F401
from test_unbound_readout import photo
from binding_operator import at_time
from readout_context import at_capture


def initial(app, client, monkeypatch, *, instruments=('InstrumentA',)):
    clock = ['2026-09-20T01:00:00+00:00']
    monkeypatch.setattr(app, 'now', lambda: clock[0])
    items = []
    for name in instruments:
        captured = scan(client, name)
        iid = captured['matches'][0]['id']
        register(client, iid)
        body = {'scan_id':captured['scan_id'],'instrument_id':iid,'operator':'甲','wearer_id':'person-a'}
        response = client.post('/api/bindings',json=body)
        assert response.status_code == 200, response.text
        items.append((body,response.json()))
    return clock, items


def stored(app):
    with app.db() as conn:
        return [json.loads(row[0]) for row in conn.execute('SELECT document FROM bindings ORDER BY rowid')]


def test_handover_is_camera_wide_preserves_multiple_instruments_and_half_open_intervals(app_client, monkeypatch):
    app,client=app_client
    clock,items=initial(app,client,monkeypatch,instruments=('InstrumentA','InstrumentB'))
    before=copy.deepcopy(stored(app))
    clock[0]='2026-09-20T02:00:00+00:00'
    body,old=items[0]
    request=body|{'operator':'乙','wearer_id':'person-b','request_id':str(uuid.uuid4()),'expected_binding_id':old['binding_id']}
    response=client.post('/api/bindings',json=request)
    assert response.status_code==200,response.text
    successor=response.json()
    assert successor['binding_action']=='handover'
    assert client.post('/api/bindings',json=request).json()==successor
    all_rows=stored(app)
    active=[b for b in all_rows if not b['ended_at']]
    assert len(all_rows)==4 and len(active)==2
    assert {b['instrument']['id'] for b in active}=={b['instrument']['id'] for b in before}
    assert {(b['operator'],b['wearer_id']) for b in active}=={('乙','person-b')}
    for original in before:
        ended=next(b for b in all_rows if b['binding_id']==original['binding_id'])
        assert ended['operator']=='甲' and ended['wearer_id']=='person-a'
        assert ended['ended_at']==clock[0]
    with app.db() as conn:
        for instant,person in [('2026-09-20T01:59:59.999999+00:00','甲'),(clock[0],'乙')]:
            context=at_capture(vars(app),conn,{'camera_id':'TestCamera','external_photo':{'captured_at':instant}},automatic=True)
            assert len(context['all_binding_snapshots'])==2
            assert {b['operator'] for b in context['all_binding_snapshots']}=={person}
            assert not context['ownership_conflicts']
        audit=[json.loads(r[0]) for r in conn.execute("SELECT document FROM binding_session_audit WHERE relation_type='bindings'")]
    assert len(audit)==2 and all(a['previous_operator']=='甲' and a['operator']=='乙' for a in audit)
    assert client.post('/api/bindings',json=request|{'operator':'丙'}).status_code==409


def test_same_request_retries_after_later_handover_do_not_switch_back(app_client,monkeypatch):
    app,client=app_client
    clock,items=initial(app,client,monkeypatch)
    body,old=items[0]
    clock[0]='2026-09-20T02:00:00+00:00'
    request=body|{'operator':'乙','wearer_id':'person-b','request_id':str(uuid.uuid4())}
    response=client.post('/api/bindings',json=request).json()
    clock[0]='2026-09-20T03:00:00+00:00'
    client.post('/api/bindings',json=body|{'operator':'丙','wearer_id':'person-c','request_id':str(uuid.uuid4())})
    assert client.post('/api/bindings',json=request).json()==response
    assert [b['operator'] for b in stored(app) if not b['ended_at']]==['丙']


def test_concurrent_handover_with_precondition_has_one_winner(app_client,monkeypatch):
    app,client=app_client
    clock,items=initial(app,client,monkeypatch)
    body,old=items[0]
    clock[0]='2026-09-20T02:00:00+00:00'
    requests=[app.BindingRequest(**(body|{'operator':person,'wearer_id':person,
        'expected_binding_id':old['binding_id'],'request_id':str(uuid.uuid4())})) for person in ('乙','丙')]
    def run(request):
        try:return app.save_binding(request)
        except HTTPException as exc:return exc.status_code
    with ThreadPoolExecutor(2) as pool:
        replies=list(pool.map(run,requests))
    assert sum(r==409 for r in replies)==1
    assert len(stored(app))==2 and sum(not b['ended_at'] for b in stored(app))==1


def test_concurrent_same_request_and_legacy_repeat_are_idempotent(app_client,monkeypatch):
    app,client=app_client
    clock,items=initial(app,client,monkeypatch)
    body,old=items[0]
    clock[0]='2026-09-20T02:00:00+00:00'
    request=app.BindingRequest(**(body|{'operator':'乙','wearer_id':'person-b','request_id':str(uuid.uuid4())}))
    with ThreadPoolExecutor(3) as pool:
        replies=list(pool.map(app.save_binding,[request]*3))
    assert len({r['binding_id'] for r in replies})==1
    assert len(stored(app))==2
    legacy=request.model_copy(update={'request_id':None})
    assert app.save_binding(legacy)['binding_id']==replies[0]['binding_id']
    assert len(stored(app))==2


def test_failed_transaction_rolls_back_close_successor_and_audit(app_client,monkeypatch):
    app,client=app_client
    clock,items=initial(app,client,monkeypatch)
    body,_=items[0]
    before=stored(app)
    clock[0]='2026-09-20T02:00:00+00:00'
    with app.db() as conn:
        audit_count=conn.execute('SELECT count(*) FROM binding_session_audit').fetchone()[0]
        conn.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON binding_session_audit BEGIN SELECT RAISE(ABORT,'injected disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError,match='injected disk failure'):
        app.save_binding(app.BindingRequest(**(body|{'operator':'乙','request_id':str(uuid.uuid4())})))
    assert stored(app)==before
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM binding_session_audit').fetchone()[0]==audit_count
        assert conn.execute('SELECT count(*) FROM binding_change_requests').fetchone()[0]==0


def test_delayed_capture_and_queued_job_keep_old_people_and_allowed_list(app_client,monkeypatch):
    app,client=app_client
    monkeypatch.setenv('FIELD_CAMERA_ID','TestCamera')
    clock,items=initial(app,client,monkeypatch)
    body,old=items[0]
    queued=[]
    monkeypatch.setattr(app.readout_pool,'submit',lambda fn,job:queued.append(job))
    clock[0]='2026-09-20T01:59:00+00:00'
    captured=photo(app,captured_at=clock[0],ident='before-handover')
    request={'photo':captured,'binding_id':old['binding_id']}
    first=client.post('/api/ocr/photo-result',json=request).json()
    frozen=copy.deepcopy(first['all_binding_snapshots'])
    clock[0]='2026-09-20T02:00:00+00:00'
    new=client.post('/api/bindings',json=body|{'operator':'乙','wearer_id':'person-b'}).json()
    clock[0]='2026-09-20T02:01:00+00:00'
    # A late file import uses the history at capture, including the closed old interval.
    late=client.post('/api/ocr/photo-result',json={'photo':captured|{'capture_id':'late-before-handover'},'binding_id':old['binding_id']}).json()
    assert late['binding_id']==old['binding_id'] and late['operator']=='甲' and late['wearer_id']=='person-a'
    assert late['all_binding_snapshots'][0]['ended_at']==new['started_at']
    seen=[]
    def predict(*args,**kwargs):
        seen.append(copy.deepcopy(kwargs['binding_snapshots']))
        return {'status':'completed','lines':[],'actual_model_invocation':False}
    monkeypatch.setattr(app,'predict_readout',predict)
    app.run_ocr(first)
    finished=app.get_job(first['job_id'])
    assert finished['status']=='completed' and finished['operator']=='甲'
    assert finished['all_binding_snapshots']==frozen and seen==[frozen]
    after=client.post('/api/ocr/photo-result',json={'photo':photo(app,ident='after-handover'),'binding_id':new['binding_id']}).json()
    assert after['operator']=='乙' and after['wearer_id']=='person-b'
    assert after['allowed_instrument_ids']==[body['instrument_id']]


def test_undated_upload_has_no_current_person_attribution(app_client,monkeypatch):
    app,client=app_client
    clock,items=initial(app,client,monkeypatch)
    body,old=items[0]
    monkeypatch.setattr(app.readout_pool,'submit',lambda *args:None)
    response=client.post('/api/ocr',json={'capture_id':body['scan_id'],'binding_id':old['binding_id']})
    assert response.status_code==202,response.text
    job=response.json()
    assert job['operator'] is job['wearer_id'] is None
    assert job['attribution_status']=='needs_review' and job['attribution_reason']=='capture_time_unverified'
    assert job['allowed_instrument_ids']==[]


def test_refresh_and_asset_update_create_auditable_successors(app_client,monkeypatch):
    app,client=app_client
    clock,items=initial(app,client,monkeypatch)
    body,old=items[0]
    assert client.post('/api/bindings',json=body).json()['binding_id']==old['binding_id']
    assert client.post('/api/bindings',json=body|{'action':'refresh'}).status_code==422
    clock[0]='2026-09-20T02:00:00+00:00'
    refresh=body|{'action':'refresh','request_id':str(uuid.uuid4())}
    fresh=client.post('/api/bindings',json=refresh).json()
    assert fresh['binding_id']!=old['binding_id'] and fresh['binding_action']=='refresh'
    assert fresh['valid_until']==old['valid_until']
    assert client.post('/api/bindings',json=refresh).json()==fresh
    client.put('/api/instruments/'+body['instrument_id'],json={'name':'更新显示名称','scene':'湿实验实验台','model':'更新型号'})
    clock[0]='2026-09-20T03:00:00+00:00'
    updated=client.post('/api/bindings',json=body).json()
    assert updated['binding_id']!=fresh['binding_id'] and updated['instrument']['model']=='更新型号'
    assert stored(app)[0]['instrument']['model']==old['instrument']['model']


def test_legacy_operator_history_remains_readable():
    legacy={'operator':'乙','wearer_id':'b','operator_history':[{
        'changed_at':'2026-09-20T02:00:00+00:00','previous_operator':'甲','previous_wearer_id':'a',
        'operator':'乙','wearer_id':'b','reason':'legacy'}]}
    assert at_time(legacy,'2026-09-20T01:59:00+00:00')['operator']=='甲'
    assert at_time(legacy,'2026-09-20T02:00:00+00:00')['wearer_id']=='b'
    assert legacy['operator']=='乙'


def test_legacy_handover_retry_does_not_revert_newer_session(app_client,monkeypatch):
    app,client=app_client
    clock,items=initial(app,client,monkeypatch)
    body,_=items[0]
    clock[0]='2026-09-20T02:00:00+00:00'
    legacy=body|{'operator':'乙','wearer_id':'person-b'}
    second=client.post('/api/bindings',json=legacy).json()
    clock[0]='2026-09-20T03:00:00+00:00'
    client.post('/api/bindings',json=body|{'operator':'丙','wearer_id':'person-c'})
    assert client.post('/api/bindings',json=legacy).json()==second
    assert [b['operator'] for b in stored(app) if not b['ended_at']]==['丙']


def test_background_scanner_refreshes_same_name_member_id(app_client,monkeypatch):
    import threading
    from types import SimpleNamespace
    from automation import AutomaticRunner
    from database import apply_migrations
    from binding_operator import activate_member
    app,_=app_client
    apply_migrations(app.database)
    with app.db() as conn:
        for uid in ('same-a','same-b'):
            conn.execute('INSERT INTO users(id,username,display_name,role,created_at) VALUES(?,?,?,?,?)',
                         (uid,uid,'同名','operator',app.now()))
        activate_member(conn,'BackgroundCamera','same-a',app.now())
        conn.execute('INSERT INTO automation_settings VALUES(?,?)',('BackgroundCamera',json.dumps({
            'enabled':True,'operator':'同名','wearer_id':'same-a','registered_at':app.now()})))
    scanner=SimpleNamespace(lock=threading.RLock(),session={'status':'scanning','owner':'automation',
        'operator':'同名','wearer_id':'same-a','session_id':'old'})
    started=[]
    def start(operator,**kwargs):
        started.append((operator,kwargs))
        scanner.session={'status':'scanning','message':'ready','session_id':'new',
                         'operator':operator,**kwargs}
        return scanner.session
    scanner.start=start
    scanner.refresh_bindings=lambda:None
    camera=SimpleNamespace(target='BackgroundCamera',snapshot=lambda:{'service_status_available':True,
        'service_status':{'online':True}})
    core=dict(vars(app),RUNTIME_ENABLED=True,live_scanner=scanner,receiver_camera=camera)
    runner=AutomaticRunner(core)
    with app.db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        activate_member(conn,'BackgroundCamera','same-b',app.now())
    runner.step()
    assert started==[('同名',{'owner':'automation','wearer_id':'same-b'})]
    assert scanner.session['wearer_id']=='same-b'
