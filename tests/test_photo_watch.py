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
    assert watcher.snapshot()['status'] == 'waiting_binding'
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
    assert len(queued) == 2 and restarted.snapshot()['status'] == 'waiting_binding'
    app.run_ocr(job)
    assert app.get_job(job['job_id'])['status'] == 'cancelled'
    watcher.close()
    restarted.close()
