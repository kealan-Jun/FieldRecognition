import hashlib
import json
from pathlib import Path

import pytest

from archive_paths import ArchivePaths, checked, load, resolve_file
from archive_store import canonical
from test_archive_store import archive  # noqa: F401
from test_demo import app_client, scan, register  # noqa: F401
from test_photo_measurements import setup, enqueue, complete, get_draft  # noqa: F401


def test_burst_conflict_correction_confirmation_and_retry_share_one_folder(setup, archive):
    app, client, binding, _ = setup
    root = archive[2]
    response, request = enqueue(setup, 3)
    mid = response['measurement_id']
    complete(setup, 0, '51')
    app.archive_store.step(100)
    file = next(root.glob('*/VoicePhotoReadings/*/Result.json'))
    complete(setup, 1, '52'); complete(setup, 2, '51')
    app.archive_store.step(100)
    result = json.loads(file.read_text())
    assert len(list(root.glob('*/VoicePhotoReadings/*/Result.json'))) == 1
    assert len(result['sources']) == 3
    assert result['instrument_measurements'][0]['record']['values'][0]['value'] is None
    draft = get_draft(client, mid)
    body = {'actor':'Reader', 'revision':draft['revision'], 'reason':'核对三张照片',
        'fields':[{'field_id':draft['fields'][0]['field_id'], 'instrument_id':binding['instrument']['id'],
                   'name':'温度', 'value':51, 'unit':'°C'}]}
    revised = client.post(f'/api/photo-measurements/{mid}/revisions', json=body).json()
    confirmation = {'actor':'Reader', 'revision':revised['revision']}
    assert client.post(f'/api/photo-measurements/{mid}/confirm', json=confirmation).status_code == 200
    app.archive_store.step(100)
    result = json.loads(file.read_text())
    assert result['record_scope'] == 'production_confirmed'
    assert result['instrument_measurements'][0]['record']['values'][0]['value'] == 51
    assert len(result['decision']['corrections']) == 1
    assert result['instrument_measurements'][0]['evidence']['human_verified']
    assert result['observations'][1]['readings'][0]['value'] == '52'
    before = {p.relative_to(root).as_posix():p.read_bytes() for p in root.glob('**/*') if p.is_file() and p.suffix in {'.png','.jpg'}}
    count = app.archive_store.snapshot()['archived_receipts']
    assert client.post('/api/ocr/photo-burst', json=request).json()['job_ids'] == response['job_ids']
    assert client.post(f'/api/photo-measurements/{mid}/confirm', json=confirmation).status_code == 200
    app.archive_store.step(100)
    assert app.archive_store.snapshot()['archived_receipts'] == count
    assert all((root/p).read_bytes() == raw for p,raw in before.items())
    assert len({hashlib.sha256(raw).hexdigest() for raw in before.values()}) == len(before)
    assert len(list(root.glob('*/VoicePhotoReadings/*/Result.json'))) == 1
    assert app.archive_store.integrity.run_once()['issue_count'] == 0


def test_interrupted_move_keeps_old_bytes_readable_then_resumes(tmp_path, monkeypatch):
    root = tmp_path/'FieldRecognitionArchive'; root.mkdir()
    old = root/'Objects/Photo.png'; old.parent.mkdir(); old.write_bytes(b'original')
    paths = ArchivePaths(root)
    original = Path.rename
    monkeypatch.setattr(Path, 'rename', lambda *args: (_ for _ in ()).throw(OSError('NAS disconnected')))
    target = '2026-09-14_Cam/Bindings/Event/Photos/Original.png'
    with pytest.raises(OSError):
        paths.move('Objects/Photo.png', target)
    assert load(root)['aliases']['Objects/Photo.png'] == target
    assert resolve_file(root, 'Objects/Photo.png').read_bytes() == b'original'
    monkeypatch.setattr(Path, 'rename', original)
    ArchivePaths(root).move('Objects/Photo.png', target)
    assert resolve_file(root, 'Objects/Photo.png').read_bytes() == b'original'
    assert not old.exists()
    with pytest.raises(ValueError):
        ArchivePaths(root).move(target, '../Outside.png')


def test_migration_conflicting_destination_never_overwrites_or_repoints(tmp_path):
    old, target = tmp_path/'Original.png', tmp_path/'Other.png'
    old.write_bytes(b'original'); target.write_bytes(b'other')
    with pytest.raises(ValueError):
        ArchivePaths(tmp_path).move('Original.png', 'Other.png')
    assert old.read_bytes() == b'original' and target.read_bytes() == b'other'
    assert not load(tmp_path)['aliases']


def test_group_export_never_refills_a_conflicting_unit_from_registration():
    from archive_events import standard_records
    from photo_measurements import candidates
    from test_measurement_records import document
    doc = document()
    doc['readings'][0]['unit'] = 'rpm'
    group = {'fields':candidates([doc]), 'status':'draft'}
    entry = standard_records([{'seq':1,'entity_id':'job','doc':doc}], group)[0]
    assert entry['record']['values'][0] == {'name':'温度','value':None,'unit':None,'range':[None,None]}


def test_migration_cli_loads_only_literal_archive_settings(tmp_path, monkeypatch):
    from scripts.migrate_archive import load_settings
    monkeypatch.delenv('FIELD_ARCHIVE_ROOT', raising=False)
    monkeypatch.delenv('FIELD_ARCHIVE_ENABLED', raising=False)
    monkeypatch.delenv('NOT_AN_ARCHIVE_KEY', raising=False)
    path = tmp_path/'Settings.env'
    path.write_text('FIELD_ARCHIVE_ENABLED=1\nFIELD_ARCHIVE_ROOT="/path with spaces/$(touch Nope)"\nNOT_AN_ARCHIVE_KEY=private\n')
    load_settings(path)
    import os
    assert os.environ['FIELD_ARCHIVE_ROOT']=='/path with spaces/$(touch Nope)'
    assert os.environ['FIELD_ARCHIVE_ENABLED']=='1' and 'NOT_AN_ARCHIVE_KEY' not in os.environ


def test_legacy_receipts_objects_and_urls_survive_migration_without_duplicates(archive):
    app, client, root = archive
    capture = scan(client); iid = capture['matches'][0]['id']; register(client, iid)
    binding = client.post('/api/bindings', json={'scan_id':capture['scan_id'],'instrument_id':iid,'operator':'甲'}).json()
    app.archive_store.step(100)
    paths = ArchivePaths(root)
    # Construct a legacy archive from the same fixture's real decoded QR evidence.
    for receipt in list(root.glob('.System/Receipts/**/*.json')):
        doc = json.loads(receipt.read_text())
        for artifact in doc['artifacts'].values():
            if not isinstance(artifact, dict):
                continue
            source = resolve_file(root, artifact['path']); digest = artifact['sha256']
            logical = f'Objects/{digest[:2]}/{digest}{source.suffix}'
            target = root/logical; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            artifact['path'] = logical
        raw = canonical(doc)
        legacy = f"Receipts/Cam/2026-09-14/Instance/{doc['sequence']:012d}-{hashlib.sha256(raw).hexdigest()}.json"
        file = root/legacy; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(raw)
        with app.db() as conn:
            conn.execute('UPDATE archive_outbox SET receipt_path=? WHERE seq=?', (legacy, doc['sequence']))
        receipt.unlink()
    for item in paths.data['assets'].values():
        checked(root, item['path']).unlink(missing_ok=True)
    for view in paths.data['views']:
        checked(root, view).unlink(missing_ok=True)
    (root/'.System/MigrationMap.json').unlink()
    with app.db() as conn:
        rows = conn.execute('SELECT * FROM archive_outbox ORDER BY seq').fetchall()
    from archive_browse import build_views
    for name, raw in build_views(rows).items():
        file = root/name; file.parent.mkdir(parents=True, exist_ok=True); file.write_bytes(raw)
    old = f"Records/Bindings/{binding['binding_id']}/Record.json"
    note = (root/old).with_name('PersonalNote.txt'); note.write_text('保留手工备注')
    before = {p.relative_to(root).as_posix():p.read_bytes() for p in root.glob('Receipts/**/*.json')}
    legacy_links = {'01_业务数据/绑定/Record.json':old}
    (root/'LegacyLinks.json').write_text(json.dumps(legacy_links))
    app.archive_store.receipt_cache = {}
    app.archive_store.write_index()
    assert note.read_text() == '保留手工备注'
    assert not (root/old).exists()
    assert client.get('/api/archive/files/'+old).json()['binding']['operator'] == '甲'
    assert client.get('/api/archive/files/01_业务数据/绑定/Record.json').json()['binding']['binding_id'] == binding['binding_id']
    for relative, raw in before.items():
        assert resolve_file(root, relative).read_bytes() == raw
    images = [p for p in root.rglob('*') if p.is_file() and p.suffix == '.png']
    assert len(images) == len({hashlib.sha256(p.read_bytes()).hexdigest() for p in images})
    assert not (root/'Objects').exists() and not (root/'Receipts').exists()
    assert not (root/'Browse').exists() and not (root/'LegacyLinks.json').exists()
    assert app.archive_store.integrity.run_once()['issue_count'] == 0
    app.archive_store.write_index()
    assert all(resolve_file(root, p).read_bytes() == raw for p,raw in before.items())
