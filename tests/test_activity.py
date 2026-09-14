import json

from activity import recent_activity
from test_demo import app_client, scan  # noqa: F401


def test_unbound_scan_is_visible_without_fabricating_binding_or_operator(app_client):
    app, client = app_client
    capture = scan(client)
    before = client.get('/api/export').json()
    events = client.get('/api/state').json()['activity']
    hit = next(e for e in events if e['event_id'] == 'scan:' + capture['scan_id'])
    assert hit['kind'] == 'scan' and hit['status'] == '已识别'
    assert hit['detail'] == '未建立仪器绑定'
    assert hit['occurred_at'] == capture['received_at']
    assert hit['image_url'] == capture['image_url']
    assert hit['operator'] is None
    assert not any(e['kind'] == 'binding_started' for e in events)
    with app.db() as conn:
        assert json.loads(conn.execute('SELECT document FROM scans WHERE id=?', (capture['scan_id'],)).fetchone()[0]) == capture
    assert before['bindings'] == client.get('/api/state').json()['bindings']


def test_activity_preserves_bind_start_end_and_filters_camera(app_client):
    app, client = app_client
    first = scan(client, camera='ActualCamera')
    scan(client, camera='OtherCamera')
    aid = first['matches'][0]['id']
    client.put('/api/instruments/' + aid, json={'name': '仪器 A', 'scene': '湿实验实验台'})
    bound = client.post('/api/bindings', json={'scan_id': first['scan_id'], 'instrument_id': aid, 'operator': '测试员'}).json()
    ended = client.post('/api/bindings/' + bound['binding_id'] + '/end').json()
    with app.db() as conn:
        events = recent_activity(conn, 'ActualCamera')
    assert all(e['camera_id'] == 'ActualCamera' for e in events)
    assert [e['kind'] for e in events] == ['binding_ended', 'binding_started', 'scan']
    assert events[0]['occurred_at'] == ended['ended_at']
    assert events[1]['occurred_at'] == bound['started_at']
    assert events[1]['operator'] == '测试员'
    assert events[2]['operator'] is None
    assert events[2]['detail'] == '已建立绑定'


def test_readout_history_uses_persisted_status_and_reading(app_client):
    app, client = app_client
    doc = {'job_id': 'test-job', 'camera_id': 'ActualCamera', 'instrument': {'name': '仪器 A'},
           'operator': '测试员', 'submitted_at': '2026-09-14T02:00:00+00:00', 'status': 'running',
           'crop_image_url': '/api/images/test-job', 'lines': [{'text': '123.45'}]}
    with app.db() as conn:
        conn.execute('INSERT INTO jobs VALUES(?,?,?)', ('test-job', 'interrupted', json.dumps(doc)))
        event = recent_activity(conn, 'ActualCamera')[0]
    assert event['kind'] == 'readout' and event['status'] == '识别中断'
    assert event['operator'] == '测试员'
    assert event['image_url'] == doc['crop_image_url']


def test_scene_scan_is_distinct_from_instrument_binding(app_client):
    _, client = app_client
    capture = scan(client, 'Scene01')
    scene_id = capture['scene_matches'][0]['id']
    response = client.post('/api/scene/enter', json={'scan_id': capture['scan_id'], 'scene_id': scene_id, 'operator': '测试员'})
    assert response.status_code == 200
    events = client.get('/api/state').json()['activity']
    hit = next(e for e in events if e['event_id'] == 'scan:' + capture['scan_id'])
    assert hit['detail'] == '已记录场景进入'
    visit = next(e for e in events if e['kind'] == 'scene_entered')
    assert visit['operator'] == '测试员'


def test_photo_local_timezone_does_not_sort_ahead_of_later_utc_readout(app_client):
    app, client = app_client
    capture = scan(client)
    capture.update(source='agent_saved_photo', external_photo={'captured_at': '2026-09-14T11:53:00+08:00'})
    job = {'job_id': 'time-order', 'camera_id': capture['camera_id'], 'instrument': None,
           'submitted_at': '2026-09-14T04:00:00+00:00', 'status': 'completed', 'lines': []}
    with app.db() as conn:
        conn.execute('UPDATE scans SET document=? WHERE id=?', (json.dumps(capture), capture['scan_id']))
        conn.execute('INSERT INTO jobs VALUES(?,?,?)', (job['job_id'], 'completed', json.dumps(job)))
        events = recent_activity(conn)
    assert [e['kind'] for e in events] == ['readout', 'photo']


def test_readout_pages_keep_cursor_camera_timing_and_archive(app_client, monkeypatch):
    app, client = app_client
    monkeypatch.setenv('FIELD_CAMERA_ID', 'ActualCamera')
    def insert(ident, camera='ActualCamera'):
        doc = {'job_id': ident, 'camera_id': camera, 'instrument': None, 'operator': None,
               'submitted_at': '2026-09-14T04:00:00+00:00', 'status': 'completed',
               'lines': [{'text': 'RN55'}, {'text': '12.34 g'}],
               'timing': {'is_backfill': False, 'durations_ms': {'write_to_result_ms': 1250, 'ocr_ms': 20}}}
        with app.db() as conn:
            conn.execute('INSERT INTO jobs VALUES(?,?,?)', (ident, 'completed', json.dumps(doc)))
    for i in range(5):
        insert('job-' + str(i))
    insert('other-camera', 'OtherCamera')
    first = client.get('/api/readouts?limit=2').json()
    assert [row['job_id'] for row in first['items']] == ['job-4', 'job-3']
    item = first['items'][0]
    assert item['timing']['durations_ms']['write_to_result_ms'] == 1250
    assert item['archive']['status'] == 'pending' and item['result_url'] == '/api/jobs/job-4'
    assert item['detail'] == '12.34 g' and item['status'] == '数字候选 · 待核对'
    insert('new-after-first-page')
    second = client.get('/api/readouts', params={'limit': 2, 'before': first['next_cursor']}).json()
    last = client.get('/api/readouts', params={'limit': 2, 'before': second['next_cursor']}).json()
    assert [row['job_id'] for row in second['items'] + last['items']] == ['job-2', 'job-1', 'job-0']
    assert last['next_cursor'] is None
    assert all(row['camera_id'] == 'ActualCamera' for row in first['items'] + second['items'] + last['items'])
    with app.db() as conn:
        conn.execute("UPDATE archive_outbox SET archived_at='2026-09-14T04:00:02Z',receipt_path='receipt.json' WHERE entity_id='job-4'")
    assert client.get('/api/readouts').json()['items'][1]['archive']['status'] == 'archived'
    for query in ('limit=0', 'limit=51', 'before=-1', 'before=abc'):
        assert client.get('/api/readouts?' + query).status_code == 422
