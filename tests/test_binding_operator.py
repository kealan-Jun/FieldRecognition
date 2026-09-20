import json
import threading
from types import SimpleNamespace

from automation import AutomaticRunner, AutomationSettings
from binding_operator import at_time
from prepare_runtime import prepare
from database import Database, apply_migrations
from readout_context import at_capture
from test_demo import app_client, scan, register  # noqa: F401
from test_archive_store import archive, receipts  # noqa: F401
from test_direct_access import direct  # noqa: F401


def test_replayed_qr_opens_successor_preserving_evidence_and_photo_time(archive, monkeypatch):
    app, client, root = archive
    clock = ['2026-09-18T01:00:00+00:00']
    monkeypatch.setattr(app, 'now', lambda: clock[0])
    picture = scan(client)
    iid = picture['matches'][0]['id']
    register(client, iid)
    body = {'scan_id': picture['scan_id'], 'instrument_id': iid, 'operator': '甲', 'wearer_id': 'person-a'}
    original = client.post('/api/bindings', json=body).json()
    app.archive_store.step(batch_size=100)
    old_receipts = {p: p.read_bytes() for p in root.glob('.System/Receipts/**/*.json')}
    clock[0] = '2026-09-18T02:00:00+00:00'
    response = client.post('/api/bindings', json=body | {'operator': '乙', 'wearer_id': 'person-b'})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated['binding_id'] != original['binding_id']
    assert updated['started_at'] == clock[0]
    assert updated['supersedes_binding_id'] == original['binding_id']
    for key in ('valid_until', 'qr_hash', 'scan_id', 'image_url'):
        assert updated[key] == original[key]
    assert updated['operator'] == '乙' and updated['wearer_id'] == 'person-b'
    assert updated['session_revision'] == 2
    assert client.post('/api/bindings', json=body | {'operator': '乙', 'wearer_id': 'person-b'}).json() == updated
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM bindings').fetchone()[0] == 2
        original_closed = json.loads(conn.execute('SELECT document FROM bindings WHERE id=?',(original['binding_id'],)).fetchone()[0])
        assert original_closed['operator'] == '甲' and original_closed['ended_at'] == clock[0]
        for captured_at, expected in [('2026-09-18T01:30:00+00:00', '甲'), (clock[0], '乙')]:
            photo = {'camera_id': 'TestCamera', 'external_photo': {'captured_at': captured_at}}
            for linked in (None, original_closed if expected == '甲' else updated):
                context = at_capture(vars(app), conn, photo, linked=linked, automatic=True)
                assert context['resolved_binding']['operator'] == expected
    app.archive_store.step(batch_size=100)
    versions = [r['document'] for r in receipts(root) if r['entity_id'] == original['binding_id']]
    assert {v['operator'] for v in versions} == {'甲'}
    assert any(v['ended_at'] == clock[0] for v in versions)
    assert all(p.read_bytes() == raw for p, raw in old_receipts.items())


def test_scene_replacement_does_not_keep_previous_person_id(app_client):
    app, client = app_client
    picture = scan(client, 'Scene01')
    body = {'scan_id': picture['scan_id'], 'scene_id': picture['scene_matches'][0]['id'], 'operator': '甲'}
    original = client.post('/api/scene/enter', json=body).json()
    with app.db() as conn:
        original['wearer_id'] = 'person-a'
        conn.execute('UPDATE scene_visits SET document=? WHERE id=?', (json.dumps(original), original['visit_id']))
    updated = client.post('/api/scene/enter', json=body | {'operator': '乙'}).json()
    assert updated['wearer_id'] is None
    assert updated['supersedes_visit_id'] == original['visit_id']
    with app.db() as conn:
        closed = json.loads(conn.execute('SELECT document FROM scene_visits WHERE id=?',(original['visit_id'],)).fetchone()[0])
    assert closed['wearer_id'] == 'person-a' and closed['ended_at'] == updated['started_at']


def test_runtime_member_switch_updates_binding_and_background_settings(direct, monkeypatch):
    app, client = direct
    first = prepare(app.DATA, 'person-a', '甲', 'cam-a')
    picture = scan(client, camera='cam-a')
    iid = picture['matches'][0]['id']
    register(client, iid, camera='cam-a')
    original = client.post('/api/bindings', json={'scan_id': picture['scan_id'], 'instrument_id': iid, 'operator': '甲'}).json()
    assert original['wearer_id'] == first['user_id']
    with app.db() as conn:
        conn.execute('INSERT INTO automation_settings VALUES(?,?)', ('cam-a', json.dumps({
            'enabled': False, 'operator': '甲', 'wearer_id': first['user_id'], 'registered_at': original['started_at']})))
    second = prepare(app.DATA, 'person-b', '乙', 'cam-a')
    members = client.get('/api/admin/cameras/cam-a/members').json()['members']
    assert {m['user_id'] for m in members} == {first['user_id'], second['user_id']}
    with app.db() as conn:
        binding = json.loads(conn.execute('SELECT document FROM bindings WHERE id=?', (original['binding_id'],)).fetchone()[0])
        settings = json.loads(conn.execute('SELECT document FROM automation_settings WHERE camera=?', ('cam-a',)).fetchone()[0])
    assert binding['operator'] == '甲' and binding['wearer_id'] == first['user_id']
    assert settings['operator'] == '乙' and settings['wearer_id'] == second['user_id']
    with app.db() as conn:
        successor = json.loads(conn.execute('SELECT document FROM bindings WHERE camera=? AND ended IS NULL',('cam-a',)).fetchone()[0])
    assert successor['operator'] == '乙' and successor['wearer_id'] == second['user_id']
    assert at_time(settings, original['started_at'])['operator'] == '甲'
    assert binding['ended_at'] == successor['started_at'] and binding['started_at'] == original['started_at']

    # Exercise the worker's configuration path without connecting to a receiver.
    runner = AutomaticRunner(dict(vars(app), receiver_camera=None,
        live_scanner=SimpleNamespace(lock=threading.RLock(), session=None)))
    settings = runner.configure(AutomationSettings(enabled=True, operator='甲'))
    assert settings['wearer_id'] == first['user_id'] and settings['operator'] == '甲'
    with app.db() as conn:
        assert conn.execute('SELECT user_id FROM camera_active_users WHERE camera_id=?', ('cam-a',)).fetchone()[0] == first['user_id']
    response = client.put('/api/admin/cameras/cam-a/owner', json={'user_id': second['user_id']})
    assert response.status_code == 200
    assert runner.settings()['wearer_id'] == second['user_id']
    assert client.delete('/api/admin/cameras/cam-a/members/' + second['user_id']).status_code == 409
    assert client.delete('/api/admin/cameras/cam-a/members/' + first['user_id']).status_code == 200


def test_same_name_members_are_not_assigned_arbitrarily(direct):
    app, client = direct
    first = prepare(app.DATA, 'same-a', '同名', 'cam-a')
    second = prepare(app.DATA, 'same-b', '同名', 'cam-a')
    picture = scan(client, camera='cam-a')
    iid = picture['matches'][0]['id']
    register(client, iid, camera='cam-a')
    body = {'scan_id': picture['scan_id'], 'instrument_id': iid, 'operator': '同名'}
    assert client.post('/api/bindings', json=body).status_code == 422
    assert client.post('/api/bindings', json=body | {'wearer_id': first['user_id']}).json()['wearer_id'] == first['user_id']
    assert client.post('/api/bindings', json=body | {'operator': '其他人', 'wearer_id': second['user_id']}).status_code == 422


def test_existing_single_member_schema_migrates_without_changing_records(tmp_path):
    first = prepare(tmp_path, 'person-a', '甲', 'cam-a')
    db = Database(first['database'])
    with db.transaction('IMMEDIATE') as conn:
        conn.execute('DROP TABLE camera_users')
        conn.execute('DROP TABLE camera_active_users')
        conn.execute('CREATE TABLE camera_users(camera_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id))')
        conn.execute('INSERT INTO camera_users VALUES(?,?)', ('cam-a', first['user_id']))
        conn.execute('DELETE FROM schema_migrations WHERE version=12')
        conn.execute('INSERT INTO bindings VALUES(?,?,?,?)', ('history', 'cam-a', 'old-time', '{"operator":"原人员"}'))
    assert apply_migrations(db)[0]['version'] == 12
    assert apply_migrations(db) == []
    with db.connection() as conn:
        assert conn.execute('SELECT user_id FROM camera_active_users WHERE camera_id=?', ('cam-a',)).fetchone()[0] == first['user_id']
        assert conn.execute('SELECT document FROM bindings WHERE id=?', ('history',)).fetchone()[0] == '{"operator":"原人员"}'
    second = prepare(tmp_path, 'person-b', '乙', 'cam-a')
    with db.connection() as conn:
        assert {r[0] for r in conn.execute('SELECT user_id FROM camera_users')} == {first['user_id'], second['user_id']}
