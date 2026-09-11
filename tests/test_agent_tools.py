import base64
from pathlib import Path

from test_demo import app_client, register  # noqa: F401


def invoke(client, name, args=None):
    return client.post('/api/tools/' + name, json=args or {})


def test_tool_flow_reuses_browser_records_and_async_ocr(app_client, monkeypatch):
    app, client = app_client
    definitions = client.get('/api/tools').json()['tools']
    assert len(definitions) == 8
    assert all(t['input_schema']['additionalProperties'] is False for t in definitions)
    image = Path(__file__).parents[1] / 'static/labels/InstrumentA.png'
    scan = invoke(client, 'scan_photo', {'camera_id': 'ToolCamera',
        'image_base64': base64.b64encode(image.read_bytes()).decode()}).json()['result']
    assert 'path' not in scan
    aid = scan['matches'][0]['id']
    register(client, aid, camera="ToolCamera")
    bound = invoke(client, 'bind_instrument', {'scan_id': scan['scan_id'],
        'instrument_id': aid, 'operator': 'ToolTester'}).json()['result']
    assert client.get('/api/state').json()['bindings'][0]['binding_id'] == bound['binding_id']
    dispatched = []
    monkeypatch.setattr(app.ocr_pool, 'submit', lambda fn, doc: dispatched.append((fn, doc)))
    job = invoke(client, 'read_panel', {'binding_id': bound['binding_id'],
        'capture_id': scan['capture_id']}).json()['result']
    assert dispatched[0][0] is app.run_ocr
    assert job['status'] == 'queued'
    assert invoke(client, 'get_panel_result', {'job_id': job['job_id']}).json()['result']['status'] == 'queued'
    invoke(client, 'end_instrument_binding', {'binding_id': bound['binding_id']})
    assert invoke(client, 'read_panel', {'binding_id': bound['binding_id'],
        'capture_id': scan['capture_id']}).status_code == 409


def test_tool_validation_and_live_capture_delegate(app_client, monkeypatch):
    app, client = app_client
    assert invoke(client, 'unknown').status_code == 404
    assert invoke(client, 'capture_and_scan', {'camera_id': 'WrongCamera'}).status_code == 422
    assert invoke(client, 'read_panel', {'binding_id': 'invalid'}).status_code == 422
    assert invoke(client, 'scan_photo', {'camera_id': 'x', 'image_base64': '!invalid!'}).status_code == 422
    monkeypatch.setattr(app, 'camera_capture', lambda: {'capture_id': 'verified-delegate', 'path': '/internal'})
    assert invoke(client, 'capture_and_scan').json()['result'] == {'capture_id': 'verified-delegate'}
