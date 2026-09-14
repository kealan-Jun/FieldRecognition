"""Separate numeric regions with explicit, reviewable identity ambiguity."""
import re
from aliyun_vision import READOUT, NUMBER


def build_readings(document):
    candidates = document.get('instrument_candidates') or ([{'instrument': document['instrument'],
        'binding_id': document.get('binding_id'), 'basis': document.get('instrument_association')}]
        if document.get('instrument') else [])
    result = []
    for line in document.get('lines', []):
        text = line.get('text', '')
        if document.get('device') != 'cloud' and not READOUT.fullmatch(text):
            continue
        match = re.match(r'\s*(' + NUMBER + ')', text)
        if not match and not line.get('value'):
            continue
        unique = candidates[0] if len(candidates) == 1 else None
        value = line.get('value') or match[1]
        issue = 'decimal_uncertain' if re.fullmatch(r'[+-]?0\d+', value) else None
        result.append({'reading_id': f"{document['job_id']}:{len(result) + 1}",
            'text': text, 'value': value, 'unit': line.get('unit') or (text[match.end():].strip() if match else None) or None,
            'polygon': line.get('polygon'), 'confidence': line.get('confidence'),
            'instrument': unique['instrument'] if unique else None,
            'binding_id': unique.get('binding_id') if unique else None,
            'association_basis': unique.get('basis') if unique else 'ambiguous' if candidates else 'unbound',
            'instrument_candidates': candidates, 'quality_issue': issue,
            'human_verified': False, 'status': 'needs_review',
            'workbench': document.get('workbench'), 'workbenches': document.get('workbenches', []),
            'activity_confirmed': False,
            'image_url': document.get('image_url'), 'capture_id': document['capture_id']})
    return result
