import hashlib,json
from pathlib import Path
from urllib.parse import unquote
import re
from test_demo import app_client,scan,register,wait_for_job
from test_archive_store import archive
from test_unbound_readout import photo


def test_classified_folders_keep_photos_source_times_and_immutable_versions(archive, monkeypatch):
    app,client,root=archive
    capture=scan(client)
    ident=capture['matches'][0]['id'];register(client,ident)
    binding=client.post('/api/bindings',json={'scan_id':capture['scan_id'],'instrument_id':ident,'operator':'甲'}).json()
    monkeypatch.setenv('FIELD_CAMERA_ID','TestCamera')
    monkeypatch.setattr(app,'predict_panel',lambda *args:{'status':'completed','device':'cpu','lines':[{'text':'0.0000 g'}]})
    job=app.read_saved_panel(app.SavedPhotoRequest(photo=photo(app)),trigger='voice_photo_directory')
    wait_for_job(client,job['job_id'])
    app.archive_store.step(batch_size=100)
    original={p:p.read_bytes() for p in root.glob('Receipts/**/*.json')}
    catalog=json.loads((root/'Browse/Catalog.json').read_text())['categories']
    for key in ('Bindings','InstrumentReadings','VoicePhotos','WorkbenchReadings','Photos'):
        assert catalog[key]
    entry=next(e for e in catalog['VoicePhotos'] if e['entity_id']==job['job_id'])
    doc=json.loads((root/entry['record']).read_text())
    assert doc['external_capture_id']==job['external_photo']['capture_id']
    assert doc['source_ref']==job['external_photo']['source_ref']
    assert doc['captured_at']==job['external_photo']['captured_at']
    assert doc['readings'][0]['value']=='0.0000'
    assert doc['readings'][0]['unit']=='g'
    assert doc['binding_id']==binding['binding_id']
    for artifact in doc['photos'].values():
        assert hashlib.sha256((root/artifact['path']).read_bytes()).hexdigest()==artifact['sha256']
    # Every local link and embedded image resolves within the standalone archive.
    assert (root/'00_归档导航.html').is_file()
    assert not app.archive_store.snapshot()['navigation_pending']
    chinese = list(root.glob('01_业务数据/04_语音拍照读数/*/甲/*/*/记录.json'))
    assert len(chinese) == 1
    readable = json.loads(chinese[0].read_text())
    assert readable['values'][0]['value'] == '0.0000'
    assert readable['values'][0]['unit'] == 'g'
    assert readable['record_scope'] == 'test_only'
    for path in [*root.glob('Browse/**/*.html'), *root.glob('01_业务数据/**/*.html'), root/'00_归档导航.html']:
        for ref in re.findall(r'(?:href|src)="([^"]+)"',path.read_text()):
            destination=(path.parent/unquote(ref)).resolve()
            destination.relative_to(root.resolve())
            assert destination.is_file(),(path,ref)
    assert client.get('/api/archive/files/'+entry['page']).status_code==200
    assert client.get('/api/archive/files/00_归档导航.html').status_code == 200
    assert client.get('/api/archive/files/' + chinese[0].relative_to(root).as_posix()).status_code == 200
    assert client.get('/api/archive/files/Browse/%2e%2e/.env').status_code==404
    client.post('/api/bindings/'+binding['binding_id']+'/end')
    app.archive_store.step(batch_size=100)
    assert all(p.read_bytes()==raw for p,raw in original.items())
    ended=next(e for e in json.loads((root/'Browse/Catalog.json').read_text())['categories']['Bindings'] if e['entity_id']==binding['binding_id'])
    changed=json.loads((root/ended['record']).read_text())
    assert changed['started_at']==binding['started_at'] and changed['ended_at']
    assert len(changed['receipt_versions'])==2


def test_readable_views_replace_changed_target_without_touching_evidence_or_user_files(archive):
    app, client, root = archive
    capture = scan(client)
    iid = capture['matches'][0]['id']; register(client, iid)
    binding = client.post('/api/bindings', json={'scan_id':capture['scan_id'],'instrument_id':iid,'operator':'甲'}).json()
    app.archive_store.step(batch_size=100)
    old = next(root.glob('01_业务数据/01_绑定记录/*/甲/*/*/记录.json'))
    note = old.parent/'个人备注.txt'; note.write_text('保留')
    originals = {p: p.read_bytes() for p in root.glob('Objects/**/*') if p.is_file()}
    receipts = {p: p.read_bytes() for p in root.glob('Receipts/**/*.json')}
    with app.db() as conn:
        updated = binding | {'operator':'乙'}
        conn.execute('UPDATE bindings SET document=? WHERE id=?', (json.dumps(updated),binding['binding_id']))
    app.archive_store.step(batch_size=100)
    assert not old.exists() and note.read_text() == '保留'
    assert len(list(root.glob('01_业务数据/01_绑定记录/*/乙/*/*/记录.json'))) == 1
    assert all(p.read_bytes() == raw for p, raw in (originals | receipts).items())
    app.archive_store.index_version += '-new-layout'
    assert app.archive_store.snapshot()['pending_receipts'] == 0
    assert app.archive_store.snapshot()['navigation_pending']
    for relative in ['01_业务数据/../../.env','01_业务数据/../Receipts/unknown.json']:
        assert client.get('/api/archive/files/' + relative).status_code == 404


def test_unassigned_multicode_readout_is_not_filed_as_first_instrument():
    from archive_browse import build_views
    job={'job_id':'job','capture_id':'capture','camera_id':'cam','submitted_at':'2026-09-14T05:26:00+00:00',
         'status':'completed','instrument':None,'instrument_candidates':[{'instrument':{'id':'a','name':'A'}},{'instrument':{'id':'b','name':'B'}}],
         'workbench':{'id':'bench','name':'实验台'},'readings':[{'text':'200 rpm','value':'200','instrument':None}]}
    views=build_views([{'entity':'jobs','entity_id':'job','seq':1,'document':json.dumps(job),'recorded_at':job['submitted_at'],'receipt_path':'Receipts/one.json'}])
    paths=list(views)
    assert 'Browse/InstrumentReadings/Unassigned/2026-09-14/job/Record.json' in paths
    assert 'Browse/WorkbenchReadings/bench/2026-09-14/job/Record.json' in paths
    assert not any('/InstrumentReadings/a/' in p or '/InstrumentReadings/b/' in p for p in paths)
    assert json.loads(views['Browse/Catalog.json'])['categories']['VoicePhotos']==[]
