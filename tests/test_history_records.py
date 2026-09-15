import copy
import json

import pytest

from history_records import panel_readings
from test_demo import app_client, scan  # noqa: F401


def job(ident='good', text='12.34', value='12.34'):
    asset={'id':'instrument-a','name':'仪器 A','scene':'实验台'}
    return {'job_id':ident,'capture_id':'capture','status':'completed','camera_id':'ActualCamera',
        'submitted_at':'2026-09-14T05:44:00Z','finished_at':'2026-09-14T05:44:01Z','instrument':asset,
        'image_url':'/api/images/capture','lines':[{'text':text}],
        'panel_regions':[{'panel_id':'panel-0-0','instrument':asset,'binding_id':'binding-a'}],
        'readings':[{'text':text,'value':value,'instrument':asset,'panel_id':'panel-0-0',
                     'binding_id':'binding-a','association_basis':'panel_detector_session_binding'}]}


@pytest.mark.parametrize('case',['empty','background','unbound','wrong_instrument','missing_instrument','failed','pending','decimal','self_test'])
def test_only_completed_attributable_panel_numbers_enter_business_history(case):
    doc=job()
    assert panel_readings(doc)
    if case=='empty':doc['readings']=[]
    if case=='background':doc['panel_regions']=[]
    if case=='unbound':doc['readings'][0]['binding_id']=None
    if case=='wrong_instrument':doc['readings'][0]['instrument']={'id':'another-device'}
    if case=='missing_instrument':doc['panel_regions'][0]['instrument']=None
    if case=='failed':doc['status']='failed'
    if case=='pending':doc['status']='running'
    if case=='decimal':doc['readings'][0]['value']='02287'
    if case=='self_test':doc['readings'][0]['value']='888888.8'
    assert not panel_readings(doc)


def test_zero_negative_and_single_device_values_remain_without_human_accuracy_claim():
    for value in ['0','0.0000','-32.4223']:
        doc=job(text=value,value=value)
        assert panel_readings(doc)==doc['readings']
        assert not doc['readings'][0].get('human_verified')


def test_filters_before_paging_and_recent_limit_and_does_not_modify_receipts(app_client,monkeypatch):
    app,client=app_client
    monkeypatch.setenv('FIELD_CAMERA_ID','ActualCamera')
    originals={}
    with app.db() as conn:
        for i in range(65):
            doc=job(str(i))
            if i not in (1,3,6):doc['panel_regions']=[]
            raw=json.dumps(doc);originals[str(i)]=raw
            conn.execute('INSERT INTO jobs VALUES(?,?,?)',(str(i),'completed',raw))
        other=job('other');other['camera_id']='AnotherCamera'
        conn.execute('INSERT INTO jobs VALUES(?,?,?)',('other','completed',json.dumps(other)))
    first=client.get('/api/readouts?related_only=true&limit=2').json()
    assert [e['job_id'] for e in first['items']]==['6','3']
    second=client.get('/api/readouts',params={'related_only':True,'limit':2,'before':first['next_cursor']}).json()
    assert [e['job_id'] for e in second['items']]==['1'] and second['next_cursor'] is None
    events=client.get('/api/state').json()['activity']
    assert {e['job_id'] for e in events}=={'1','3','6'}
    assert client.get('/api/state').json()['latest_panel_job']['job_id']=='6'
    with app.db() as conn:
        assert {r['id']:r['document'] for r in conn.execute("SELECT * FROM jobs WHERE id!='other'")}==originals
    assert len(client.get('/api/readouts?limit=50').json()['items'])==50  # raw API stays compatible


def test_recent_qr_is_not_buried_by_newer_photos_and_voice_qr_is_a_qr_event(app_client,monkeypatch):
    app,client=app_client
    monkeypatch.setenv('FIELD_CAMERA_ID','ActualCamera')
    qr=scan(client,camera='ActualCamera')
    with app.db() as conn:
        qr['source']='agent_saved_photo'
        conn.execute('UPDATE scans SET document=? WHERE id=?',(json.dumps(qr),qr['scan_id']))
        for i in range(60):
            photo=dict(qr,scan_id=f'empty-{i}',matches=[],scene_matches=[],received_at='2099-01-01T00:00:00Z')
            conn.execute('INSERT INTO scans VALUES(?,?)',(photo['scan_id'],json.dumps(photo)))
    events=client.get('/api/state').json()['activity']
    assert len(events)==1 and events[0]['kind']=='scan'
    assert events[0]['image_url']==qr['image_url']


def test_mixed_job_excludes_unrelated_line_from_timeline_and_workbench(app_client,monkeypatch):
    app,client=app_client
    monkeypatch.setenv('FIELD_CAMERA_ID','ActualCamera')
    doc=job();bad=copy.deepcopy(doc['readings'][0]);bad.update(text='5000',value='5000',instrument=None,panel_id=None)
    doc['readings'].append(bad);doc['lines'].append({'text':'5000'})
    with app.db() as conn:conn.execute('INSERT INTO jobs VALUES(?,?,?)',(doc['job_id'],'completed',json.dumps(doc)))
    event=client.get('/api/readouts?related_only=true').json()['items'][0]
    assert event['detail']=='12.34' and len(event['readings'])==1
    latest=client.get('/api/state').json()['latest_panel_job']
    assert [line['text'] for line in latest['lines']]==['12.34']
    with app.db() as conn:
        assert len(json.loads(conn.execute('SELECT document FROM jobs WHERE id=?',(doc['job_id'],)).fetchone()[0])['readings'])==2
    grouped=client.get('/api/workbenches?related_only=true').json()
    assert sum(g['reading_count'] for g in grouped['workbenches'])==1
    assert not any(g['unassigned_readings'] for g in grouped['workbenches'])


def test_two_instruments_in_one_photo_keep_their_own_readings(app_client,monkeypatch):
    app,client=app_client
    monkeypatch.setenv('FIELD_CAMERA_ID','ActualCamera')
    doc=job()
    other=job('b',text='51',value='51')
    asset={'id':'instrument-b','name':'仪器 B','scene':'实验台'}
    for item in [other['panel_regions'][0],other['readings'][0]]:
        item.update(instrument=asset,binding_id='binding-b',panel_id='panel-1-0')
    doc['panel_regions']+=other['panel_regions']
    doc['readings']+=other['readings']
    doc['instrument']=None
    doc['association_status']='localized_panels'
    with app.db() as conn:
        conn.execute('INSERT INTO jobs VALUES(?,?,?)',(doc['job_id'],'completed',json.dumps(doc)))
    event=client.get('/api/readouts?related_only=true').json()['items'][0]
    assert len(event['readings'])==2 and set(event['binding_ids'])=={'binding-a','binding-b'}
    assert event['target']=='仪器 A、仪器 B · 面板分别定位'
    grouped=client.get('/api/workbenches?related_only=true').json()['workbenches']
    bench=next(g for g in grouped if g['name']=='实验台')
    assert bench['photo_count']==1 and bench['reading_count']==2
    assert {i['id']:[r['value'] for r in i['readings']] for i in bench['instruments']}=={
        'instrument-a':['12.34'],'instrument-b':['51']}
    assert all(g['photo_count']==0 for g in grouped if g['name']=='未确定实验台')


def test_recent_limit_compares_instants_across_timezones(app_client):
    from history_records import job_rows
    from activity import recent_activity
    app,client=app_client
    for ident,time in [('newer','2026-09-14T04:00:00Z'),('older','2026-09-14T11:59:59+08:00')]:
        doc=job(ident)
        doc['finished_at']=time
        with app.db() as conn:
            conn.execute('INSERT INTO jobs VALUES(?,?,?)',(ident,'completed',json.dumps(doc)))
    with app.db() as conn:
        assert job_rows(conn,'ActualCamera',limit=1,recent=True)[0]['id']=='newer'
        assert recent_activity(conn,'ActualCamera',limit=1)[0]['job_id']=='newer'


def test_attributable_fallback_reading_keeps_its_display_text(app_client):
    from activity import recent_activity
    app,_=app_client
    doc=job(text='温度 179 °C',value='179')
    doc['device']='gpu:0'
    with app.db() as conn:
        conn.execute('INSERT INTO jobs VALUES(?,?,?)',(doc['job_id'],'completed',json.dumps(doc)))
        event=recent_activity(conn,'ActualCamera')[0]
    assert event['detail']=='温度 179 °C' and event['status']=='自动识别完成'


def test_latest_photo_reports_empty_result_and_orders_by_capture_time(app_client,monkeypatch):
    app,client=app_client
    monkeypatch.setenv('FIELD_CAMERA_ID','ActualCamera')
    older=job('old')
    older.update(external_photo={'captured_at':'2026-09-15T13:21:00+08:00'},finished_at='2026-09-15T10:00:00Z')
    newer=job('new')
    newer.update(external_photo={'captured_at':'2026-09-15T17:23:00+08:00'},readings=[],lines=[],recognition_skipped=True)
    video=job('video');video.update(request_trigger='video_stream',submitted_at='2026-09-15T11:00:00Z')
    with app.db() as conn:
        for doc in (newer,older,video):
            conn.execute('INSERT INTO jobs VALUES(?,?,?)',(doc['job_id'],doc['status'],json.dumps(doc)))
    state=client.get('/api/state').json()
    assert state['latest_photo_job']['job_id']=='new'
    assert state['latest_photo_job']['readings']==[]
    assert state['latest_panel_job']['job_id']=='old'
