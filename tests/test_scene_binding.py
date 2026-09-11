import uuid
from test_demo import app_client, scan, enter_scene  # noqa: F401


def test_scene_required_and_camera_isolation(app_client):
    app, client = app_client
    picture = scan(client)
    aid = picture['matches'][0]['id']
    client.put('/api/instruments/'+aid,json={'name':'称量仪器 A','scene':'湿实验实验台'})
    args={'scan_id':picture['scan_id'],'instrument_id':aid,'operator':'Tester'}
    assert client.post('/api/bindings',json=args).status_code==409
    assert enter_scene(client,'DifferentCamera').status_code==200
    assert client.post('/api/bindings',json=args).status_code==409
    visit=enter_scene(client).json()
    bound=client.post('/api/bindings',json=args)
    assert bound.status_code==200
    assert bound.json()['scene_visit_id']==visit['visit_id']
    assert enter_scene(client).json()['visit_id']==visit['visit_id']
    # Instrument photos cannot serve as scene-entry evidence.
    assert client.post('/api/tools/enter_scene',json={'scan_id':picture['scan_id'],'scene_id':visit['scene']['id']}).status_code==409
    assert enter_scene(client,scene_id=str(uuid.uuid4())).status_code==409


def test_scene_change_ends_binding_and_invalidates_ocr(app_client):
    app,client=app_client
    picture=scan(client);aid=picture['matches'][0]['id']
    client.put('/api/instruments/'+aid,json={'name':'称量仪器 A','scene':'湿实验实验台'})
    enter_scene(client)
    args={'scan_id':picture['scan_id'],'instrument_id':aid,'operator':'Tester'}
    bound=client.post('/api/bindings',json=args).json()
    second=str(uuid.uuid4())
    with app.db() as conn:conn.execute('INSERT INTO scenes VALUES(?,?)',(second,'其他场景'))
    assert enter_scene(client,scene_id=second).status_code==200
    assert client.post('/api/bindings',json=args).status_code==409
    assert client.post('/api/ocr',json={'binding_id':bound['binding_id'],'capture_id':picture['capture_id']}).status_code==409
    assert client.get('/api/state').json()['bindings'][0]['ended_at']
