"""Personnel registration, explicit selection and authorization in isolated databases."""
import json
import uuid

from database import Database, apply_migrations
from live_scan import LiveScanner
from qr_decode import decode_qr
from test_direct_access import direct  # noqa: F401
from test_live_scan import Camera, label
from test_production_regressions import managed  # noqa: F401


def bearer(auth, username, password):
    session, _ = auth.login(username, password)
    return {'Authorization': 'Bearer ' + session.id}


def seed_personnel(conn):
    for user_id, name in (('person-a', '甲'), ('person-b', '乙')):
        conn.execute('''INSERT INTO users(id,username,display_name,role,created_at)
                        VALUES(?,?,?,'operator',?)''', (user_id, user_id, name, '2026-09-18T00:00:00Z'))


def assert_registration_and_selection(client, first_id, second_id, headers=None):
    headers = headers or {}
    members_url = '/api/admin/cameras/cam-a/members'
    read_url = '/api/cameras/cam-a/members'
    switch_url = '/api/cameras/cam-a/active-user'
    for user_id in (first_id, second_id, first_id):
        response = client.post(members_url, json={'user_id': user_id}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()['active_user_id'] is None
    response = client.get(read_url, headers=headers)
    assert response.status_code == 200, response.text
    members = response.json()
    assert {row['user_id'] for row in members['members']} == {first_id, second_id}
    assert len(members['members']) == 2
    assert members['active_user_id'] is None

    first_body = {'user_id': first_id, 'request_id': 'select-first', 'expected_revision': members['revision']}
    first = client.put(switch_url, json=first_body, headers=headers)
    assert first.status_code == 200, first.text
    first = first.json()
    assert first['active_user_id'] == first_id and first['revision'] > members['revision']
    assert client.put(switch_url, json=first_body, headers=headers).json() == first
    # Repeated registration must not replace an explicitly selected current user.
    assert client.post(members_url, json={'user_id': second_id}, headers=headers).json()['active_user_id'] == first_id

    second_body = {'user_id': second_id, 'request_id': 'select-second', 'expected_revision': first['revision']}
    second = client.put(switch_url, json=second_body, headers=headers)
    assert second.status_code == 200, second.text
    second = second.json()
    assert second['active_user_id'] == second_id and second['revision'] > first['revision']
    assert client.put(switch_url, json=second_body, headers=headers).json() == second
    # A delayed retry returns its receipt without reverting the more recent choice.
    assert client.put(switch_url, json=first_body, headers=headers).json() == first
    current = client.get(read_url, headers=headers).json()
    assert current['active_user_id'] == second_id and current['revision'] == second['revision']
    stale = client.put(switch_url, json=first_body | {'request_id': 'stale-selection'}, headers=headers)
    assert stale.status_code == 409
    reused = client.put(switch_url, json=first_body | {'user_id': second_id}, headers=headers)
    assert reused.status_code == 409
    assert client.get(read_url, headers=headers).json() == current


def test_direct_membership_registration_never_selects_first_member(direct):
    app, client = direct
    with app.db() as conn:
        seed_personnel(conn)
    assert_registration_and_selection(client, 'person-a', 'person-b')
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM camera_users').fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM binding_change_requests').fetchone()[0] == 2


def test_admin_registers_two_members_and_selects_with_retry_and_revision(managed):
    app, client, auth, admin, first = managed
    second = auth.create_user('bob', '乙', 'operator', 'second-person-password', admin.id)
    headers = bearer(auth, 'admin', 'correct horse battery')
    assert_registration_and_selection(client, first.id, second.id, headers)
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM camera_users WHERE camera_id=?', ('cam-a',)).fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM binding_change_requests').fetchone()[0] == 2


def test_removing_noncurrent_membership_keeps_user_and_experiment_history(managed):
    app, client, auth, admin, first = managed
    second = auth.create_user('bob', '乙', 'operator', 'second-person-password', admin.id)
    headers = bearer(auth, 'admin', 'correct horse battery')
    assert client.post('/api/admin/cameras/cam-a/members', json={'user_id': second.id}, headers=headers).status_code == 200
    assert client.put('/api/cameras/cam-a/active-user', json={'user_id': second.id}, headers=headers).status_code == 200
    binding = json.dumps({'binding_id': 'historical-binding', 'camera_id': 'cam-a', 'operator': '甲',
                          'wearer_id': first.id, 'started_at': '2026-09-18T09:00:00+08:00',
                          'ended_at': '2026-09-18T10:00:00+08:00'})
    reading = json.dumps({'job_id': 'historical-reading', 'camera_id': 'cam-a', 'operator': '甲',
                          'wearer_id': first.id, 'binding_id': 'historical-binding', 'raw_text': '0.0010 g'})
    with app.db() as conn:
        conn.execute('INSERT INTO bindings(id,camera,ended,document) VALUES(?,?,?,?)',
                     ('historical-binding', 'cam-a', '2026-09-18T10:00:00+08:00', binding))
        conn.execute('INSERT INTO jobs(id,status,document) VALUES(?,?,?)', ('historical-reading', 'completed', reading))
    response = client.delete('/api/admin/cameras/cam-a/members/' + first.id, headers=headers)
    assert response.status_code == 200, response.text
    assert [row['user_id'] for row in response.json()['members']] == [second.id]
    assert client.delete('/api/admin/cameras/cam-a/members/' + second.id, headers=headers).status_code == 409
    with app.db() as conn:
        assert conn.execute('SELECT disabled FROM users WHERE id=?', (first.id,)).fetchone()[0] == 0
        assert conn.execute('SELECT document FROM bindings WHERE id=?', ('historical-binding',)).fetchone()[0] == binding
        assert conn.execute('SELECT document FROM jobs WHERE id=?', ('historical-reading',)).fetchone()[0] == reading
        assert conn.execute('SELECT user_id FROM camera_active_users WHERE camera_id=?', ('cam-a',)).fetchone()[0] == second.id


def test_operator_can_select_self_but_cannot_register_impersonate_or_cross_cameras(managed):
    app, client, auth, admin, first = managed
    second = auth.create_user('bob', '乙', 'operator', 'second-person-password', admin.id)
    admin_headers = bearer(auth, 'admin', 'correct horse battery')
    assert client.post('/api/admin/cameras/cam-a/members', json={'user_id': second.id}, headers=admin_headers).status_code == 200
    assert client.put('/api/cameras/cam-a/active-user', json={'user_id': second.id}, headers=admin_headers).status_code == 200
    headers = bearer(auth, 'alice', 'test-password')
    response = client.get('/api/cameras/cam-a/members', headers=headers)
    assert response.status_code == 200
    assert len(response.json()['members']) == 2
    assert client.get('/api/cameras/cam-b/members', headers=headers).status_code == 403
    assert client.post('/api/admin/cameras/cam-a/members', json={'user_id': second.id}, headers=headers).status_code == 403
    assert client.delete('/api/admin/cameras/cam-a/members/' + second.id, headers=headers).status_code == 403
    assert client.put('/api/admin/cameras/cam-a/owner', json={'user_id': first.id}, headers=headers).status_code == 403
    assert client.put('/api/cameras/cam-a/active-user', json={'user_id': second.id}, headers=headers).status_code == 403
    assert client.put('/api/cameras/cam-b/active-user', json={'user_id': first.id}, headers=headers).status_code == 403
    before = client.get('/api/cameras/cam-a/members', headers=headers).json()
    assert before['active_user_id'] == second.id
    response = client.put('/api/cameras/cam-a/active-user', json={'user_id': first.id,
        'expected_revision': before['revision'], 'request_id': 'self-selection'}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['active_user_id'] == first.id


def test_device_credential_cannot_choose_personnel_even_for_current_user(managed):
    app, client, auth, admin, first = managed
    admin_headers = bearer(auth, 'admin', 'correct horse battery')
    assert client.put('/api/cameras/cam-a/active-user', json={'user_id': first.id}, headers=admin_headers).status_code == 200
    with app.db() as conn:
        conn.execute('''INSERT OR IGNORE INTO camera_registry(camera_id,display_name,enabled,registered_at)
                        VALUES('cam-a','测试相机',1,?)''', (app.now(),))
    credential = 'isolated-device-test-credential'
    auth.register_device('cam-a', 'neck_camera', credential, admin.id)
    headers = {'Authorization': 'Device ' + credential, 'X-Camera-Id': 'cam-a'}
    # A valid read proves the following denials are authorization, not failed login.
    before = client.get('/api/cameras/cam-a/members', headers=headers)
    assert before.status_code == 200, before.text
    assert client.put('/api/cameras/cam-a/active-user', json={'user_id': first.id}, headers=headers).status_code == 403
    assert client.post('/api/admin/cameras/cam-a/members', json={'user_id': first.id}, headers=headers).status_code == 403
    assert client.get('/api/cameras/cam-b/members', headers=headers).status_code == 403
    assert client.get('/api/cameras/cam-a/members', headers=headers).json() == before.json()


def test_last_membership_removal_rolls_back_and_unregistered_selection_is_rejected(direct):
    app, client = direct
    with app.db() as conn:
        seed_personnel(conn)
    assert client.post('/api/admin/cameras/cam-a/members', json={'user_id': 'person-a'}).status_code == 200
    assert client.put('/api/cameras/cam-a/active-user', json={'user_id': 'person-b'}).status_code == 422
    assert client.delete('/api/admin/cameras/cam-a/members/person-a').status_code == 409
    members = client.get('/api/cameras/cam-a/members').json()
    assert members['active_user_id'] is None
    assert [row['user_id'] for row in members['members']] == ['person-a']


def test_v12_multiple_members_without_current_user_are_not_arbitrarily_selected(tmp_path):
    db = Database(tmp_path / 'multiple.sqlite3')
    apply_migrations(db)
    with db.transaction('IMMEDIATE') as conn:
        seed_personnel(conn)
        conn.executemany('INSERT INTO camera_users VALUES(?,?)', [('camera', 'person-a'), ('camera', 'person-b')])
        conn.execute('DELETE FROM schema_migrations WHERE version>=12')
    assert [row['version'] for row in apply_migrations(db)] == [12, 13, 14]
    with db.connection() as conn:
        assert conn.execute('SELECT count(*) FROM camera_users').fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM camera_active_users').fetchone()[0] == 0
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


def test_v14_preserves_explicit_old_selection_and_removes_default_trigger(tmp_path):
    db = Database(tmp_path / 'selected.sqlite3')
    apply_migrations(db)
    with db.transaction('IMMEDIATE') as conn:
        seed_personnel(conn)
        conn.execute("INSERT INTO camera_users VALUES('camera','person-a')")
        conn.execute("INSERT INTO camera_active_users VALUES('camera','person-a')")
        conn.execute('''CREATE TRIGGER camera_member_default_active AFTER INSERT ON camera_users
                        WHEN NOT EXISTS (SELECT 1 FROM camera_active_users WHERE camera_id=NEW.camera_id)
                        BEGIN INSERT INTO camera_active_users VALUES(NEW.camera_id,NEW.user_id); END''')
        conn.execute('DELETE FROM schema_migrations WHERE version=14')
    assert [row['version'] for row in apply_migrations(db)] == [14]
    with db.transaction('IMMEDIATE') as conn:
        assert tuple(conn.execute('SELECT * FROM camera_active_users').fetchone()) == ('camera', 'person-a')
        conn.execute("INSERT INTO camera_users VALUES('new-camera','person-b')")
        assert conn.execute("SELECT user_id FROM camera_active_users WHERE camera_id='new-camera'").fetchone() is None
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='camera_member_default_active'").fetchone()


def test_handover_between_scan_check_and_binding_transaction_does_not_restore_old_user(direct, monkeypatch):
    app, client = direct
    with app.db() as conn:
        seed_personnel(conn)
    for user_id in ('person-a', 'person-b'):
        assert client.post('/api/admin/cameras/cam-a/members', json={'user_id': user_id}).status_code == 200
    assert client.put('/api/cameras/cam-a/active-user', json={'user_id': 'person-a'}).status_code == 200
    camera = Camera()
    camera.target = 'cam-a'
    camera.publish(label(app, 'InstrumentA'))
    frame, metadata = camera.frame()
    decoded = decode_qr(frame)
    matches = app.qr_matches(*decoded[:2])
    assert len(matches['matches']) == 1 and not matches['scene_matches']
    instrument_id = matches['matches'][0]['id']
    assert client.put('/api/instruments/' + instrument_id,
        json={'name': '隔离测试仪器', 'scene': '隔离实验台'}).status_code == 200
    monkeypatch.setattr(app, 'receiver_camera', camera)
    original_save = app.save_camera_scan

    def handover_after_scanner_check(*args, **kwargs):
        # The scan already passed its initial read-only active-user check. Commit
        # another request before persist_binding starts its IMMEDIATE transaction.
        response = client.put('/api/cameras/cam-a/active-user', json={'user_id': 'person-b'})
        assert response.status_code == 200, response.text
        return original_save(*args, **kwargs)

    monkeypatch.setattr(app, 'save_camera_scan', handover_after_scanner_check)
    scanner = LiveScanner(vars(app))
    scanner.session = {'session_id': str(uuid.uuid4()), 'operator': '甲', 'wearer_id': 'person-a',
                       'status': 'scanning', 'camera_id': 'cam-a', 'bindings': [], 'scene_visits': []}
    with scanner.lock:
        scanner._accept(frame, metadata, decoded, matches)
    assert scanner.session['binding_errors']
    assert scanner.session['bindings'] == []
    assert client.get('/api/cameras/cam-a/members').json()['active_user_id'] == 'person-b'
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM bindings').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM camera_users').fetchone()[0] == 2


def test_authenticated_admin_scan_does_not_implicitly_register_personnel(managed):
    from pathlib import Path

    app, client, auth, admin, first = managed
    headers = bearer(auth, 'admin', 'correct horse battery')
    for name in ('Scene01', 'InstrumentA'):
        path = Path(__file__).parents[1] / 'static' / 'labels' / (name + '.png')
        scanned = client.post('/api/scans', files={'file': (path.name, path.read_bytes(), 'image/png')},
                              data={'camera_id': 'cam-a'}, headers=headers)
        assert scanned.status_code == 200, scanned.text
        picture = scanned.json()
        if name == 'Scene01':
            result = client.post('/api/scene/enter', json={'scan_id': picture['scan_id'],
                'scene_id': picture['scene_matches'][0]['id'], 'operator': admin.display_name}, headers=headers)
        else:
            instrument_id = picture['matches'][0]['id']
            assert client.put('/api/instruments/' + instrument_id, json={'name': '隔离仪器', 'scene': '隔离实验台'},
                              headers=headers).status_code == 200
            result = client.post('/api/bindings', json={'scan_id': picture['scan_id'],
                'instrument_id': instrument_id, 'operator': admin.display_name}, headers=headers)
        assert result.status_code == 422, result.text
    with app.db() as conn:
        assert conn.execute('SELECT user_id FROM camera_users WHERE camera_id=?', ('cam-a',)).fetchall()[0][0] == first.id
        assert conn.execute('SELECT count(*) FROM camera_users').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM camera_active_users').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM scene_visits').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM bindings').fetchone()[0] == 0
