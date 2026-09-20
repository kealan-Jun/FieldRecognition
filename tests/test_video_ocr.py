from concurrent.futures import Future
import json

import numpy as np
import pytest

from test_demo import app_client, wait_for_job  # noqa: F401
from test_live_scan import Camera
from video_ocr import VideoOcr


@pytest.fixture
def video(app_client, monkeypatch):
    app, client = app_client
    app.video_ocr.close()
    camera = Camera()
    monkeypatch.setattr(app, 'receiver_camera', camera)
    monkeypatch.setattr(camera, 'snapshot', lambda: {'service_status_available': True,
        'service_status': {'online': True, 'media_session_id': 5}})
    client.put('/api/automation', json={'enabled': False, 'operator': '甲'})
    assert client.put('/api/video-ocr', json={'enabled': True}).status_code == 200
    assert app.ocr_warmup_future.result(timeout=2)
    tick = [0.]
    reader = VideoOcr(vars(app), clock=lambda: tick[0])
    frame = np.zeros((240, 320, 3), np.uint8)
    return app, client, camera, reader, frame, tick


def local(text='12.34 g'):
    return {'status': 'completed', 'device': 'cpu', 'model': 'test', 'actual_model_invocation': True,
            'lines': [{'text': text, 'confidence': .9, 'polygon': [[30,30],[130,30],[130,50],[30,50]]}] if text else []}


def accept(context, text='12.34 g'):
    app, _, _, reader, frame, tick = context
    reader.accept(frame, {'sequence': int(tick[0] * 100)}, app.now(), local(text))


def test_video_only_changed_stable_evidence_is_saved_and_ocr_reused(video, monkeypatch):
    app, client, _, reader, frame, tick = video
    monkeypatch.setattr(app, 'predict_panel', lambda *args: pytest.fail('Evidence must reuse the video inference'))
    accept(video)
    assert not list((app.DATA/'Images').iterdir())
    tick[0] = .5
    accept(video)
    jid = reader.state['last_job_id']
    done = wait_for_job(client, jid)
    assert done['request_trigger'] == 'video_stream' and done['readings'][0]['value'] == '12.34'
    assert done['video_observation']['stable_samples'] == 2
    for n in range(1, 15):
        tick[0] = n + .5
        accept(video)
    with app.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 1
    tick[0] = 16
    accept(video, '20.00 g')
    tick[0] = 16.5
    accept(video, '20.00 g')
    assert reader.state['evidence_saved'] == 2
    wait_for_job(client, reader.state['last_job_id'])


def test_unreadable_visible_instrument_is_saved_once_not_periodically(video):
    import cv2
    app, client, _, reader, _, tick = video
    video = (*video[:4], cv2.imread(str(app.BASE/'static/labels/InstrumentA.png')), tick)
    accept(video, None)
    tick[0] = 4.9
    accept(video, None)
    assert reader.state['evidence_saved'] == 0
    tick[0] = 5
    accept(video, None)
    assert reader.state['evidence_saved'] == 1
    done = wait_for_job(client, reader.state['last_job_id'])
    assert done['video_observation']['no_digits_elapsed_seconds'] == 5
    tick[0] = 34.9
    accept(video, None)
    assert reader.state['evidence_saved'] == 1
    tick[0] = 35
    accept(video, None)
    assert reader.state['evidence_saved'] == 1


def test_background_without_reading_or_instrument_never_reaches_storage(video):
    app, _, _, reader, _, tick = video
    for moment in (0, .5, 5, 35, 1000):
        tick[0] = moment
        accept(video, None)
    assert reader.state['frames_inferred'] == reader.state['background_frames_skipped'] == 5
    assert reader.state['evidence_saved'] == 0
    assert not list((app.DATA/'Images').iterdir())
    with app.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 0


def test_one_inflight_frame_and_saved_photo_priority(video, monkeypatch):
    app, _, camera, reader, frame, tick = video
    calls, future = [], Future()
    monkeypatch.setattr(app.ocr_pool, 'submit', lambda *args, **kwargs: calls.append(args) or future)
    camera.publish(frame)
    reader.step()
    assert len(calls) == 1
    tick[0] = 1
    camera.publish(frame)
    reader.step()
    assert len(calls) == 1
    future.set_result(local())
    monkeypatch.setattr(app.saved_photo_watcher, 'snapshot', lambda: {'pending_files': 1})
    reader.step()
    assert reader.state['frames_inferred'] == 1 and reader.state['status'] == 'yielding_to_photo'
    assert len(calls) == 1


def test_stale_inference_and_device_epoch_are_not_saved(video, monkeypatch):
    app, _, camera, reader, frame, tick = video
    future = Future()
    monkeypatch.setattr(app.ocr_pool, 'submit', lambda *args, **kwargs: future)
    camera.publish(frame)
    reader.step()
    tick[0] = 4
    future.set_result(local())
    reader.step()
    assert reader.state['frames_inferred'] == 0
    assert reader.state['evidence_saved'] == 0


def test_paused_video_status_persists_without_pausing_photos(video):
    app, client, _, reader, _, _ = video
    assert client.put('/api/video-ocr', json={'enabled': False}).status_code == 200
    assert not VideoOcr(vars(app)).enabled()
    assert app.saved_photo_watcher.snapshot()['enabled'] is False  # Unchanged fixture setting.
    reader.step()
    assert reader.state['status'] == 'paused'


def test_panel_video_without_binding_never_schedules_inference(video,monkeypatch):
    app,_,camera,reader,frame,tick=video
    monkeypatch.setenv('FIELD_PANEL_DETECTOR_ENABLED','1')
    monkeypatch.setattr(app.ocr_pool,'submit',lambda *a,**kw:pytest.fail('No binding'))
    camera.publish(frame)
    reader.step()
    assert reader.state['status']=='waiting_binding'
    assert reader.state['frames_inferred']==reader.state['evidence_saved']==0


def test_binding_ended_during_panel_inference_discards_result(video,monkeypatch):
    app,_,_,reader,frame,tick=video
    monkeypatch.setenv('FIELD_PANEL_DETECTOR_ENABLED','1')
    result=local()
    result.update(panel_detection={},panel_regions=[{'instrument_id':'ended-instrument'}])
    for moment in (0,.5,5):
        tick[0]=moment
        reader.accept(frame,{},app.now(),result)
    assert reader.state['evidence_saved']==0
    assert not list((app.DATA/'Images').iterdir())


def test_same_value_on_another_decoded_instrument_is_new_evidence(video):
    import cv2
    app, client, camera, reader, _, tick = video
    for i, name in enumerate(('InstrumentA', 'InstrumentB')):
        pixels = cv2.imread(str(app.BASE/'static/labels'/f'{name}.png'))
        for delta in (0, .5):
            tick[0] = i * 3 + delta
            reader.accept(pixels, {'sequence': i * 10 + int(delta * 10)}, app.now(), local())
        result = wait_for_job(client, reader.state['last_job_id'])
        assert len(result['qr_matches']) == 1
    assert reader.state['evidence_saved'] == 2


def test_localized_panel_votes_and_saved_receipts_are_independent(video,monkeypatch):
    from datetime import datetime
    def synced(sequence):
        return {'sequence':sequence,'clock_sync_valid':True,
                'global_timestamp_us':int(datetime.fromisoformat(app.now()).timestamp()*1_000_000)}
    from test_multi_readout import two_bindings
    from test_panel_regions import BOXES
    app,client,camera,reader,frame,tick=video
    bindings=two_bindings(app,client)
    monkeypatch.setenv('FIELD_PANEL_DETECTOR_ENABLED','1')
    monkeypatch.setattr(app.panel_detector,'predict',lambda *a:pytest.fail('Reuse frame inference'))
    def sample(a,b):
        regions=[]
        for i,(text,box) in enumerate(zip((a,b),BOXES)):
            result=local(text);result['lines'][0]['panel_id']=str(i)
            x,y,x2,y2=box['xyxy']
            regions.append({'panel_id':str(i),'class_id':i,'instrument_id':box['instrument_id'],
                'bbox':box['xyxy'],'crop':[x,y,x2-x,y2-y],'detector_confidence':.9,'local_ocr':result})
        return {'panel_detection':{'status':'completed'},'panel_regions':regions,
                'lines':[l for r in regions for l in r['local_ocr']['lines']],'device':'cpu','status':'completed'}
    reader.accept(frame,synced(1),app.now(),sample('100','0.0000'))
    assert reader.state['latest_lines']==[] and reader.state['evidence_saved']==0
    tick[0]=.5
    reader.accept(frame,synced(2),app.now(),sample('101','0.0000'))
    result=wait_for_job(client,reader.state['last_job_id'])
    assert [r['text'] for r in result['readings']]==['0.0000']
    assert result['readings'][0]['binding_id']==bindings[1]['binding_id']
    assert result['readings'][0]['temporal_confirmation']['votes']==2
    tick[0]=1
    reader.accept(frame,synced(3),app.now(),sample('101','0.0000'))
    result=wait_for_job(client,reader.state['last_job_id'])
    assert [r['text'] for r in result['readings']]==['101']
    assert result['readings'][0]['binding_id']==bindings[0]['binding_id']
    assert reader.state['evidence_saved']==2


def test_preview_is_memory_only_and_expires_on_pause_or_session_change(video):
    app, client, camera, reader, _, tick = video
    accept(video, None)
    reader.epoch = 5
    cached = reader.preview(epoch=5)
    assert cached and cached[1].startswith(b'\xff\xd8')
    assert not list((app.DATA/'Images').iterdir())
    assert reader.preview(epoch=6) is None
    tick[0] = 1.61
    assert reader.preview(epoch=5) is None
    tick[0] = 0.5
    client.put('/api/video-ocr', json={'enabled': False})
    assert reader.preview(epoch=5) is None


def test_backing_off_nas_file_does_not_stop_live_ocr(video,monkeypatch):
    app,_,camera,reader,frame,tick=video
    calls=[]
    monkeypatch.setattr(app.saved_photo_watcher,'snapshot',lambda:{'pending_files':1,'ready_files':0,'retrying_files':1})
    monkeypatch.setattr(app.ocr_pool,'submit',lambda *a,**kw:calls.append(a) or Future())
    camera.publish(frame)
    reader.step()
    assert calls and reader.state['status']=='inferring'
