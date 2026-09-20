#!/usr/bin/env python3
"""Read a copied Evidence.json/job receipt without fetching images or touching a DB."""
import argparse
import json
from pathlib import Path


def audit(document):
    observations = document.get('observations') or ([document] if document.get('job_id') else [])
    tasks = []
    for job in observations:
        snapshots = job.get('all_binding_snapshots') or job.get('binding_snapshots') or []
        source = next((s for s in document.get('sources', []) if s.get('capture_id') == job.get('capture_id')), {})
        expected = sorted({b['instrument']['id'] for b in snapshots
                           if b.get('attribution_status') != 'needs_review' and (b.get('instrument') or {}).get('id')})
        local = job.get('local_ocr') or {}
        detection = job.get('panel_detection') or local.get('panel_detection') or {}
        allowed = detection.get('allowed_instrument_ids')
        fields = []
        for region in job.get('panel_regions', []):
            readings = [r for r in job.get('readings', []) if r.get('panel_id') == region.get('panel_id')]
            fields.append({'instrument_id':region.get('instrument_id') or (region.get('instrument') or {}).get('id'),
                'binding_id':region.get('binding_id'), 'panel_id':region.get('panel_id'),
                'name':region.get('measurement_name'), 'bbox':region.get('bbox'),
                'identity_evidence':region.get('identity_evidence'), 'display_state':region.get('display_state'),
                'readings':[{'raw_text':r.get('text'), 'raw_value':r.get('value'), 'unit':r.get('unit'),
                             'quality_issue':r.get('quality_issue')} for r in readings]})
        tasks.append({'task_id':job.get('job_id'), 'capture_id':job.get('capture_id'),
            'captured_at':(job.get('external_photo') or {}).get('captured_at') or source.get('captured_at'),
            'attribution_status':job.get('attribution_status'), 'attribution_reason':job.get('attribution_reason'),
            'snapshot_instrument_ids':expected, 'allowed_instrument_ids':allowed,
            'allowed_matches_snapshot':set(allowed)==set(expected) if allowed is not None and snapshots else None,
            'binding_snapshot_count':len(snapshots), 'status':job.get('status'),
            'skip_reason':job.get('skip_reason'), 'failure_reason':job.get('failure_reason') or local.get('failure_reason'),
            'detection_diagnostics':detection.get('diagnostics'), 'fields':fields})
    return {'validation_scope':'receipt_structure_and_binding_consistency_only',
            'evidence_level':'PARTIAL_EVIDENCE', 'real_image_accuracy':'NOT_PROVEN',
            'group_id':document.get('measurement_id') or document.get('group_id') or document.get('event_id'),
            'task_count':len(tasks), 'tasks':tasks,
            'unit_statistics_note':'Count fields per instrument and task; do not interpret group counts as field accuracy.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt',type=Path,help='Local copied Evidence.json or job JSON; read only')
    args = parser.parse_args()
    print(json.dumps(audit(json.loads(args.receipt.read_text())),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
