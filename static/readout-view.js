/* Pure presentation rules shared by the workbench and its regression checks. */
(function(root){
  const instant=value=>{const n=Date.parse(value);return Number.isFinite(n)?n:0;};
  const capturedAt=job=>job.external_photo?.captured_at||job.video_observation?.captured_at||job.video_observation?.observed_at||job.submitted_at;
  function selectLatest(state,camera){
    return [state.latest_photo_job,state.latest_panel_job,
      ...(state.jobs||[]).filter(j=>['queued','running'].includes(j.status))]
      .filter(j=>j&&j.camera_id===camera)
      .sort((a,b)=>instant(capturedAt(b))-instant(capturedAt(a))||instant(b.submitted_at)-instant(a.submitted_at))[0]||null;
  }
  function duration(value){
    if(value===null||value===undefined||!Number.isFinite(value))return '—';
    if(value>=60000)return `${Math.floor(value/60000)} 分 ${((value%60000)/1000).toFixed(1)} 秒`;
    return value<1000?Math.round(value)+' ms':(value/1000).toFixed(2)+' 秒';
  }
  function timing(job){
    const t=job.timing||{},d=t.durations_ms||{};
    const video=job.request_trigger==='video_stream'||job.input_mode==='video';
    const processing=d.processing_ms??(t.run_started_at&&t.result_finished_at?
      Math.max(0,instant(t.result_finished_at)-instant(t.run_started_at)):null);
    return {label:video?'视频帧至结果':t.is_backfill?'补处理 · 发现至结果':'写入至结果',
      total:video?d.frame_to_result_ms:t.is_backfill?d.detect_to_result_ms:d.write_to_result_ms,
      processing,ingestWait:d.ingest_wait_ms,
      delayedIngest:!video&&d.ingest_wait_ms>=30000,
      retries:t.ingest_retry_count||0};
  }
  function historical(job,bindings,now=Date.now()){
    const ids=[job.binding_id,...(job.binding_ids||[])].filter(Boolean);
    const current=new Set((bindings||[]).filter(b=>!b.ended_at).map(b=>b.binding_id));
    return (ids.length>0&&!ids.some(id=>current.has(id)))||
      (instant(capturedAt(job))>0&&now-instant(capturedAt(job))>120000);
  }
  const api={capturedAt,selectLatest,duration,timing,historical};
  root.ReadoutView=api;
  if(typeof module!=='undefined')module.exports=api;
})(globalThis);
