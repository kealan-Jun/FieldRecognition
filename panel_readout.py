"""One user-requested photograph: resident local OCR first, timed vision fallback."""
import hashlib
import json
import time
from concurrent.futures import Future
from functools import partial

import cv2

import aliyun_vision as vision
from panel_regions import needs_fallback, finish as finish_regions
from readout_timing import update_timing


def save(core, document):
    if document['status'] in {'completed', 'failed', 'cancelled', 'interrupted'}:
        shutdown = bool(core.get('stopping') and core['stopping'].is_set())
        document['resume_pending'] = shutdown and document.get('request_trigger') != 'video_stream'
        if document['resume_pending']:
            document['status'] = 'interrupted'
    update_timing(document)
    with core['db']() as conn:
        conn.execute('UPDATE jobs SET status=?,document=? WHERE id=?',
                     (document['status'], json.dumps(document), document['job_id']))
        if document.get('measurement_id'):
            from photo_measurements import refresh
            refresh(core, conn, document['measurement_id'])


def run(core, document, *, clock=time.monotonic, pause=time.sleep):
    started = clock()
    future = None
    local = None
    cloud = None
    document.update(status='running', phase='local_ocr', started_at=core['now'](),
                    reading_mode='one_requested_photo', no_digits_timeout_seconds=vision.no_digits_seconds())
    save(core, document)

    def valid():
        return core['current_readout_binding'](document)

    def result_if_ready():
        return future.result() if future.done() and not future.cancelled() else None

    def cancelled():
        document.update(status='cancelled', error='binding_ended_or_changed', lines=[])

    try:
        if not valid():
            cancelled()
            return
        image = cv2.imread(str(core['DATA'] / 'Images' / f"{document['capture_id']}.png"))
        x, y, w, h = document['crop']
        panel = image[y:y+h, x:x+w].copy()
        path = core['DATA'] / 'Images' / f"{document['job_id']}.png"
        if not cv2.imwrite(str(path), panel):
            raise ValueError('Panel evidence encoding failed')
        document.update(crop_image_url=f"/api/images/{document['job_id']}",
                        crop_image_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        accuracy='not_human_verified', panel_selection='user_selected_crop_or_full_photo')
        precomputed = document.pop('precomputed_local', None)
        if precomputed is not None:
            future = Future()
            future.set_result(precomputed)
        else:
            predictor = partial(core['predict_readout'], binding_snapshots=document.get('all_binding_snapshots', document.get('binding_snapshots', []))) if 'predict_readout' in core else core['predict_panel']
            future = core['ocr_pool'].submit(predictor, panel, x, y)
        configured = vision.public_config()['available']
        video_elapsed = (document.get('video_observation') or {}).get('no_digits_elapsed_seconds', 0)
        deadline = started + (max(0, vision.no_digits_seconds() - video_elapsed) if configured else 90)
        # A successful numeric result returns immediately. A miss cannot trigger a paid
        # request before the deadline, and slow local inference cannot block the watchdog.
        while True:
            if not valid():
                cancelled()
                return
            local = result_if_ready()
            if local and local.get('recognition_skipped'):
                break
            if local and (not configured or needs_fallback(local) is None):
                break
            remaining = deadline - clock()
            if remaining <= 0:
                break
            if local and document['phase'] != 'waiting_readout':
                document.update(phase='waiting_readout', local_ocr=local)
                save(core, document)
            pause(min(.1, remaining))
        local = result_if_ready()
        if local and local.get('recognition_skipped'):
            document.update(local)
            document.update(local_ocr=local, outcome='skipped_unbound_or_unlocalized_panel', phase='completed')
            return
        if local and local.get('panel_regions'):
            finish_regions(core, document, local, image, valid=valid, clock=clock, started=started)
            return
        if core.get('panel_detector') and core['panel_detector'].enabled():
            # Missing/slow localization must never fall back to reading the entire
            # frame: that could include an unbound laptop or another instrument.
            document.update(status='failed', error='bound_panel_localization_unavailable', lines=[],
                fallback={'status':'skipped', 'reason':'no_verified_bound_panel'},
                panel_detection={'status':'unavailable'}, panel_regions=[])
            return
        reason = vision.fallback_reason(local['lines'], local.get('error')) if local else 'local_ocr_timeout'
        if reason and configured:
            if not valid():
                cancelled()
                return
            fallback_scope = document.get('binding_id') or 'unbound-camera:' + document['camera_id']
            # Timers for separate photos can overlap, but paid requests must not.
            # A busy provider slot does not consume the persistent cooldown.
            if not core['vision_call_lock'].acquire(blocking=False):
                document['fallback'] = {'status': 'skipped', 'reason': 'busy'}
            else:
                try:
                    if not valid():
                        cancelled()
                        return
                    if core['reserve_fallback'](fallback_scope):
                        document.update(phase='extended_reading', fallback={'status': 'running', 'trigger': reason,
                                        'trigger_elapsed_seconds': round(clock() - started + video_elapsed, 3), 'attempted_at': core['now']()})
                        save(core, document)
                        cloud = vision.read_panel(panel)
                        document['fallback'].update(cloud)
                        document['fallback']['finished_at'] = core['now']()
                    else:
                        document['fallback'] = {'status': 'skipped', 'reason': 'cooldown', 'cooldown_seconds': 30}
                finally:
                    core['vision_call_lock'].release()
        local = result_if_ready()
        document['local_ocr'] = local or {'status': 'running', 'lines': [], 'model': core['ocr_state']['engine']}
        if not valid():
            cancelled()
            return
        if local:
            document.update(local)
        else:
            document.update(status='failed', error='local_ocr_timeout', lines=[])
        # When local OCR finishes during the vision call, prefer its valid numeric result.
        local_has_digits = local and vision.fallback_reason(local['lines'], local.get('error')) is None
        if cloud and cloud.get('answer', {}).get('readings') and not local_has_digits:
            document.update(status='completed', model=cloud['model'], device='cloud', actual_model_invocation=True,
                            lines=[{'text': r['text'], 'value': r['value'], 'unit': r.get('unit'), 'label': r.get('label'), 'numeric_candidates': [r['value']],
                                    'confidence': None, 'polygon': None} for r in cloud['answer']['readings']])
            document.pop('error', None)
        document['outcome'] = ('numeric_readout' if local_has_digits or
            (cloud and cloud.get('answer', {}).get('readings')) else 'no_numeric_readout')
    except Exception as exc:
        document.update(status='failed', error=type(exc).__name__, lines=[])
    finally:
        if document.get('panel_detection') and not document.get('panel_regions'):
            document['capture_association'] = {k:document.get(k) for k in ('instrument','binding_id','instrument_candidates','workbench')}
            document.update(instrument=None, binding_id=None, instrument_candidates=[], binding_ids=[],
                instrument_association='unbound_photo', instrument_identity_basis='not_localized', association_status='unlocalized')
        from reading_results import build_readings
        document['readings'] = build_readings(document)
        from measurement_records import build_records
        document['measurement_records'] = build_records(document)
        document.update(finished_at=core['now'](), wall_seconds=round(clock() - started, 3))
        if future and not document.get('local_ocr'):
            document['local_ocr'] = result_if_ready() or {'status': 'running', 'lines': []}
        save(core, document)
        if future and document.get('local_ocr', {}).get('status') == 'running':
            # An in-flight OCR operation cannot be killed safely. Its eventual raw
            # result is kept for audit, without replacing the answer already delivered.
            def late_result(completed):
                if completed.cancelled():
                    late = {'status': 'cancelled', 'lines': []}
                else:
                    try:
                        late = completed.result()
                    except Exception as exc:
                        late = {'status': 'failed', 'error': type(exc).__name__, 'lines': []}
                with core['db']() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row = conn.execute('SELECT document FROM jobs WHERE id=?', (document['job_id'],)).fetchone()
                    stored = json.loads(row['document'])
                    stored.update(local_ocr=late, local_ocr_finished_late=True)
                    update_timing(stored)
                    conn.execute('UPDATE jobs SET document=? WHERE id=?', (json.dumps(stored), document['job_id']))
            future.add_done_callback(late_result)
            future.cancel()
