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
    result = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert result["latest"] == "new" and result["other"] is None and result["old"]
    assert result["timing"]["total"] == 3855652  # Preserve the actual elapsed time.
    assert result["timing"]["processing"] == 5000 and result["timing"]["delayedIngest"]
    assert result["duration"] == "64 分 15.7 秒"


def test_parallel_initial_and_reviewed_values_replace_the_same_result_container():
    script = """
    require('./static/readout-view.js');
    require('./static/readout-controller.js');
    globalThis.document={addEventListener:()=>{}};
    const elements={};
    const ctx={
      $:id => elements[id] ||= {innerHTML:'',textContent:'',addEventListener:()=>{}},
      busy:(_,fn)=>fn,
      esc:String, stamp:String, state:{bindings:[]},
      ReadoutView:globalThis.ReadoutView,
      ViewUtils:{timestamp:String},
    };
    globalThis.ReadoutController.install(ctx);
    const initial={job_id:'same',capture_id:'photo',status:'running',
      reading_mode:'parallel_photo',record_scope:'draft',result_available:true,
      result_stage:'preliminary',parallel_review:{status:'running'},
      display_fields:[{name:'质量',value:12.34,unit:'g'}]};
    ctx.renderOcr(initial);
    const first=elements['#ocrResults'].innerHTML;
    ctx.renderOcr({...initial,status:'completed',result_stage:'reviewed',
      parallel_review:{status:'completed',result:'updated'},
      display_fields:[{name:'质量',value:12.35,unit:'g'}]});
    const final=elements['#ocrResults'].innerHTML;
    const state={latest_photo_job:{...initial,job_id:'newer',external_photo:{captured_at:'2026-10-15T09:00:00Z'}},
      latest_panel_job:{...initial,camera_id:'cam',external_photo:{captured_at:'2026-10-15T08:00:00Z'}}};
    state.latest_photo_job.camera_id='cam';
    const unverified=ctx.ReadoutView.reviewLabel({...initial,
      parallel_review:{status:'completed',result:'unverified',reason:'local_panel_not_localized'}});
    process.stdout.write(JSON.stringify({first,final,unverified,latest:ctx.ReadoutView.selectLatest(state,'cam').job_id}));
    """
    result = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert "12.34 g" in result["first"] and "云端复核中" in result["first"]
    assert "12.35 g" in result["final"] and "12.34 g" not in result["final"]
    assert result["latest"] == "newer"
    assert result["unverified"] == "仅整图候选 · 面板未定位 · 待核实"
