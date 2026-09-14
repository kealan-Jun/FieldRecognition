from datetime import datetime, timezone, timedelta

from PIL import Image

from photo_watch import SavedPhotoWatcher
from test_demo import app_client, register, scan  # noqa: F401


def test_bound_camera_watch_waits_for_stable_files_and_survives_restart(app_client, monkeypatch, tmp_path):
    app, client = app_client
    root = tmp_path / 'voice_photos'
    root.mkdir()
    monkeypatch.setenv('FIELD_SAVED_PHOTO_ROOT', str(root))
    monkeypatch.setenv('FIELD_CAMERA_ID', 'TestCamera')
    monkeypatch.setenv('FIELD_SAVED_PHOTO_WATCH_ENABLED', '1')
    monkeypatch.setenv('FIELD_SAVED_PHOTO_TIMEZONE', 'Asia/Shanghai')
    queued = []
    monkeypatch.setattr(app.readout_pool, 'submit', lambda fn, job: queued.append(job))
    clock = [0.0]
    watcher = SavedPhotoWatcher(vars(app), clock=lambda: clock[0])
    watcher.step()
    assert watcher.snapshot()['status'] == 'waiting_photo'
    first = scan(client)
    aid = first['matches'][0]['id']
    register(client, aid)
    binding = client.post('/api/bindings', json={'scan_id': first['scan_id'], 'instrument_id': aid, 'operator': 'VoiceTester'}).json()
    app.ocr_warmup_future.result(timeout=2)
    stamp = datetime.now(timezone(timedelta(hours=8))) + timedelta(seconds=1)

    def photo(camera, when, suffix=''):
        path = root / camera / when.strftime('%Y-%m-%d/%H-%M-%S') / (when.strftime('%Y%m%d_%H%M%S') + suffix + '.jpg')
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (400, 300), 'gray').save(path)
        return path

    old = photo('TestCamera', stamp - timedelta(days=1))
    photo('OtherCamera', stamp)
    current = photo('TestCamera', stamp)
    original = current.read_bytes()
    watcher.step()
    assert not queued and watcher.snapshot()['pending_files'] == 1
    clock[0] = 2
    watcher.step()
    assert len(queued) == 1
    job = queued[0]
    assert job['request_trigger'] == 'voice_photo_directory'
    assert job['external_photo']['source_ref'] == str(current)
    assert current.read_bytes() == original and old.exists()
    watcher.step()
    assert len(queued) == 1
    restarted = SavedPhotoWatcher(vars(app), clock=lambda: clock[0])
    restarted.step()
    clock[0] = 4
    restarted.step()
    assert len(queued) == 1  # Durable receipt, not just an in-memory filename cache.

    # A newly appearing partial write must stabilize again after its bytes change.
    pending = current.with_name(stamp.strftime('%Y%m%d_%H%M%S_001.jpg'))
    pending.write_bytes(b'partial')
    restarted.step()
    clock[0] = 6
    pending.write_bytes(original)
    restarted.step()
    assert len(queued) == 1
    clock[0] = 8
    restarted.step()
    assert len(queued) == 2
    client.post('/api/bindings/' + binding['binding_id'] + '/end')
    photo('TestCamera', stamp, '_002')
    clock[0] = 10
    restarted.step()
    assert len(queued) == 2 and restarted.snapshot()['status'] == 'watching'
    clock[0] = 12
    restarted.step()
    assert len(queued) == 3 and queued[-1]['binding_id'] is None
    assert queued[-1]['instrument'] is None
    app.run_ocr(job)
    assert app.get_job(job['job_id'])['status'] == 'completed'
    watcher.close()
    restarted.close()


def test_unbound_photo_watch_records_detection_and_no_duplicate_after_restart(app_client, monkeypatch, tmp_path):
    app, client = app_client
    root = tmp_path / 'voice_photos'
    root.mkdir()
    monkeypatch.setenv('FIELD_SAVED_PHOTO_ROOT', str(root))
    monkeypatch.setenv('FIELD_CAMERA_ID', 'TestCamera')
    monkeypatch.setenv('FIELD_SAVED_PHOTO_WATCH_ENABLED', '1')
    client.put('/api/automation', json={'enabled': False, 'operator': '实验员甲'})
    queued = []
    monkeypatch.setattr(app.readout_pool, 'submit', lambda fn, job: queued.append(job))
    clock = [0.0]
    watcher = SavedPhotoWatcher(vars(app), clock=lambda: clock[0])
    watcher.step()
    stamp = datetime.now(timezone(timedelta(hours=8))) + timedelta(seconds=1)
    path = root / 'TestCamera' / stamp.strftime('%Y-%m-%d/%H-%M-%S/%Y%m%d_%H%M%S.jpg')
    path.parent.mkdir(parents=True)
    Image.new('RGB', (400, 300), 'gray').save(path)
    raw = path.read_bytes()
    watcher.step()
    assert not queued and watcher.snapshot()['pending_files'] == 1
    clock[0] = .6
    watcher.step()
    assert len(queued) == 1
    job = queued[0]
    assert job['binding_id'] is None and job['instrument'] is None and job['scene'] is None
    assert job['operator'] == '实验员甲' and job['operator_basis'] == 'camera_registration'
    assert job['timing']['first_observed_at'] and job['timing']['stable_at']
    assert job['timing']['source_written_at'] and job['timing']['imported_at']
    assert path.read_bytes() == raw
    assert watcher.snapshot()['poll_seconds'] == .5
    restarted = SavedPhotoWatcher(vars(app), clock=lambda: clock[0])
    restarted.step()
    assert len(queued) == 1
    app.run_ocr(job)
    assert app.get_job(job['job_id'])['status'] == 'completed'
    events = client.get('/api/state').json()['activity']
    assert not any(e['kind']=='readout' for e in events)
    assert app.get_job(job['job_id'])['instrument'] is None
    watcher.close()
    restarted.close()
