"""Scene receipts must remain visible independently of instrument occupancy."""
import hashlib
import json
import re
import subprocess
from urllib.parse import unquote

from test_archive_store import archive  # noqa: F401
from test_demo import app_client, scan  # noqa: F401


def test_shared_scene_receipts_reach_nas_with_details_and_independent_end(archive):
    app, client, root = archive
    visits = []
    for camera, operator in [('CameraA', '甲'), ('CameraB', '乙')]:
        decoded = scan(client, 'Scene01', camera=camera)
        assert decoded['scene_matches'] and not decoded['matches']
        response = client.post('/api/scene/enter', json={
            'scan_id': decoded['scan_id'], 'scene_id': decoded['scene_matches'][0]['id'], 'operator': operator})
        assert response.status_code == 200
        visits.append(response.json())
    app.archive_store.step(100)
    files = list(root.glob('*/Bindings/*/Binding.json'))
    assert len(files) == 2
    originals = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in root.glob('.System/Receipts/**/*.json')}
    for path in files:
        doc = json.loads(path.read_text())
        assert doc['entity'] == 'scene_visits' and doc['status'] == 'active'
        saved = next(v for v in visits if v['visit_id'] == doc['binding']['visit_id'])
        assert doc['binding'] == saved
        html = path.with_name('Readme.html').read_text()
        assert '场景绑定' in html and '已绑定' in html
        for key in ['camera_id', 'operator', 'visit_id', 'scan_id']:
            assert saved[key] in html
        assert saved['scene']['id'] in html and saved['scene']['name'] in html
        assert '绑定时间' in html and '有效至' in html and '扫码凭证' in html
        assert doc['sources'][0]['scene_matches'][0]['id'] == saved['scene']['id']
        for image in doc['photos'].values():
            assert hashlib.sha256((root / image['path']).read_bytes()).hexdigest() == image['sha256']
        old = f"/api/archive/files/Records/SceneVisits/{saved['visit_id']}/Readme.html"
        assert client.get(old).text == html
    assert 'SceneBindings.html' in (root / 'Readme.html').read_text()
    index = (root / 'SceneBindings.html').read_text()
    assert all(v['camera_id'] in index and v['operator'] in index for v in visits)
    for path in [root / 'Readme.html', root / 'SceneBindings.html', *root.glob('*/Bindings/*/Readme.html'),
                 *root.glob('*/DailyReport/DailyReport.html')]:
        for link in re.findall(r'(?:href|src)="([^"]+)"', path.read_text()):
            destination = (path.parent / unquote(link)).resolve()
            destination.relative_to(root.resolve())
            assert destination.is_file(), (path, link)
    assert client.post('/api/camera/relations/end', json={'camera_id': 'CameraA'}).status_code == 200
    app.archive_store.step(100)
    docs = {json.loads(p.read_text())['camera_id']: json.loads(p.read_text()) for p in files}
    assert docs['CameraA']['status'] == 'ended'
    assert docs['CameraB']['status'] == 'active'
    assert docs['CameraA']['binding']['started_at'] == visits[0]['started_at']
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in originals.items())


def test_browser_scene_status_is_scoped_to_receipts_and_instrument_conflicts_are_separate():
    script = r"""
    const view = require('./static/binding-view.js');
    const now = Date.parse('2026-09-20T12:00:00+08:00');
    const scene = {visit_id:'visit-a', camera_id:'A', operator:'原实验员',
      scene:{id:'scene-id',name:'共享实验台'}, started_at:'2026-09-20T03:00:00Z',
      valid_until:'2026-09-20T16:00:00Z', image_url:'/api/images/scan-a'};
    const data = {scene_visits:[scene, {...scene,visit_id:'visit-b',camera_id:'B',operator:'乙'},
      {...scene,visit_id:'ended',ended_at:'2026-09-20T03:30:00Z'},
      {...scene,visit_id:'expired',valid_until:'2026-09-19T16:00:00Z'}],
      bindings:[{binding_id:'binding-b',camera_id:'B',operator:'乙',instrument:{id:'instrument',name:'仪器'}}],
      last_camera_scan:{camera_id:'A',received_at:'2026-09-20T03:00:00Z',matches:[{id:'instrument',name:'仪器'}]}};
    const scoped = view.current(data,'A',now);
    const html = view.scenesMarkup(scoped.scenes);
    const pending = view.pendingInstruments(data,'A',now);
    const hostile = view.scenesMarkup([{...scene,operator:'<script>bad</script>',image_url:'javascript:bad'}]);
    process.stdout.write(JSON.stringify({scoped,html,pending,hostile,
      unrelated:view.pendingInstruments(data,'C',now),
      registrationOnly:view.current({scenes:[scene.scene]},'A',now),
      expired:view.current(data,'A',Date.parse('2026-09-21T00:00:00+08:00')),
      afterHandoff:view.pendingInstruments({...data,bindings:[{...data.bindings[0],camera_id:'A'}]},'A',now)}));
    """
    result = json.loads(subprocess.check_output(['node', '-e', script], text=True))
    assert [v['visit_id'] for v in result['scoped']['scenes']] == ['visit-a']
    assert result['scoped']['instruments'] == []
    assert '场景已绑定' in result['html'] and '原实验员' in result['html']
    assert '/api/images/scan-a' in result['html'] and '绑定时间' in result['html']
    assert '仪器未绑定' in result['pending'] and '乙 · B' in result['pending'] and '交接' in result['pending']
    assert result['afterHandoff'] == result['unrelated'] == ''
    assert result['registrationOnly']['scenes'] == result['expired']['scenes'] == []
    assert '<script>' not in result['hostile'] and 'javascript:' not in result['hostile']
