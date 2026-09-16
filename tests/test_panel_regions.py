import copy
import hashlib
import json

import numpy as np
import pytest

import panel_readout
import panel_regions
from test_demo import app_client, wait_for_job  # noqa: F401
from test_multi_readout import two_bindings
from test_unbound_readout import photo
from test_panel_readout import Clock, cloud_result
from test_archive_store import archive  # noqa: F401
from archive_paths import resolve_file

A = 'e9434a0a-3319-414a-b988-4cc6884edce4'
B = 'eae17924-9fa7-4445-ac45-3987f5687be9'
BOXES = [dict(class_id=0, instrument_id=A, confidence=.8, xyxy=[20,30,80,50]),
         dict(class_id=1, instrument_id=B, confidence=.9, xyxy=[210,100,300,140])]


def test_off_display_is_a_state_not_zero_or_a_fallback_miss(app_client,monkeypatch):
    app,_=app_client
    boxes=[dict(class_id=0,instrument_id=A,confidence=.8,xyxy=[20,30,80,50])]
    configure(app,monkeypatch,boxes)
    monkeypatch.setattr(panel_regions,'refine_digits',lambda predict,image,initial,*args,**kwargs:
        dict(initial, lines=[], panel_ocr=copy.deepcopy(initial)))
    monkeypatch.setattr(app,'predict_panel',lambda *args:{'status':'completed','lines':[{'text':'OFF','confidence':.9}]})
    result=panel_regions.predict(vars(app),np.zeros((100,150,3),np.uint8),allowed_instrument_ids=[A])
    assert result['lines']==[]
    assert result['panel_regions'][0]['display_state']['text']=='OFF'
    assert panel_regions.needs_fallback(result) is None


def test_small_detector_border_overlap_keeps_temperature_and_speed_roles():
    from panel_layout import roles_for_boxes
    boxes=[dict(class_id=0,instrument_id=A,xyxy=box) for box in ([354,315,398,347],[395,313,438,342])]
    assert list(roles_for_boxes(boxes).values())==['温度','转速']
    boxes[1]['xyxy']=[360,313,401,342]
    assert roles_for_boxes(boxes)=={}  # Overlapping instances cannot be guessed apart.


def configure(app, monkeypatch, boxes=BOXES):
    # These tests isolate association/crop routing; digit refinement has its own tests.
    monkeypatch.setattr(panel_regions,'refine_digits',lambda predict,image,initial,x=0,y=0,**kw:initial)
    monkeypatch.setenv('FIELD_PANEL_DETECTOR_ENABLED', '1')
    monkeypatch.setattr(app.panel_detector, 'warmup', lambda: True)
    monkeypatch.setattr(app.panel_detector, 'predict', lambda image: {'status':'completed', 'boxes':copy.deepcopy(boxes), 'weights_sha256':'a'*64})
    monkeypatch.setattr(app, 'predict_panel', lambda image, x=0, y=0: {
        'status':'completed', 'device':'cpu', 'model':'test', 'actual_model_invocation':True,
        'lines':[{'text':'12.3 g' if x<100 else '200 g', 'polygon':[[x,y],[x+10,y],[x+10,y+8],[x,y+8]]}]})


def test_two_panels_keep_separate_bindings_polygons_and_archived_crops(archive, monkeypatch):
    app, client, root = archive
    monkeypatch.setenv('FIELD_CAMERA_ID', 'TestCamera')
    bindings = two_bindings(app, client)
    configure(app, monkeypatch)
    job = app.read_saved_panel(app.SavedPhotoRequest(photo=photo(app)), trigger='voice_photo_directory')
    done = wait_for_job(client, job['job_id'])
    assert done['status'] == 'completed' and done['association_status']=='localized_panels'
    assert [r['instrument']['id'] for r in done['readings']] == [A,B]
    assert [r['binding_id'] for r in done['readings']] == [b['binding_id'] for b in bindings]
    assert done['readings'][0]['polygon'][0] == [18,28]
    assert done['readings'][1]['polygon'][0] == [207,97]
    assert all(r['association_basis']=='panel_detector_session_binding' for r in done['readings'])
    assert done['instrument'] is None and len(done['binding_ids'])==2
    for r in done['readings']:
        raw=client.get(r['panel_image_url']).content
        assert hashlib.sha256(raw).hexdigest()==r['panel_image_sha256']
    app.archive_store.step()
    documents=[json.loads(p.read_text()) for p in root.glob('.System/Receipts/**/*.json')]
    receipt=next(r for r in documents if r['entity_id']==job['job_id'] and r['document'].get('readings'))
    crops=[a for k,a in receipt['artifacts'].items() if k.startswith('panel_')]
    assert len(crops)==2
    assert all(hashlib.sha256(resolve_file(root,c['path']).read_bytes()).hexdigest()==c['sha256'] for c in crops)
    for iid, text in ((A,'12.3 g'),(B,'200 g')):
        records=list(root.glob('*/VoicePhotoReadings/*/Evidence.json'))
        assert len(records)==1
        r=json.loads(records[0].read_text())
        assert [v['text'] for v in r['observations'][0]['readings'] if v['instrument']['id']==iid]==[text]
        entry=next(v for v in r['measurement_files'] if v['instrument_id']==iid)
        standard=json.loads((root/entry['result']).read_text())
        assert set(standard)=={'wearer_id','device_model','device_no','qr_hash','photo_time','values'}
        assert standard['qr_hash']==next(b['qr_hash'] for b in bindings if b['instrument']['id']==iid)
    exported=client.get('/api/jobs/'+job['job_id']+'/measurements').json()
    assert exported['format_available'] and len(exported['records'])==2
    assert '面板分别定位' in client.get('/api/readouts').json()['items'][0]['target']
    related=client.get('/api/readouts?related_only=true').json()['items'][0]
    assert related['job_id']==done['job_id']
    assert [r['instrument']['id'] for r in related['readings']]==[A,B]


def test_unlocalized_ocr_does_not_borrow_the_only_bound_instrument(app_client, monkeypatch):
    app,client=app_client
    monkeypatch.setenv('FIELD_CAMERA_ID','TestCamera')
    bindings=two_bindings(app,client)
    client.post('/api/bindings/'+bindings[1]['binding_id']+'/end')
    configure(app,monkeypatch,[])
    job=app.read_saved_panel(app.SavedPhotoRequest(photo=photo(app)),trigger='voice_photo_directory')
    done=wait_for_job(client,job['job_id'])
    assert done['readings']==[] and done['recognition_skipped']
    assert done['skip_reason']=='no_visible_bound_panel'
    assert not done['actual_model_invocation']
    assert done['instrument'] is done['binding_id'] is None


def test_unbound_photo_skips_detector_ocr_and_cloud(app_client,monkeypatch):
    app,client=app_client
    monkeypatch.setenv('FIELD_CAMERA_ID','TestCamera')
    configure(app,monkeypatch)
    import aliyun_vision as vision
    monkeypatch.setenv('FIELD_ALIYUN_FALLBACK_ENABLED','1')
    monkeypatch.setenv('DASHSCOPE_API_KEY','test-only-key')
    monkeypatch.setattr(app.panel_detector,'predict',lambda *a:pytest.fail('No bound instrument'))
    monkeypatch.setattr(app,'predict_panel',lambda *a:pytest.fail('No bound instrument'))
    monkeypatch.setattr(vision,'read_panel',lambda *a:pytest.fail('No bound instrument'))
    job=app.read_saved_panel(app.SavedPhotoRequest(photo=photo(app)),trigger='voice_photo_directory')
    done=wait_for_job(client,job['job_id'])
    assert done['readings']==[] and done['recognition_skipped']
    assert done['skip_reason']=='no_active_instrument_binding'
    assert not client.get('/api/state').json()['bindings']


def test_missing_a_uses_its_crop_for_fallback_even_when_b_has_digits(app_client,monkeypatch):
    import aliyun_vision as vision
    app,client=app_client
    monkeypatch.setenv('FIELD_CAMERA_ID','TestCamera')
    bindings=two_bindings(app,client)
    configure(app,monkeypatch)
    monkeypatch.setattr(app.readout_pool,'submit',lambda *a:None)
    job=app.read_saved_panel(app.SavedPhotoRequest(photo=photo(app)),trigger='voice_photo_directory')
    local=app.predict_readout(np.zeros((300,400,3),np.uint8),binding_snapshots=bindings)
    local['panel_regions'][0]['local_ocr']['lines']=[]
    local['lines']=local['panel_regions'][1]['local_ocr']['lines']
    job['precomputed_local']=local
    monkeypatch.setenv('FIELD_ALIYUN_FALLBACK_ENABLED','1')
    monkeypatch.setenv('DASHSCOPE_API_KEY','test-only-key')
    monkeypatch.setenv('FIELD_ALIYUN_NO_DIGITS_SECONDS','5')
    clock=Clock();calls=[]
    def cloud(crop):
        assert clock.value>=5
        calls.append(crop.shape[:2]);return cloud_result()
    monkeypatch.setattr(vision,'read_panel',cloud)
    panel_readout.run(vars(app),job,clock=clock,pause=clock.pause)
    done=app.get_job(job['job_id'])
    assert calls==[(24,64)]
    assert [r['value'] for r in done['readings']]==['12.34','200']
    assert [r['instrument']['id'] for r in done['readings']]==[A,B]


def test_no_panel_in_video_skips_ocr_and_does_not_use_background_numbers(app_client,monkeypatch):
    app,_=app_client
    configure(app,monkeypatch,[])
    monkeypatch.setattr(app,'predict_panel',lambda *a:pytest.fail('Background video must not invoke OCR'))
    local=app.predict_readout(np.zeros((300,400,3),np.uint8),video=True)
    assert local['lines']==local['panel_regions']==[]
    assert not local['actual_model_invocation']


def test_crop_offsets_are_in_full_photo_coordinates(app_client,monkeypatch):
    app,_=app_client
    configure(app,monkeypatch,BOXES[:1])
    local=app.predict_readout(np.zeros((150,150,3),np.uint8),100,200,binding_snapshots=[{'instrument':{'id':A}}])
    region=local['panel_regions'][0]
    assert region['bbox']==[120,230,180,250]
    assert region['crop']==[118,228,64,24]
    assert local['lines'][0]['polygon'][0]==[118,228]


def test_only_b_bound_skips_a_panels_before_ocr(app_client,monkeypatch):
    app,_=app_client
    configure(app,monkeypatch)
    calls=[]
    monkeypatch.setattr(app,'predict_panel',lambda image,x=0,y=0:calls.append((x,y)) or {'lines':[]})
    local=app.predict_readout(np.zeros((300,400,3),np.uint8),binding_snapshots=[{'instrument':{'id':B}}])
    assert calls==[(207,97)]
    assert [r['instrument_id'] for r in local['panel_regions']]==[B]
    assert [r['instrument_id'] for r in local['panel_detection']['skipped_unbound_panels']]==[A]


def test_localization_timeout_never_sends_whole_image_to_cloud(app_client,monkeypatch):
    from concurrent.futures import Future
    import aliyun_vision as vision
    app,client=app_client
    monkeypatch.setenv('FIELD_CAMERA_ID','TestCamera')
    two_bindings(app,client)
    configure(app,monkeypatch)
    monkeypatch.setattr(app.readout_pool,'submit',lambda *a:None)
    job=app.read_saved_panel(app.SavedPhotoRequest(photo=photo(app)),trigger='voice_photo_directory')
    future=Future()
    monkeypatch.setattr(app.ocr_pool,'submit',lambda *a:future)
    monkeypatch.setenv('FIELD_ALIYUN_FALLBACK_ENABLED','1')
    monkeypatch.setenv('DASHSCOPE_API_KEY','test-only-key')
    monkeypatch.setattr(vision,'read_panel',lambda *a:pytest.fail('Unlocalized cloud call'))
    clock=Clock()
    panel_readout.run(vars(app),job,clock=clock,pause=clock.pause)
    done=app.get_job(job['job_id'])
    assert done['error']=='bound_panel_localization_unavailable'
    assert done['readings']==[] and done['instrument'] is None


def test_stirrer_values_use_aligned_left_right_windows_before_partial_frame_selection(app_client,monkeypatch):
    app,_=app_client
    boxes=[BOXES[0],dict(BOXES[0],xyxy=[90,30,150,50]),BOXES[1]]
    configure(app,monkeypatch,boxes)
    local=app.predict_readout(np.zeros((300,400,3),np.uint8),binding_snapshots=[{'instrument':{'id':A}},{'instrument':{'id':B}}])
    assert [r['measurement_name'] for r in local['panel_regions']]==['温度','转速','质量']
    # Only one changed window can subsequently be retained, with its role intact.
    assert local['panel_regions'][1]['measurement_name']=='转速'
    configure(app,monkeypatch,boxes[1:])
    local=app.predict_readout(np.zeros((300,400,3),np.uint8),binding_snapshots=[{'instrument':{'id':A}}])
    assert local['panel_regions'][0]['measurement_name'] is None
