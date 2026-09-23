import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

from evidence_timeline import capture_time
from security import Principal, identity
from test_demo import app_client  # noqa: F401
from test_measurement_records import document


def evidence(ident, captured_at, stream='voice', camera='Camera1'):
    doc = document()
    doc.update(job_id=ident, capture_id='capture-'+ident, camera_id=camera,
               record_scope='draft' if stream == 'voice' else 'field_observation',
               submitted_at='2026-09-23T02:00:00Z')
    if stream == 'video':
        doc.pop('external_photo')
        doc.update(request_trigger='video_stream', video_observation={'captured_at': captured_at,
            'clock_sync_valid': True, 'capture_time_basis': 'synchronized_frame_timestamp'})
    else:
        doc.update(request_trigger='voice_photo_directory', external_photo={'captured_at': captured_at,
                    'timestamp_basis': 'nas_filename_local_time_not_hardware_verified'})
    return doc


def store(app, docs):
    with app.db() as conn:
        for doc in docs:
            conn.execute('INSERT INTO jobs(id,status,document) VALUES(?,?,?)',
                         (doc['job_id'], doc['status'], json.dumps(doc)))


def read(client, **kwargs):
    return client.get('/api/evidence-timeline', params={'camera_id': 'Camera1',
        'start': '2026-09-22T18:00:00+08:00', 'end': '2026-09-22T18:01:00+08:00', **kwargs})


def test_two_streams_sorted_by_capture_not_arrival_and_old_receipts_unchanged(app_client):
    app, client = app_client
    docs = [evidence('later', '2026-09-22T18:00:30+08:00'),
            evidence('video', '2026-09-22T10:00:10Z', 'video'),
            evidence('late_import', '2026-09-22T18:00:00.123+08:00'),
            evidence('other_camera', '2026-09-22T10:00:01Z', camera='Camera2')]
    store(app, docs)
    with app.db() as conn:
        before = [tuple(r) for r in conn.execute('SELECT * FROM jobs')]
    reply = read(client, limit=2)
    assert reply.status_code == 200, reply.text
    page = reply.json()
    assert [e['sources'][0]['job_id'] for e in page['items']] == ['late_import', 'video']
    assert page['next_offset'] == 2 and page['total'] == 3
    assert page['items'][0]['sources'][0]['acquisition']['time']['quality'] == 'filename_time'
    assert page['items'][1]['sources'][0]['acquisition']['time']['quality'] == 'synchronized_frame'
    assert page['items'][0]['sources'][0]['measurements']  # Drafts remain inspectable evidence.
    assert read(client, offset=2).json()['items'][0]['sources'][0]['job_id'] == 'later'
    with app.db() as conn:
        assert before == [tuple(r) for r in conn.execute('SELECT * FROM jobs')]


def test_unsynchronized_video_and_naive_times_are_not_placed_using_receive_time(app_client):
    app, client = app_client
    video = evidence('unsynced', '2026-09-22T10:00:10Z', 'video')
    video['video_observation']['clock_sync_valid'] = False
    video['submitted_at'] = '2026-09-22T10:00:10Z'
    photo = evidence('naive', '2026-09-22T18:00:10')
    store(app, [video, photo])
    reply = read(client).json()
    assert reply['items'] == [] and reply['unknown_time_events'] == 2
    assert capture_time(video)['timestamp_ms'] is None


def test_burst_members_stay_together_across_query_boundary_and_versions_do_not_duplicate(app_client):
    app, client = app_client
    docs = [evidence(str(i), t) for i, t in enumerate([
        '2026-09-22T18:00:59.800+08:00', '2026-09-22T18:01:00+08:00', '2026-09-22T18:01:00.200+08:00'])]
    context = {'burst_id': 'explicit-1', 'expected_photos': 3}
    for d in docs:
        d.update(measurement_id='measurement-1', measurement_context=context)
    revised = copy.deepcopy(docs[0])
    revised.update(job_id='revision', recognition_root_job_id='0', recognition_revision=2)
    pending = copy.deepcopy(revised)
    pending.update(job_id='pending', status='running', recognition_revision=3)
    store(app, docs+[revised, pending])
    with app.db() as conn:
        conn.execute('INSERT INTO photo_measurements VALUES(?,?,?,?,?)', ('measurement-1', 'Camera1', 'explicit-1',
            'draft', json.dumps({'context': context, 'status': 'draft', 'record_scope': 'draft'})))
    page = read(client).json()
    assert page['total'] == 1
    event = page['items'][0]
    assert event['batch_status'] == 'complete' and event['received_photos'] == 3
    assert [s['job_id'] for s in event['sources']] == ['revision', '1', '2']
    assert event['end_at'] == '2026-09-22T10:01:00.200000+00:00'


def test_no_time_proximity_grouping_no_manual_line_and_filters(app_client):
    app, client = app_client
    docs = [evidence('one', '2026-09-22T10:00:10Z'), evidence('two', '2026-09-22T10:00:10Z')]
    manual = evidence('manual', '2026-09-22T10:00:10Z')
    manual.pop('external_photo'); manual['request_trigger'] = 'explicit_request'
    store(app, docs+[manual])
    result = read(client, instrument_id='a', wearer_id='wearer-7').json()
    assert result['total'] == 2 and all(e['batch_status'] == 'unverified' for e in result['items'])
    assert all([m['instrument_id'] for m in e['sources'][0]['measurements']] == ['a'] for e in result['items'])
    assert read(client, instrument_id='missing').json()['total'] == 0
    assert read(client, stream='video').json()['total'] == 0


@pytest.mark.parametrize('params', [
    {'start':'2026-09-22T10:00:00'}, {'end':'2026-09-22T09:00:00Z'},
    {'end':'2027-01-01T00:00:00Z'}, {'limit':101}, {'offset':-1}, {'stream':'upload'}])
def test_invalid_range_rejected(app_client, params):
    assert read(app_client[1], **params).status_code == 422


def test_camera_permission_is_enforced_before_reading(app_client):
    from evidence_timeline import timeline
    app, _ = app_client
    store(app, [evidence('private', '2026-09-22T10:00:10Z')])
    token = identity.set(Principal('user', 'name', 'operator', frozenset({'Camera2'})))
    try:
        with app.db() as conn:
            result = timeline(conn, 'Camera1', start=datetime(2026,9,22,tzinfo=timezone.utc),
                              end=datetime(2026,9,23,tzinfo=timezone.utc))
        assert result['items'] == []
    finally:
        identity.reset(token)
