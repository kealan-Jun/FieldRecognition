"""Separate numeric regions with explicit, reviewable identity ambiguity."""
import re
from aliyun_vision import READOUT, NUMBER


def build_readings(document):
    candidates = document.get('instrument_candidates') or ([{'instrument': document['instrument'],
        'binding_id': document.get('binding_id'), 'basis': document.get('instrument_association')}]
        if document.get('instrument') else [])
    result = []
    regions = {r['panel_id']:r for r in document.get('panel_regions', [])}
    for line in document.get('lines', []):
        text = line.get('text', '')
        if document.get('device') != 'cloud' and line.get('reading_source') != 'vision' and not READOUT.fullmatch(text):
            continue
        match = re.match(r'\s*(' + NUMBER + ')', text)
        if not match and not line.get('value'):
            continue
        region = regions.get(line.get('panel_id'))
        local_candidates = ([{'instrument':region['instrument'], 'binding_id':region.get('binding_id'),
                              'basis':region['association_basis']}] if region and region.get('instrument') else
                            [] if document.get('panel_detection') else candidates)
        unique = local_candidates[0] if len(local_candidates) == 1 else None
        value = line.get('value') or match[1]
        issue = 'decimal_uncertain' if re.fullmatch(r'[+-]?0\d+', value) else None
        result.append({'reading_id': f"{document['job_id']}:{len(result) + 1}",
            'text': text, 'value': value, 'unit': line.get('unit') or (text[match.end():].strip() if match else None) or None,
            'polygon': line.get('polygon'), 'confidence': line.get('confidence'),
            'instrument': unique['instrument'] if unique else None,
            'binding_id': unique.get('binding_id') if unique else None,
            'association_basis': unique.get('basis') if unique else 'ambiguous' if local_candidates else 'unlocalized' if document.get('panel_detection') else 'unbound',
            'instrument_candidates': local_candidates, 'quality_issue': issue,
            'human_verified': False, 'status': 'needs_review',
            'workbench': region.get('workbench') if region else document.get('workbench'), 'workbenches': document.get('workbenches', []),
            'activity_confirmed': False,
            'image_url': document.get('image_url'), 'capture_id': document['capture_id'],
            'panel_id':line.get('panel_id'), 'panel_bbox':region.get('bbox') if region else None,
            'digit_region':region.get('digit_region') if region else None,
            'temporal_confirmation':line.get('temporal_confirmation') or {'status':'single_frame'},
            'panel_image_url':region.get('image_url') if region else None,
            'panel_image_sha256':region.get('image_sha256') if region else None,
            'detector_confidence':region.get('detector_confidence') if region else None,
            'detector_weights_sha256':(document.get('panel_detection') or {}).get('weights_sha256')})
    return result
