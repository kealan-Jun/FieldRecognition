"""Freeze photo-time relationships without choosing the first of several instruments."""
import json
from datetime import datetime


def binding_contains_photo(binding, capture):
    try:
        moment = datetime.fromisoformat(capture['external_photo']['captured_at'])
        return moment.tzinfo is not None and datetime.fromisoformat(binding['started_at']) <= moment and (
            not binding.get('ended_at') or moment < datetime.fromisoformat(binding['ended_at']))
    except (TypeError, KeyError, ValueError):
        return False


def at_capture(core, conn, capture, *, linked=None, automatic=False):
    captured_at = (capture.get('external_photo') or {}).get('captured_at')
    moment = datetime.fromisoformat(captured_at or capture['received_at'])
    def contained(document):
        return (datetime.fromisoformat(document['started_at']) <= moment and
                (not document.get('ended_at') or moment < datetime.fromisoformat(document['ended_at'])))
    snapshots = [linked] if linked else []
    if automatic and not linked:
        snapshots = [json.loads(row['document']) for row in conn.execute(
            'SELECT document FROM bindings WHERE camera=? ORDER BY rowid', (capture['camera_id'],))]
        snapshots = [b for b in snapshots if contained(b) and (bool(captured_at) or core['current_readout_binding'](b))]
    # Legacy overlapping ownership has no uniquely provable operator. Preserve the
    # snapshots as a conflict, never pick the first or the newest relationship.
    conflicts = []
    for binding in snapshots:
        others = [json.loads(r[0]) for r in conn.execute(
            "SELECT document FROM bindings WHERE id<>? AND json_extract(document,'$.instrument.id')=?",
            (binding['binding_id'], binding['instrument']['id']))]
        if any(contained(other) for other in others):
            conflicts.append(binding['instrument']['id'])
    snapshots = [b for b in snapshots if b['instrument']['id'] not in conflicts]
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
        'SELECT document FROM scene_visits WHERE camera=? ORDER BY rowid', (capture['camera_id'],))]
    visits = [v for v in visits if contained(v)]
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
    return {'all_binding_snapshots': snapshots, 'binding_snapshots': selected, 'binding_ids': [b['binding_id'] for b in selected],
            'instrument_candidates': candidates, 'scene_snapshots': visits,
            'qr_matches': hits, 'qr_scene_matches': capture.get('scene_matches', []),
            'workbenches': workbenches, 'workbench': workbenches[0] if len(workbenches) == 1 else None,
            'association_status': 'multiple_candidates' if len(candidates) > 1 else
                'single_candidate' if candidates else 'unbound',
            'ownership_conflicts': conflicts,
            'binding_time_basis': 'source_capture_time' if captured_at else 'received_time_only',
            'resolved_binding': unique}
