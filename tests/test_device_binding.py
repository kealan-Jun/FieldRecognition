import json

from test_demo import app_client, register, scan


def test_multiple_qr_devices_can_share_one_camera_and_are_classified(app_client):
    _, client = app_client
    first = scan(client, 'InstrumentA', 'CameraAlpha')
    second = scan(client, 'InstrumentB', 'CameraAlpha')
    register(client, first['matches'][0]['id'], 'CameraAlpha')
    register(client, second['matches'][0]['id'], 'CameraAlpha')
    for capture in (first, second):
        response = client.post('/api/bindings', json={
            'scan_id': capture['scan_id'], 'instrument_id': capture['matches'][0]['id'], 'operator': '甲'})
        assert response.status_code == 200

    state = client.get('/api/state').json()
    assert len([b for b in state['bindings'] if not b['ended_at']]) == 2
    assert set(state['device_categories']) == {'instrument'}
    devices = client.get('/api/devices').json()
    assert len(devices['items']) == 2
    assert all(item['qr_registered'] and item['active_camera_id'] == 'CameraAlpha' for item in devices['items'])


def test_one_qr_device_cannot_be_bound_to_another_camera(app_client):
    _, client = app_client
    source = scan(client, 'InstrumentA', 'CameraAlpha')
    instrument_id = source['matches'][0]['id']
    register(client, instrument_id, 'CameraAlpha')
    bound = client.post('/api/bindings', json={
        'scan_id': source['scan_id'], 'instrument_id': instrument_id, 'operator': '甲'}).json()

    target = scan(client, 'InstrumentA', 'CameraBeta')
    register(client, instrument_id, 'CameraBeta')
    blocked = client.post('/api/bindings', json={
        'scan_id': target['scan_id'], 'instrument_id': instrument_id, 'operator': '乙'})
    assert blocked.status_code == 409
    assert blocked.json()['detail']['code'] == 'instrument_in_use'
    assert blocked.json()['detail']['binding_id'] == bound['binding_id']


def test_same_qr_identity_cannot_be_assigned_to_another_device(app_client):
    app, client = app_client
    source = scan(client, 'InstrumentA', 'CameraAlpha')
    source_id = source['matches'][0]['id']
    register(client, source_id, 'CameraAlpha')
    client.post('/api/bindings', json={
        'scan_id': source['scan_id'], 'instrument_id': source_id, 'operator': '甲'})

    target = scan(client, 'InstrumentB', 'CameraBeta')
    target_id = target['matches'][0]['id']
    register(client, target_id, 'CameraBeta')
    target['matches'][0]['qr_hash'] = source['matches'][0]['qr_hash']
    with app.db() as conn:
        conn.execute('UPDATE scans SET document=? WHERE id=?',
                     (json.dumps(target), target['scan_id']))

    blocked = client.post('/api/bindings', json={
        'scan_id': target['scan_id'], 'instrument_id': target_id, 'operator': '乙'})
    assert blocked.status_code == 409
    assert blocked.json()['detail']['code'] == 'qr_assigned_to_other_device'


def test_same_qr_can_be_reassigned_after_explicit_unbind(app_client):
    app, client = app_client
    source = scan(client, 'InstrumentA', 'CameraAlpha')
    source_id = source['matches'][0]['id']
    register(client, source_id, 'CameraAlpha')
    bound = client.post('/api/bindings', json={
        'scan_id': source['scan_id'], 'instrument_id': source_id, 'operator': '甲'}).json()
    assert client.post('/api/bindings/' + bound['binding_id'] + '/end').status_code == 200

    replacement = scan(client, 'InstrumentB', 'CameraBeta')
    replacement_id = replacement['matches'][0]['id']
    register(client, replacement_id, 'CameraBeta')
    # Simulate a replacement physical device carrying the released QR label.
    replacement['matches'][0]['qr_hash'] = source['matches'][0]['qr_hash']
    with app.db() as conn:
        conn.execute('UPDATE scans SET document=? WHERE id=?',
                     (json.dumps(replacement), replacement['scan_id']))

    rebound = client.post('/api/bindings', json={
        'scan_id': replacement['scan_id'], 'instrument_id': replacement_id, 'operator': '乙'})
    assert rebound.status_code == 200
    assert rebound.json()['camera_id'] == 'CameraBeta'
    directory = {item['device_id']: item for item in client.get('/api/devices').json()['items']}
    assert directory[replacement_id]['qr_hash'] == source['matches'][0]['qr_hash']


def test_device_category_is_persisted_and_can_filter_directory(app_client):
    _, client = app_client
    capture = scan(client)
    instrument_id = capture['matches'][0]['id']
    response = client.put('/api/instruments/' + instrument_id, json={
        'name': '搅拌器 A', 'scene': '湿实验实验台', 'device_category': '搅拌设备'})
    assert response.status_code == 200
    assert response.json()['device_category'] == '搅拌设备'
    preserved = client.put('/api/instruments/' + instrument_id, json={
        'name': '搅拌器 A（改名）', 'scene': '湿实验实验台'})
    assert preserved.status_code == 200
    assert preserved.json()['device_category'] == '搅拌设备'
    filtered = client.get('/api/devices?category=%E6%90%85%E6%8B%8C%E8%AE%BE%E5%A4%87').json()
    assert [item['device_id'] for item in filtered['items']] == [instrument_id]
