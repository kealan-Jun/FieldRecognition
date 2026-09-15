import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from PIL import Image

from photo_job_queue import recover
from photo_watch import SavedPhotoWatcher
from test_demo import app_client  # noqa: F401
from test_photo_measurements import setup, enqueue, complete, get_draft  # noqa: F401


def test_nas_read_failure_is_persistent_and_recovers_once(app_client, tmp_path, monkeypatch):
    app, client = app_client
    root = tmp_path / 'voice_photos'
    root.mkdir()
    monkeypatch.setenv('FIELD_SAVED_PHOTO_ROOT', str(root))
    monkeypatch.setenv('FIELD_CAMERA_ID', 'TestCamera')
    monkeypatch.setenv('FIELD_SAVED_PHOTO_WATCH_ENABLED', '1')
    client.put('/api/automation', json={'enabled': False, 'operator': 'Reader'})
    clock = [0.0]
    watcher = SavedPhotoWatcher(vars(app), clock=lambda: clock[0])
    watcher.step()
    stamp = datetime.now(timezone(timedelta(hours=8))) + timedelta(seconds=1)
    path = root / 'TestCamera' / stamp.strftime('%Y-%m-%d/%H-%M-%S/%Y%m%d_%H%M%S.jpg')
    path.parent.mkdir(parents=True)
    Image.new('RGB', (400, 300), 'gray').save(path)
    raw = path.read_bytes()
    watcher.step()
    original = app.read_saved_panel
    def disconnected(*args, **kwargs):
        raise HTTPException(503, 'NAS IO_DEVICE_ERROR')
    monkeypatch.setattr(app, 'read_saved_panel', disconnected)
    clock[0] = .6
    watcher.step()
    assert watcher.snapshot()['persisted_pending_files'] == 1
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM photo_watch_files').fetchone()[0] == 0
        previous = json.loads(conn.execute('SELECT document FROM photo_ingest_queue').fetchone()[0])
        previous['retry_after'] = 0
        conn.execute('UPDATE photo_ingest_queue SET document=?', (json.dumps(previous),))
    queued = []
    monkeypatch.setattr(app.readout_pool, 'submit', lambda fn, job: queued.append(job))
    monkeypatch.setattr(app, 'read_saved_panel', original)
    restarted = SavedPhotoWatcher(vars(app), clock=lambda: clock[0])
    clock[0] = 1.2
    restarted.step()
    assert len(queued) == 1 and restarted.snapshot()['persisted_pending_files'] == 0
    assert queued[0]['timing']['first_observed_at'] == previous['first_observed_at']
    assert queued[0]['timing']['ingest_retry_count']==1
    assert queued[0]['timing']['ingest_last_error']=='NAS IO_DEVICE_ERROR'
    assert queued[0]['timing']['ingest_first_failed_at']==previous['first_failed_at']
    restarted.step()
    assert len(queued) == 1 and path.read_bytes() == raw


def test_interrupted_ocr_uses_same_job_and_draft_and_preserves_attempt(setup):
    app, client, _, queued = setup
    reply, request = enqueue(setup, 1)
    job_id, mid = reply['job_ids'][0], reply['measurement_id']
    with app.db() as conn:
        conn.execute("UPDATE jobs SET status='interrupted' WHERE id=?", (job_id,))
    assert recover(vars(app)) == [job_id]
    assert recover(vars(app)) == []
    assert queued[-1]['job_id'] == job_id and queued[-1]['measurement_id'] == mid
    assert queued[-1]['attempt_history'][0]['status'] == 'interrupted'
    complete(setup, 1)
    draft = get_draft(client, mid)
    assert draft['status'] == 'draft' and len(draft['job_ids']) == 1
    response = client.post(f'/api/photo-measurements/{mid}/confirm', json={'actor': 'Reader', 'revision': draft['revision']})
    assert response.status_code == 200
    assert client.get('/api/experiment-records/' + mid).json() == response.json()
    assert client.post('/api/ocr/photo-burst', json=request).json()['job_ids'] == [job_id]
    with app.db() as conn:
        assert conn.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM experiment_records').fetchone()[0] == 1
