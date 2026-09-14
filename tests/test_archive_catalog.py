import json
import subprocess
from pathlib import Path

from test_archive_store import archive  # noqa: F401
from test_demo import app_client, scan  # noqa: F401


def select(items, filters):
    result = subprocess.run(['node', '-e', "const {selectArchive}=require('./static/archive.js');let s='';process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>{let a=JSON.parse(s);process.stdout.write(JSON.stringify(selectArchive(a.items,a.filters)));});"],
        input=json.dumps({'items': items, 'filters': filters}), text=True, capture_output=True,
        check=True, cwd=Path(__file__).parents[1])
    return json.loads(result.stdout)


def test_full_index_filters_old_versions_without_rewriting_evidence(archive):
    app, client, root = archive
    capture = scan(client)
    with app.db() as conn:
        for i in range(205):
            doc = capture | {'operator': '旧实验员' if i == 0 else '新实验员',
                             'received_at': '2026-09-13T16:00:00+00:00'}
            # Keep versions different while retaining one actual decoded identity.
            doc['revision_note'] = i
            conn.execute('UPDATE scans SET document=? WHERE id=?', (json.dumps(doc), capture['scan_id']))
    app.archive_store.step(batch_size=300)
    original_receipts = {p: p.read_bytes() for p in root.glob('Receipts/**/*.json')}
    index = json.loads((root / 'Index.json').read_text())
    assert index['schema'] == 'field-recognition-index/2'
    assert index['listed_receipts'] == index['archived_receipts'] == 206
    older = select(index['items'], {'versions': 'all', 'operator': '旧实验员',
        'instrument': capture['matches'][0]['id'], 'from': '2026-09-14', 'to': '2026-09-14'})
    assert len(older) == 1 and older[0]['sequence'] == 2
    assert not select(index['items'], {'operator': '旧实验员'})  # Latest first, then filter.
    assert len(select(index['items'], {'operator': '新实验员'})) == 1
    assert not select(index['items'], {'from': '2026-09-15'})
    (root / 'Readme.html').unlink()
    app.archive_store.step()
    assert (root / 'Readme.html').exists()
    assert all(p.read_bytes() == raw for p, raw in original_receipts.items())


def test_index_keeps_unknown_operator_and_instrument_and_escapes_html(archive):
    app, client, root = archive
    with app.db() as conn:
        for ident, operator in [('unknown', None), ('escaped', '</script><img src=x onerror=alert(1)>')]:
            doc = {'job_id': ident, 'camera_id': 'Camera', 'operator': operator, 'instrument': None,
                   'submitted_at': '2026-09-14T01:00:00+00:00', 'status': 'completed', 'lines': []}
            conn.execute('INSERT INTO jobs VALUES(?,?,?)', (ident, 'completed', json.dumps(doc)))
    app.archive_store.step()
    items = json.loads((root / 'Index.json').read_text())['items']
    assert [i['entity_id'] for i in select(items, {'operator': '__missing__', 'instrument': '__missing__'})] == ['unknown']
    assert select(items, {'camera': 'Other'}) == []
    html = (root / 'Readme.html').read_text()
    assert '</script><img' not in html and '\\u003c/script>' in html
    assert '/*ARCHIVE_DATA*/' not in html and '/*ARCHIVE_SCRIPT*/' not in html


def test_read_only_archive_routes_restrict_scope(archive, tmp_path):
    app, client, root = archive
    scan(client); app.archive_store.step()
    response = client.get('/api/archive/files/Readme.html')
    assert response.status_code == 200 and '查询历史留存' in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert client.get('/api/archive/files/Index.json').json()['listed_receipts'] > 0
    outside = tmp_path / 'private.json'; outside.write_text('private')
    (root / 'Objects' / 'escape.json').symlink_to(outside)
    for relative in ['Objects/escape.json', 'Objects/%2e%2e/%2e%2e/private.json', '.env', 'unknown.json']:
        result = client.get('/api/archive/files/' + relative)
        assert result.status_code == 404 and 'private' not in result.text
