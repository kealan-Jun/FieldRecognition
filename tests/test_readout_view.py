import json
import subprocess


def test_new_photo_miss_replaces_old_reading_but_delayed_old_photo_cannot_replace_newer():
    script = """
    const v=require('./static/readout-view.js');
    const old={job_id:'old',camera_id:'cam',status:'completed',binding_id:'previous',
      external_photo:{captured_at:'2026-09-15T13:21:00+08:00'},submitted_at:'2026-09-15T06:25:11Z'};
    const photo={job_id:'new',camera_id:'cam',status:'completed',readings:[],
      external_photo:{captured_at:'2026-09-15T17:23:00+08:00'},submitted_at:'2026-09-15T09:23:01Z'};
    const late={...old,job_id:'late',status:'queued',submitted_at:'2026-09-15T10:00:00Z'};
    const state={latest_panel_job:old,latest_photo_job:photo,jobs:[late]};
    const result={latest:v.selectLatest(state,'cam').job_id,other:v.selectLatest(state,'other'),
      old:v.historical(old,[{binding_id:'current'}],Date.parse('2026-09-15T09:24:00Z')),
      timing:v.timing({timing:{run_started_at:'2026-09-15T06:25:11Z',result_finished_at:'2026-09-15T06:25:16Z',
        durations_ms:{ingest_wait_ms:3849445,write_to_result_ms:3855652,ocr_ms:18}}}),
      duration:v.duration(3855652)};
    process.stdout.write(JSON.stringify(result));
    """
    result=json.loads(subprocess.check_output(['node','-e',script],text=True))
    assert result['latest']=='new' and result['other'] is None and result['old']
    assert result['timing']['total']==3855652  # Preserve the actual elapsed time.
    assert result['timing']['processing']==5000 and result['timing']['delayedIngest']
    assert result['duration']=='64 分 15.7 秒'
