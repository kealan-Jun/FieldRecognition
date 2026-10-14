"""Shared display rules and workbench boundaries, without a camera or model."""

import json
import os
import subprocess


def node(script, *, timezone="UTC"):
    return json.loads(
        subprocess.check_output(
            ["node", "-e", script], text=True, env=dict(os.environ, TZ=timezone)
        )
    )


def test_beijing_dates_do_not_follow_browser_timezone_and_preserve_precision():
    script = """
    const v=require('./static/view-utils.js');
    process.stdout.write(JSON.stringify({
      midnight:v.timestamp('2026-10-09T16:00:00.123Z'),
      precise:v.timestamp('2026-10-09T16:00:00.123Z',{milliseconds:true}),
      time:v.timestamp('2026-10-09T16:00:00.123Z',{timeOnly:true}),
      input:v.beijingInput('2026-10-09T16:00:00.123Z'),
      missing:v.timestamp(null,{missing:'未记录'}),invalid:v.timestamp('broken'),
      escaped:v.escapeHtml(`<img a="&">'`)
    }));
    """
    result = node(script)
    assert result == node(script, timezone="America/Los_Angeles")
    assert result == node(script, timezone="Asia/Shanghai")
    assert "2026/10/10" in result["midnight"] and "00:00:00" in result["midnight"]
    assert ".123" not in result["midnight"] and ".123" in result["precise"]
    assert result["time"] == "00:00:00"
    assert result["input"] == "2026-10-10T00:00:00"
    assert result["missing"] == "未记录" and result["invalid"] == "—"
    assert result["escaped"] == "&lt;img a=&quot;&amp;&quot;&gt;&#39;"


def test_camera_labels_keep_service_failure_above_streaming_and_page_overrides():
    result = node("""
    const v=require('./static/view-utils.js');
    process.stdout.write(JSON.stringify({
      offline:v.cameraStatus({status:'streaming',service_status:{online:false}}),
      default:v.cameraStatus({status:'camera_offline'}),
      detail:v.cameraStatus({status:'camera_offline'},{camera_offline:'设备离线'}),
      unknown:v.cameraStatus({}),future:v.cameraStatus({status:'future_status'}),
      sample:v.displayCamera('SampleCamera'),real:v.displayCamera('CameraA')
    }));
    """)
    assert result == {
        "offline": "采集服务中断，等待恢复",
        "default": "相机离线",
        "detail": "设备离线",
        "unknown": "状态未知",
        "future": "future_status",
        "sample": "样张相机",
        "real": "CameraA",
    }


def test_reconnecting_bindings_remain_visible_with_deadline_and_gap_notice():
    result = node("""
    const v=require('./static/view-utils.js'),b=require('./static/binding-view.js');
    const held={camera_id:'cam',connection_status:'recovering',reconnect_deadline:'2026-10-14T11:47:24Z'};
    process.stdout.write(JSON.stringify({
      camera:v.cameraStatus({service_status:{online:false,reconnectable:true}}),
      held:b.current({bindings:[held]},'cam').instruments.length,
      message:b.connectionMarkup(held),
      restored:b.connectionMarkup({...held,connection_status:'connected'}),
      ended:b.current({bindings:[{...held,ended_at:'2026-10-14T11:47:24Z'}]},'cam').instruments.length
    }));
    """)
    assert result["camera"] == "连接恢复中 · 等待新画面"
    assert result["held"] == 1 and result["ended"] == 0 and result["restored"] == ""
    assert "无需重扫" in result["message"] and "断流期间照片归属待核实" in result["message"]
    assert "19:47:24" in result["message"]


def test_auth_csrf_is_same_origin_mutation_only_and_direct_access_stays_open():
    result = node("""
    const fs=require('fs'),vm=require('vm');
    async function check(enabled){
      const calls=[],store=[];
      const sandbox={URL,Headers,Request,location:{href:'https://field.test/',origin:'https://field.test',replace:u=>store.push(u)},sessionStorage:{removeItem:k=>store.push(k)}};
      sandbox.window={fetch:async (url,init={})=>{calls.push({url,headers:Object.fromEntries(new Headers(init.headers)),method:init.method||'GET'});return {status:200,ok:true,json:async()=>({auth_enabled:enabled,csrf_token:'test-csrf'})};}};
      vm.runInNewContext(fs.readFileSync('static/auth-session.js','utf8'),sandbox);
      await sandbox.window.fetch('/api/state');
      await sandbox.window.fetch('/api/bindings',{method:'POST'});
      await sandbox.window.fetch('https://other.test/api',{method:'POST'});
      return {calls,store};
    }
    Promise.all([check(true),check(false)]).then(([enabled,direct])=>process.stdout.write(JSON.stringify({enabled,direct})));
    """)
    enabled = result["enabled"]["calls"]
    assert enabled[0]["url"] == "/api/state" and enabled[0]["headers"] == {}
    assert enabled[1]["url"] == "/api/auth/me"
    assert enabled[2]["headers"]["x-csrf-token"] == "test-csrf"
    assert enabled[3]["headers"] == {}
    assert result["direct"]["calls"][2]["headers"] == {}
    assert result["direct"]["store"] == ["field_csrf"]


def test_workbench_context_rejects_old_camera_responses_and_keeps_controller_actions():
    result = node("""
    const fs=require('fs'),vm=require('vm');
    const elements=new Map();
    function element(){return {value:'',hidden:false,disabled:false,textContent:'',dataset:{},classList:{toggle(){}},getContext(){return {clearRect(){}}},querySelectorAll(){return []},setAttribute(){},removeAttribute(){},replaceChildren(){}};}
    const document={querySelector:s=>{if(!elements.has(s))elements.set(s,element());return elements.get(s);},querySelectorAll:()=>[],addEventListener(){},body:element(),activeElement:null};
    let resolveRequest;
    const sandbox={document,Headers,DOMException,URLSearchParams,URL,AbortSignal,Set,Image:function(){},location:{href:'https://field.test/',hash:''},localStorage:{getItem(){return null},setItem(){},removeItem(){}},window:{addEventListener(){},scrollTo(){}},fetch:()=>new Promise(resolve=>{resolveRequest=resolve}),setTimeout(){return 1},clearTimeout(){},setInterval(){return 1},clearInterval(){}};
    sandbox.globalThis=sandbox;
    for(const name of ['view-utils','binding-view','readout-view','preview-controller','readout-controller','personnel-controller','history-controller','app'])vm.runInNewContext(fs.readFileSync('static/'+name+'.js','utf8'),sandbox);
    const page=sandbox.FieldWorkbench;
    page.selectedCameraId='A';
    const pending=page.api('/api/state').catch(error=>error.name);
    page.cameraEpoch++;page.selectedCameraId='B';
    resolveRequest({ok:true,status:200,json:async()=>({camera:{id:'A'}})});
    pending.then(error=>{
      page.state={camera:{id:'B'},bindings:[],scene_visits:[],automation:{enabled:false},record_policy:{mode:'test'}};
      page.scan={camera_id:'B'};
      page.renderBinding();
      page.renderOcr({job_id:'job',status:'completed',display_fields:[{name:'质量',display_value:'1.230',value:1.23,unit:'g'}],timing:{durations_ms:{write_to_result_ms:12,ocr_ms:0}},external_photo:{captured_at:'2026-10-10T00:00:00Z'}});
      process.stdout.write(JSON.stringify({error,current:page.currentCamera(),readout:elements.get('#ocrResults').innerHTML,controllers:['showScan','renderOcr','refreshPersonnel','renderHistory'].every(name=>typeof page[name]==='function')}));
    });
    """)
    assert result["error"] == "AbortError" and result["current"] == "B"
    assert result["controllers"]
    assert "1.230 g" in result["readout"] and "OCR 0 ms" in result["readout"]
