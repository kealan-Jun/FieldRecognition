"""Freeze photo-time relationships without choosing the first of several instruments."""
import json
from datetime import datetime


def at_capture(core, conn, capture, *, linked=None, automatic=False):
    moment = datetime.fromisoformat((capture.get('external_photo') or {}).get('captured_at') or capture['received_at'])
    snapshots = [linked] if linked else []
    if automatic and not linked:
        snapshots = [json.loads(row['document']) for row in conn.execute(
            'SELECT document FROM bindings WHERE camera=? AND ended IS NULL ORDER BY rowid', (capture['camera_id'],))]
        snapshots = [b for b in snapshots if datetime.fromisoformat(b['started_at']) <= moment
                     and core['current_readout_binding'](b)]
    hits = capture.get('matches', [])
    visible = {hit['id'] for hit in hits}
    selected = [b for b in snapshots if not visible or b['instrument']['id'] in visible]
    candidates = [{'instrument': b['instrument'], 'binding_id': b['binding_id'],
                   'basis': 'same_image_qr' if visible else 'session_binding'} for b in selected]
    if automatic:
        for hit in hits:
            if not any(c['instrument']['id'] == hit['id'] for c in candidates):
                candidates.append({'instrument': {k: hit[k] for k in ('id', 'name', 'scene', 'model') if k in hit},
                                   'binding_id': None, 'basis': 'same_image_qr'})
    visits = [json.loads(row['document']) for row in conn.execute(
        'SELECT document FROM scene_visits WHERE camera=? AND ended IS NULL ORDER BY rowid', (capture['camera_id'],))]
    visits = [v for v in visits if datetime.fromisoformat(v['started_at']) <= moment]
    unique = selected[0] if len(selected) == 1 and len(candidates) == 1 else None
    names = {c['instrument'].get('scene') for c in candidates if c['instrument'].get('scene')}
    if not names:
        names = {v['scene']['name'] for v in visits}
    workbenches = []
    for name in sorted(names):
        scene = conn.execute('SELECT * FROM scenes WHERE name=?', (name,)).fetchone()
        visit = next((v for v in visits if v['scene']['name'] == name), None)
        workbenches.append({'id': scene['id'] if scene else None, 'name': name,
            'basis': 'decoded_scene_qr' if visit else 'instrument_registration',
            'scene_visit_id': visit['visit_id'] if visit else None})
    return {'binding_snapshots': selected, 'binding_ids': [b['binding_id'] for b in selected],
            'instrument_candidates': candidates, 'scene_snapshots': visits,
            'qr_matches': hits, 'qr_scene_matches': capture.get('scene_matches', []),
            'workbenches': workbenches, 'workbench': workbenches[0] if len(workbenches) == 1 else None,
            'association_status': 'multiple_candidates' if len(candidates) > 1 else
                'single_candidate' if candidates else 'unbound',
            'resolved_binding': unique}
