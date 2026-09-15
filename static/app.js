const $ = s => document.querySelector(s);
let activeBindings = [], videoState, videoTimer;
let state, scan, activeBinding, picture, stream, crop = null, drag = null, jobTimer;
let previewLive = false, scanSession = null, scanTimer, cameraTimer, scanStarting = false, displayedJobKey = null;
let operatorDirty = false, automationScanId = null, previewInitialized = false, previewRetry;
let historicalScan = false, recognitionPreview = false;
function previewUrl(){return "/api/camera/preview.mjpg"+(recognitionPreview?"?recognition=true":"");}
let historyMode='all', historyStack=[null], historyNext=null, historyData=[], historyLoading=false, historyRequest=0;
let historyRenderedKey=null, workbenchData=null, workbenchLoading=false;
const pendingButtons = new Set();
const canvas = $('#imageCanvas'), ctx = canvas.getContext('2d');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const displayCamera = s => s==='DemoSampleCamera'||s==='SampleCamera'?'样张相机':s;
const displayOperator = s => s==='Demo验证'?'样张验证':s;
const stamp = s => s ? new Date(s).toLocaleString('zh-CN',{hour12:false}) : '—';
function message(text,error=false){$('#message').hidden=false;$('#message').className=error?'error':'';$('#message').textContent=text;}
async function api(path,options={}) {const r=await fetch(path,options);const data=await r.json();if(!r.ok){const error=Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));error.status=r.status;throw error;}return data;}
const json = data=>({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
function busy(button,work){return async()=>{pendingButtons.add(button);button.disabled=true;try{await work();}catch(e){message(e.message,true);}finally{pendingButtons.delete(button);button.disabled=false;updateButtons();}};}
function updateButtons(){
  const manualScanning = Boolean(scanSession) || scanStarting;
  const autoScanning = state?.automation?.enabled && ['scanning','waiting_camera'].includes(state.automation.session?.status);
  const scanning = manualScanning || autoScanning;
  $('#enterScene').hidden=manualScanning||historicalScan||state?.automation?.enabled||!scan?.scene_matches?.length;
  $('#bind').hidden=manualScanning||historicalScan||Boolean(state?.automation?.enabled)||!scan?.matches?.length;
  $('#bind').textContent=scan?.matches?.length>1?'绑定所选仪器':'绑定这台仪器';
  $('#enterScene').disabled=manualScanning||!scan?.scene_matches?.length;
  $('#bind').disabled=manualScanning||!scan?.matches?.length;
  $('#ocr').disabled=historicalScan||previewLive||!scan||Boolean(jobTimer);
  $('#endBinding').hidden=!activeBindings.length&&!state?.scene_visits?.some(v=>v.camera_id===currentCamera()); $('#endBinding').disabled=false;
  $('#continuousScan').disabled=scanning||state?.automation?.enabled||state?.camera.mode!=='gwhp_main';
  $('#stopScan').hidden=!scanSession; $('#operator').disabled=scanning||Boolean(activeBindings.length);
  $('#autoEnable').disabled=scanning;
  $('#autoPause').disabled=!state?.automation?.enabled;
  for(const s of ['#neckCapture','#upload','#webcamOpen','#shutter', '[data-sample="A"]','[data-sample="B"]','[data-sample="Scene01"]']) $(s).disabled=manualScanning;
  $('#neckLive').disabled=manualScanning||state?.camera.mode!=='gwhp_main';
  $('#recognitionPreview').disabled=state?.camera.mode!=='gwhp_main';
  for(const button of pendingButtons)button.disabled=true;
}

function closePreview(){
  clearTimeout(previewRetry);
  previewLive=false; $('#neckPreview').removeAttribute('src'); $('#neckPreview').hidden=true;
  $('#liveBadge').hidden=true; $('#neckLive').textContent='打开实时视频';
  canvas.hidden=!picture; $('#empty').hidden=Boolean(picture);
  $('#sourceLabel').textContent=picture&&scan?`${scan.camera_id} · ${scan.width}×${scan.height}`:'等待拍摄';
  if(!picture)$('#cropHint').textContent='识别面板时，在照片上拖动框选显示区域。照片原件会保留。';
  updateButtons();
}
function openPreview(){
  clearTimeout(previewRetry);
  stopWebcam(); previewLive=true; canvas.hidden=true; $('#empty').hidden=true;
  $('#neckPreview').src=previewUrl(); $('#neckPreview').hidden=false;
  $('#liveBadge').hidden=false; $('#neckLive').textContent='关闭实时视频';
  $('#sourceLabel').textContent=`${state.camera.id} · 实时视频`;
  $('#cropHint').textContent='视频持续扫码；只读取已绑定设备的面板。';
  updateButtons();
}
async function pollCamera(){
  try{
    const previousBinding=activeBinding?.binding_id;
    await refresh(false);
    if(previousBinding&&!activeBinding){
      const text=state.automation?.enabled?'原绑定已结束，相机恢复后将自动重新扫码。':'原绑定已结束，可在登记区重新开启自动运行。';
      $('#liveStatus').textContent=text;message(text);
    }
    const fresh=state.camera.status==='streaming'&&state.camera.frame_age_ms<=1000&&state.camera.service_status?.online!==false;
    $('#liveBadge').textContent=fresh?(recognitionPreview?'识别叠加 · 抽样画面':'实时视频'):'等待相机新画面';
  }catch(e){$('#liveBadge').textContent='连接中断，等待恢复';}
  cameraTimer=setTimeout(pollCamera,2000);
}
$('#recognitionPreview').onclick=()=>{recognitionPreview=!recognitionPreview;$('#recognitionPreview').setAttribute('aria-pressed',String(recognitionPreview));$('#recognitionPreview').textContent=recognitionPreview?'返回流畅视频':'查看识别叠加';if(previewLive)$('#neckPreview').src=previewUrl();else openPreview();$('#liveBadge').textContent=recognitionPreview?'识别叠加 · 抽样画面':'实时视频';};
$('#neckLive').onclick=()=>{if(previewLive){closePreview();draw();}else openPreview();};
$('#neckPreview').onerror=()=>{if(!previewLive)return;$('#liveBadge').textContent='视频连接中断，正在重连';clearTimeout(previewRetry);previewRetry=setTimeout(()=>{if(previewLive)$('#neckPreview').src=previewUrl()+(recognitionPreview?'&':'?')+'retry='+Date.now();},2000);};

async function handleSession(result){
  $('#liveStatus').textContent=`${result.message}${result.frames_scanned ? ` · 已扫描 ${result.frames_scanned} 帧` : ''}`;
  if(['scanning','waiting_camera'].includes(result.status))return false;
  scanSession=null; clearTimeout(scanTimer); scanTimer=null;
  await refresh(false);
  if(result.scan)await showScan(result.scan,{keepLive:result.status==='bound'});
  selectBindings(result.camera_id);
  renderBinding(); updateButtons();
  message(result.message,['failed','needs_selection'].includes(result.status));
  return true;
}
async function pollScan(){
  const id=scanSession;if(!id)return;
  try{
    const result=await api(`/api/camera/scan-sessions/${id}`);
    if(scanSession!==id)return;
    if(await handleSession(result))return;
  }catch(e){
    if(scanSession!==id)return;
    if(e.status===404){scanSession=null;updateButtons();message('扫码会话已结束，请重新开始；已有绑定会从服务端恢复。',true);return;}
    $('#liveStatus').textContent='扫码状态暂不可用，正在重连；页面断开超过 20 秒后服务端停止扫码。';
  }
  if(scanSession===id)scanTimer=setTimeout(pollScan,500);
}
$('#continuousScan').onclick=busy($('#continuousScan'),async()=>{
  const operator=$('#operator').value.trim();if(!operator)throw Error('请先填写右侧实验员姓名或编号');
  scanStarting=true;updateButtons();
  try{
    const result=await api('/api/camera/scan-sessions',json({operator}));
    localStorage.setItem('fieldOperator',operator);openPreview();
    scanSession=result.session_id;
    if(!await handleSession(result))pollScan();
  }finally{scanStarting=false;updateButtons();}
});
$('#stopScan').onclick=busy($('#stopScan'),async()=>{
  const id=scanSession;if(!id)return;
  const result=await api(`/api/camera/scan-sessions/${id}`,{method:'DELETE'});
  await handleSession(result);
});
function draw(){if(!picture)return;ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(picture,0,0);if(crop){ctx.fillStyle='#1e4a5222';ctx.fillRect(...crop);ctx.strokeStyle='#cf8337';ctx.lineWidth=Math.max(3,canvas.width/350);ctx.strokeRect(...crop);}$('#resetCrop').hidden=!crop;$('#cropHint').textContent=previewLive?'视频持续扫码；只读取已绑定设备的面板。':crop?`已选面板区域：${crop[2]} × ${crop[3]} 像素。识别结果保留对应原图位置。`:'拖动框选面板；未选框时会识别整张照片。';}
function point(e){const r=canvas.getBoundingClientRect();return [Math.max(0,Math.min(canvas.width,Math.round((e.clientX-r.left)*canvas.width/r.width))),Math.max(0,Math.min(canvas.height,Math.round((e.clientY-r.top)*canvas.height/r.height)))];}
canvas.onpointerdown=e=>{drag=point(e);canvas.setPointerCapture(e.pointerId);};
canvas.onpointermove=e=>{if(!drag)return;const p=point(e);crop=[Math.min(drag[0],p[0]),Math.min(drag[1],p[1]),Math.abs(p[0]-drag[0]),Math.abs(p[1]-drag[1])];draw();};
canvas.onpointerup=()=>{drag=null;if(crop&&(crop[2]<16||crop[3]<16))crop=null;draw();};
$('#resetCrop').onclick=()=>{crop=null;draw();};
function renderQrEvidence(){
  const hits=[...(scan?.matches||[]),...(scan?.scene_matches||[])];
  const figure=$('#qrEvidence');figure.hidden=!hits.length;
  if(!hits.length)return;
  const receipt=state?.bindings?.find(item=>item.scan_id===scan.scan_id);
  const label=receipt?(receipt.ended_at?'历史绑定凭证':'绑定成功帧'):historicalScan?'历史识别 · 未绑定':'识别命中帧';
  $('#qrEvidenceTitle').textContent=hits.map(item=>item.name).join('、')+' · '+label;
  $('#qrEvidenceTime').textContent='收到画面：'+stamp(scan.received_at)+' · '+scan.camera_id;
  $('#qrEvidenceLink').href=scan.image_url;$('#qrEvidenceImage').src=scan.image_url;
  const overlay=$('#qrEvidenceOverlay');overlay.setAttribute('viewBox',`0 0 ${scan.width} ${scan.height}`);overlay.replaceChildren();
  for(const item of hits){
    if(!Array.isArray(item.polygon)||!item.polygon.every(point=>Array.isArray(point)&&point.length===2&&point.every(Number.isFinite)))continue;
    const polygon=document.createElementNS('http://www.w3.org/2000/svg','polygon');
    polygon.setAttribute('points',item.polygon.map(point=>point.join(',')).join(' '));
    overlay.appendChild(polygon);
  }
}
function renderScan(){renderQrEvidence(); $('#sceneResults').innerHTML=(scan.scene_matches||[]).map(s=>`<label class="match"><input type="radio" name="scene" value="${esc(s.id)}" ${(scan.scene_matches.length===1)?'checked':''}><strong>${esc(s.name)}</strong></label>`).join(''); $('#scanBadge').textContent=historicalScan?'历史识别':(scan.matches.length||scan.scene_matches?.length)?'已读出二维码':scan.status==='no_qr'?'二维码未能解码':'未登记';$('#scanResults').innerHTML=scan.matches.length?scan.matches.map((a,i)=>`<label class="match">${!historicalScan&&scan.matches.length>1?`<input type="radio" name="instrument" value="${esc(a.id)}">`:''}<div><strong>${esc(a.name)}</strong><small>${esc(a.scene||'尚未登记场景，请在下方填写')}</small><small>${esc(a.id)}</small></div></label>`).join(''):`<p>${esc(scan.unknown.join('；')||(scan.scene_matches?.length?'场景码已识别，请先确认进入场景。':'二维码未能解码。请让标签平整、正对镜头并避开反光；调整距离直到黑白格边缘清晰，再拍照。仅靠放大无法恢复失焦细节。若拍的是面板，可沿用已确认的仪器绑定进行 OCR。'))}</p>`; }
async function showScan(result,{keepLive=false,historical=false}={}){if(!keepLive)closePreview();historicalScan=historical;scan=result;crop=null;picture=new Image();await new Promise((resolve,reject)=>{picture.onload=resolve;picture.onerror=()=>reject(Error('图片预览失败'));picture.src=result.image_url;});canvas.width=picture.width;canvas.height=picture.height;canvas.hidden=previewLive;$('#empty').hidden=true;stopWebcam();$('#sourceLabel').textContent=previewLive?`${state.camera.id} · 实时视频`:`${result.camera_id} · ${result.width}×${result.height}`;renderScan();draw();selectBindings(scan.camera_id);renderBinding();updateButtons();}
async function upload(blob,source=activeBinding?.camera_id||'UploadedPhoto'){const form=new FormData();form.append('file',blob,'Capture.png');form.append('camera_id',source);await showScan(await api('/api/scans',{method:'POST',body:form}));}
$('#upload').onchange=async e=>{const file=e.target.files[0];if(!file)return;try{message('正在解码图片与二维码…');await upload(file);message('图片已留存，可读取面板。');}catch(err){message(err.message,true);}e.target.value='';};
document.querySelectorAll('[data-sample]').forEach(b=>b.onclick=busy(b,async()=>{const r=await fetch(`/static/labels/${b.dataset.sample==='Scene01'?'Scene01':'Instrument'+b.dataset.sample}.png`);await upload(await r.blob(),'SampleCamera');message('这是二维码样张解码结果，可用于验证扫码与绑定流程。');}));
$('#neckCapture').onclick=busy($('#neckCapture'),async()=>{await showScan(await api('/api/camera/capture',{method:'POST'}));message('已取得挂脖设备照片。');});
function stopWebcam(){if(stream)stream.getTracks().forEach(t=>t.stop());stream=null;$('#webcam').hidden=true;$('#shutter').hidden=true;}
$('#webcamOpen').onclick=busy($('#webcamOpen'),async()=>{closePreview();if(!navigator.mediaDevices?.getUserMedia)throw Error('本机摄像头需要 localhost 或 HTTPS；挂脖设备取图和上传不受此限制。');stopWebcam();stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'},audio:false});$('#webcam').srcObject=stream;$('#webcam').hidden=false;$('#shutter').hidden=false;canvas.hidden=true;$('#empty').hidden=true;});
$('#shutter').onclick=busy($('#shutter'),async()=>{const v=$('#webcam'),c=document.createElement('canvas');c.width=v.videoWidth;c.height=v.videoHeight;if(!c.width)throw Error('摄像头尚未就绪');c.getContext('2d').drawImage(v,0,0);await upload(await new Promise(r=>c.toBlob(r,'image/png')),'BrowserCamera');});
function waitingBindingText(){
  if(state?.automation?.enabled){
    return state.automation.status==='waiting_camera'
      ?'等待相机恢复，恢复后将自动扫码绑定，无需手动确认。'
      :'等待仪器二维码。识别后自动绑定，无需确认。';
  }
  if(historicalScan)return '当前未绑定。这里展示历史识别照片；开启自动运行后，使用新的相机画面绑定。';
  return '当前未绑定。登记实验员并开启自动运行后，仪器码入镜即可绑定。';
}
function currentCamera(){return previewLive?state?.camera.id:scan?.camera_id||state?.camera.id;}
function selectBindings(camera){activeBindings=(state?.bindings||[]).filter(b=>!b.ended_at&&b.camera_id===camera);activeBinding=activeBindings.length===1?activeBindings[0]:null;}
function renderBinding(){
  selectBindings(currentCamera());
  const visits=(state?.scene_visits||[]).filter(v=>v.camera_id===currentCamera());
  $('#sceneInfo').innerHTML=visits.map(v=>`<div>场景 · ${esc(v.scene.name)} <a href="${esc(v.image_url)}" target="_blank" rel="noopener">扫码凭证 ↗</a></div>`).join('');
  $('#sceneInfo').hidden=!visits.length;
  $('#bindingInfo').innerHTML=activeBindings.length?activeBindings.map(b=>`<div class="bound-item"><b>${esc(b.instrument.name)}</b><small>${esc(b.operator)} · ${esc(stamp(b.started_at))}</small><a href="${esc(b.image_url)}" target="_blank" rel="noopener">绑定命中帧 ↗</a></div>`).join(''):visits.length?'场景已关联，继续扫描仪器码。':waitingBindingText();
  $('#scanResults').hidden=historicalScan||Boolean(state?.automation?.enabled);
  $('#scanBadge').textContent=`${activeBindings.length} 台仪器 · ${visits.length} 个场景`;
  if(scan)renderQrEvidence();updateButtons();
}
$('#bind').onclick=busy($('#bind'),async()=>{const selected=scan?.matches?.length===1?{value:scan.matches[0].id}:$('input[name="instrument"]:checked');if(!selected)throw Error('请选择本次使用的仪器');const operator=$('#operator').value.trim();if(!operator)throw Error('请填写实验员姓名或编号');activeBinding=await api('/api/bindings',json({scan_id:scan.scan_id,instrument_id:selected.value,operator}));localStorage.setItem('fieldOperator',operator);await refresh(false);renderBinding();message('绑定已保存。现在可拍摄面板并框选识别区域。');});
$('#endBinding').onclick=busy($('#endBinding'),async()=>{await api('/api/camera/relations/end',json({camera_id:currentCamera()}));await refresh(false);message('本轮仪器与场景关联已结束；历史记录保留。');});
function renderVideoOcr(v){
  videoState=v;if(!v)return;
  const status={paused:'视频识别已暂停',unconfigured:'等待配置相机',waiting_operator:'等待实验员登记',waiting_binding:'等待扫码绑定设备，暂不识别面板',waiting_camera:'等待新鲜画面',loading_model:'正在加载模型',inferring:'视频持续识别中',watching:'视频持续识别中',yielding_to_photo:'优先处理语音照片',error:'视频识别异常'}[v.status]||'正在准备';
  $('#videoOcrStatus').textContent=`${status}${v.pending_panels&&['watching','inferring'].includes(v.status)?' · '+v.pending_panels+' 个面板自动校验中':''} · 已推理 ${v.frames_inferred||0} 帧 · 留存 ${v.evidence_saved||0} 帧${v.last_result_at?' · 更新 '+new Date(v.last_result_at).toLocaleTimeString('zh-CN',{hour12:false}):''}`;
  $('#videoOcrToggle').textContent=v.enabled?'暂停视频识别':'开启视频识别';
  $('#videoReadings').innerHTML=(['waiting_camera','waiting_binding','paused','error'].includes(v.status)?[]:v.latest_lines||[]).map(line=>`<div class="live-reading"><strong>${esc(line.text)}</strong><small>${esc(line.instrument?.name||'设备未定位')} · 自动读数</small></div>`).join('')||'<span class="caption">等待画面中出现清晰读数</span>';
}
async function pollVideo(){try{renderVideoOcr(await api('/api/video-ocr'));}catch(e){$('#videoOcrStatus').textContent='视频识别状态连接中，正在重试';}videoTimer=setTimeout(pollVideo,500);}
$('#videoOcrToggle').onclick=busy($('#videoOcrToggle'),async()=>{renderVideoOcr(await api('/api/video-ocr',{...json({enabled:!videoState?.enabled}),method:'PUT'}));});
pollVideo();
function readoutPhoto(job){
  const url=job.image_url||job.crop_image_url;
  return url?`<a class="readout-photo" href="${esc(url)}" target="_blank" rel="noopener"><img src="${esc(url)}" alt="本次语音拍照原图"><span>本次照片 ↗</span></a>`:'';
}
function readoutLatency(job){
  const timing=job.timing||{},d=timing.durations_ms||{};
  const seconds=value=>value===null||value===undefined?'—':(value/1000).toFixed(2)+' 秒';
  const label=job.request_trigger==='video_stream'?'视频帧至结果':timing.is_backfill?'补处理 · 发现至结果':'写入至结果';
  const total=job.request_trigger==='video_stream'?d.frame_to_result_ms:timing.is_backfill?d.detect_to_result_ms:d.write_to_result_ms;
  return `<div class="readout-latency" title="NAS 写入时间取自文件修改时间，尚未独立校准时钟；完整阶段时间保存在结果 JSON"><b>${label} ${seconds(total)}</b><span>${job.request_trigger==='video_stream'?'视频变化证据':timing.is_backfill?'历史照片补处理，不代表实时延迟':'写入至发现 '+seconds(d.write_to_detect_ms)} · OCR ${seconds(d.ocr_ms)}${d.vision_ms!==null&&d.vision_ms!==undefined?' · 进一步识别 '+seconds(d.vision_ms):''}</span></div>`;
}
function readoutIssue(job){
  const fallback=job.fallback||{};
  if(fallback.status==='skipped')return `<p class="caption">${fallback.reason==='busy'?'进一步识别正在处理其他照片，本次未调用。':'进一步识别处于调用间隔内，本次未调用。'}原因已留存。</p>`;
  if(fallback.status!=='failed')return '';
  const reason=fallback.error==='account_arrearage'?'阿里云账户欠费或余额状态受限，请恢复账户后重试。':`进一步识别未完成${fallback.http_status?'（HTTP '+fallback.http_status+'）':''}，具体原因已留存。`;
  return `<p class="readout-issue">${esc(reason)}</p>`;
}
// Match complete number + optional unit, consistent with the server's readout rule.
const completeReadout=/^\s*[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+-]?\d+)?\s*(?:%|°?[CF]|℃|℉|[μµu]?g|kg|mg|ml|mL|L|rpm|r\/min|[mkM]?[AVW]|[kM]?Hz|Pa|kPa|MPa|bar|mm|cm|m|s|min|h|pH|ppm)?\s*$/i;
function renderOcr(job){
  const target=$('#ocrResults');
  if(job.record_scope==='draft'){
    target.innerHTML=readoutPhoto(job)+`<p>拍照 OCR ${['queued','running'].includes(job.status)?'正在处理':'已生成测量草稿'}，确认后提交实验记录。</p><a href="/photo-measurements">查看草稿与校正 ↗</a>`;return;
  }
  if(['queued','running'].includes(job.status)){
    const phase={local_ocr:'正在识别面板',waiting_readout:'暂未读到数字，继续等待识别结果',extended_reading:'正在进一步识别面板'}[job.phase]||'任务已入队';
    target.innerHTML=readoutPhoto(job)+`<p>◌ ${phase}…</p>`;return;
  }
  if(job.status!=='completed'){target.innerHTML=readoutPhoto(job)+`<p>${job.status==='cancelled'?'这次识别已取消，原因已保存在回执中。':'识别未完成，请稍后重新拍摄。'}</p>${readoutLatency(job)}${readoutIssue(job)}<a href="/api/jobs/${job.job_id}" target="_blank" rel="noopener">结果 JSON ↗</a>`;return;}
  if(job.recognition_skipped){target.innerHTML=readoutPhoto(job)+`<p>${job.skip_reason==='no_active_instrument_binding'?'拍照时没有有效仪器绑定，已跳过读数识别。':'未检测到已绑定设备的面板，已跳过读数识别。'}</p>`;return;}
  const lines=(job.lines||[]).filter(line=>job.device==='cloud'?line.numeric_candidates?.length:completeReadout.test(line.text));
  target.innerHTML=readoutPhoto(job)+`<p class="caption">${esc(job.instrument?.name||(job.association_status==='localized_panels'?'A / B 面板分别定位':job.instrument_candidates?.length?'设备定位中':'未绑定仪器'))} · ${esc(stamp(job.external_photo?.captured_at||job.video_observation?.observed_at||job.submitted_at))}<br>自动识别结果</p>${readoutLatency(job)}${lines.length?(job.readings?.length?job.readings:lines).map(line=>`<div class="ocr-line">${line.instrument?`<small>${esc(line.instrument.name)}</small>`:""}<strong>${esc(line.text)}</strong>${line.panel_image_url?`<a href="${esc(line.panel_image_url)}" target="_blank" rel="noopener">本面板照片 ↗</a>`:""}${line.association_basis==='ambiguous'?'<small>无法确定设备归属，本条未作为设备读数</small>':''}${line.quality_issue?`<small>${line.quality_issue==='possible_display_self_test'?'疑似屏幕自检，不作为测量值':'小数点信息不足，本条未作为有效数值'}</small>`:''}</div>`).join(''):'<p>这次照片未读出数字，请调整角度、清晰度或选框后重新拍摄。</p>'}${readoutIssue(job)}<p><a href="${job.crop_image_url}" target="_blank" rel="noopener">查看面板照片 ↗</a> · <a href="/api/jobs/${job.job_id}" target="_blank" rel="noopener">结果 JSON ↗</a></p>`;
}
function watchJob(job){
  renderOcr(job);clearInterval(jobTimer);
  jobTimer=setInterval(async()=>{try{
    const result=await api(`/api/jobs/${job.job_id}`);
    renderOcr(result);
    if(!['queued','running'].includes(result.status)){clearInterval(jobTimer);jobTimer=null;updateButtons();await refresh(false);}
  }catch(e){clearInterval(jobTimer);jobTimer=null;message(e.message,true);updateButtons();}},1000);
  updateButtons();
}
$('#ocr').onclick=busy($('#ocr'),async()=>{watchJob(await api('/api/ocr',json({binding_id:crop?activeBinding?.binding_id||null:null,auto_associate:true,capture_id:scan.capture_id,crop})));});
function renderLatestJob(){
  if(jobTimer)return;
  const camera=previewLive?state.camera.id:scan?.camera_id||state.camera.id;
  const latest=state.jobs.find(j=>j.camera_id===camera&&['queued','running'].includes(j.status))||
    (state.latest_panel_job?.camera_id===camera?state.latest_panel_job:null);
  const key=latest?`${latest.job_id}:${latest.status}:${latest.phase}:${latest.finished_at}`:`no-related:${camera}`;
  if(key!==displayedJobKey){
    displayedJobKey=key;
    if(latest)renderOcr(latest);else $('#ocrResults').textContent='暂无有效面板读数。正在等待已绑定设备的清晰画面或语音照片。';
  }
}
function renderPhotoWatch(){
  $('#recordPolicy').textContent=state.record_policy?.mode==='production'?'生产拍照草稿 · 确认提交 ↗':'测试用途 · 查看测量草稿 ↗';
  const watch=state.photo_watch;
  if(!watch){$('#photoWatchState').textContent='';return;}
  const labels={disabled:'照片监控未开启',waiting_binding:'正在准备照片监控',watching:'正在等待新的语音拍照文件',waiting_photo:'等待这台相机的第一张语音照片',waiting_queue:'已有照片正在识别，稍后处理新照片',storage_unavailable:'拍照存储暂不可用，正在等待恢复',storage_unconfigured:'尚未配置拍照存储',watch_error:'照片监控暂不可用',invalid_camera_directory:'相机照片目录不可用'};
  $('#photoWatchState').textContent=watch.last_photo_status==='rejected'?`照片未能处理：${watch.detail}`:`${labels[watch.status]||'等待语音拍照'}${watch.submitted_files!==undefined?' · 已接收 '+watch.submitted_files+' 张':''}${watch.pending_files?' · 待处理 '+watch.pending_files+' 张':''}`;
}
function renderAutomation(){
  const automatic=state.automation;
  if(!automatic)return;
  const labels={starting:'正在启动',paused:'自动扫码已暂停',waiting_operator:'等待实验员登记',waiting_config:'等待相机配置',waiting_camera:'等待相机上线',scanning:'后台自动扫码中',bound:'已绑定 · 等待语音照片',manual_scan:'正在手动扫码',retrying:'正在恢复连接'};
  $('#automationState').textContent=labels[automatic.status]||automatic.status;
  $('#automationDetail').textContent=automatic.message;
  $('#operatorSummary').textContent=automatic.operator?`实验员 · ${automatic.operator}`:'请先登记实验员';if(!automatic.registered_at)$('#operatorSettings').open=true;$('#operatorRegistration').textContent=automatic.registered_at?`已登记：${automatic.operator} · ${stamp(automatic.registered_at)}。更换实验员前请先结束当前绑定。`:'首次使用请登记姓名或编号，保存后本机持续运行，关闭网页也有效。';
  if(!operatorDirty && document.activeElement!==$('#operator'))$('#operator').value=automatic.operator||'';
  if(automatic.enabled){
    const session=automatic.session;
    const detail=session?.message||automatic.message;
    $('#automationDetail').textContent=detail;
    $('#liveStatus').textContent=detail+(session?.frames_scanned?` · 已扫描 ${session.frames_scanned} 帧`:'');
  }
  updateButtons();
}
$('#operator').oninput=()=>{operatorDirty=true;};
$('#autoEnable').onclick=busy($('#autoEnable'),async()=>{
  const operator=$('#operator').value.trim();
  if(!operator)throw Error('请先登记实验员姓名或编号');
  await api('/api/automation',{...json({enabled:true,operator}),method:'PUT'});
  operatorDirty=false;await refresh(false);
  message('实验员登记已保存，后台自动运行已开启；关闭网页仍会继续。');
});
$('#autoPause').onclick=busy($('#autoPause'),async()=>{
  await api('/api/automation',{...json({enabled:false}),method:'PUT'});
  await refresh(false);message('自动扫码已暂停。已有绑定和语音照片监控仍保留；结束使用请点击结束绑定。');
});
function forms(){ $('#instrumentForms').innerHTML=state.instruments.map((a,i)=>`<form class="instrument-form" data-id="${esc(a.id)}"><strong>${esc(a.name)}</strong><div class="id">${esc(a.id)}</div><div class="row"><label class="field">仪器名称<input name="name" value="${esc(a.name)}" required maxlength="80"></label><label class="field">所属场景 / 实验区域<input name="scene" value="${esc(a.scene)}" placeholder="例如：A 实验室 / 离心区" required maxlength="100"></label></div><label class="field">型号（可选）<input name="model" value="${esc(a.model)}" maxlength="100"></label><button class="secondary" type="submit">保存登记</button> <a href="/static/labels/Instrument${a.id==='e9434a0a-3319-414a-b988-4cc6884edce4'?'A':'B'}.png" target="_blank" rel="noopener">查看标签 ↗</a></form>`).join('');document.querySelectorAll('.instrument-form').forEach(form=>form.onsubmit=async e=>{e.preventDefault();const button=form.querySelector('button');button.disabled=true;try{const values=Object.fromEntries(new FormData(form));await api(`/api/instruments/${form.dataset.id}`,{...json(values),method:'PUT'});await refresh(false);if(scan){scan.matches=scan.matches.map(m=>({...m,...state.instruments.find(a=>a.id===m.id)}));renderScan();}message('仪器与场景登记已保存。');}catch(err){message(err.message,true);}finally{button.disabled=false;}}); }
async function refresh(renderForms=true){state=await api('/api/state');if(!previewInitialized&&state.camera.mode==='gwhp_main'){previewInitialized=true;if(!picture&&!stream)openPreview();}$('#cameraState').className='pill'+(state.camera.status==='streaming'?'':' warning');$('#cameraState').title=state.camera.id;$('#cameraState').textContent=state.camera.configured?`${{streaming:'主码流在线',discovering:'正在发现',reconnecting:'接收端连接失败',camera_offline:'设备离线',waiting_keyframe:'等待关键帧',not_started:'正在连接'}[state.camera.status]||'已配置取图'}`:`${state.camera.id} · 等待连接`;$('#ocrState').textContent=`PaddleOCR · ${(state.ocr.device||'cpu').toUpperCase()} · ${{not_loaded:'等待加载',queued:'等待后台加载',loading:'模型加载中',ready:'模型常驻 · 已就绪',error:'加载/识别失败'}[state.ocr.status]||state.ocr.status}${state.panel_detector?.enabled?' · '+(state.panel_detector.status==='ready'?'A/B 面板定位已就绪':state.panel_detector.status==='error'?'面板定位异常':'面板定位加载中'):''}`;selectBindings(currentCamera());renderBinding();renderLatestJob();renderPhotoWatch();renderAutomation();const autoScan=state.automation?.session?.scan||state.last_camera_scan;if(autoScan && automationScanId!==autoScan.scan_id){automationScanId=autoScan.scan_id;await showScan(autoScan,{keepLive:state.automation?.session?.status!=='needs_selection',historical:!state.automation?.session?.scan});}if(renderForms)forms();renderHistory();if(historyMode==='readouts'&&location.hash==='#history'&&!historyLoading)loadReadoutPage();if(historyMode==='workbenches'&&location.hash==='#history')loadWorkbenches();}

$('#refresh').onclick=busy($('#refresh'),()=>refresh());
$('#export').onclick=busy($('#export'),async()=>{const data=await api('/api/export');const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='FieldRecognitionSession.json';a.click();URL.revokeObjectURL(url);});
$('#operator').value='';
refresh().then(()=>{renderBinding();pollCamera();}).catch(e=>{message(e.message,true);cameraTimer=setTimeout(pollCamera,2000);});
window.addEventListener('pagehide',()=>{stopWebcam();closePreview();clearTimeout(cameraTimer);clearTimeout(videoTimer);clearTimeout(scanTimer);if(scanSession)fetch(`/api/camera/scan-sessions/${scanSession}`,{method:'DELETE',keepalive:true}).catch(()=>{});});

$('#enterScene').onclick=busy($('#enterScene'),async()=>{const chosen=$('input[name="scene"]:checked');if(!chosen)throw Error('请选择场景');await api('/api/scene/enter',json({scan_id:scan.scan_id,scene_id:chosen.value}));await refresh(false);selectBindings(scan.camera_id);renderBinding();message('已进入场景，请拍摄仪器二维码并绑定使用。');});

function duration(value){return value===null||value===undefined?'—':value<1000?Math.round(value)+' ms':(value/1000).toFixed(2)+' 秒';}
function historyLatency(event){
  if(event.kind!=='readout')return '—';
  const timing=event.timing||{},d=timing.durations_ms||{};
  const total=event.input_mode==='video'?d.frame_to_result_ms:timing.is_backfill?d.detect_to_result_ms:d.write_to_result_ms;
  const parts=[['文件发现',d.write_to_detect_ms],['稳定等待',d.file_stability_ms],['读取',d.file_read_ms],['导入等待',d.ingest_wait_ms],['导入',d.import_ms],['任务排队',d.queue_wait_ms],['OCR 等待',d.ocr_wait_ms],['OCR',d.ocr_ms],['进一步识别',d.vision_ms],['写入至归档',event.archive?.write_to_archive_ms]];
  return `<strong>${event.input_mode==='video'?'视频帧至结果':timing.is_backfill?'补处理':'写入至结果'} ${duration(total)}</strong><small>OCR ${duration(d.ocr_ms)}</small><details class="history-timing"><summary>阶段耗时</summary><dl>${parts.map(([label,value])=>`<div><dt>${label}</dt><dd>${duration(value)}</dd></div>`).join('')}</dl><small>${timing.is_backfill?'历史补处理，不计入实时性能。':'写入时间取自文件 mtime，时钟尚未独立校准。'}</small></details>`;
}
function historyArchive(event){
  const a=event.archive||{};
  return `<span class="pill ${a.status==='archived'?'':'muted'}">${a.status==='archived'?'已归档':a.status==='pending'?'待同步':'未记录'}</span>${a.archived_at?`<small>${esc(stamp(a.archived_at))}</small>`:''}`;
}
async function loadReadoutPage(){
  const request=++historyRequest, cursor=historyStack.at(-1);
  historyLoading=true;renderHistory();
  try{
    const page=await api('/api/readouts?related_only=true&limit=20'+(cursor?'&before='+cursor:''));
    if(request!==historyRequest)return;
    historyData=page.items;historyNext=page.next_cursor;
  }catch(error){if(request===historyRequest)message(error.message,true);}
  finally{if(request===historyRequest){historyLoading=false;renderHistory();}}
}
$('#historyMode').onchange=()=>{
  historyMode=$('#historyMode').value;historyStack=[null];historyNext=null;historyData=[];
  historyRequest++;historyLoading=false;
  if(historyMode==='readouts')loadReadoutPage();else if(historyMode==='workbenches')loadWorkbenches();else renderHistory();
};
$('#historyNext').onclick=()=>{if(!historyLoading&&historyNext){historyStack.push(historyNext);historyData=[];historyNext=null;loadReadoutPage();}};
$('#historyPrevious').onclick=()=>{if(!historyLoading&&historyStack.length>1){historyStack.pop();historyData=[];historyNext=null;loadReadoutPage();}};
async function loadWorkbenches(){
  if(workbenchLoading)return;workbenchLoading=true;
  try{workbenchData=await api('/api/workbenches?related_only=true');if(historyMode==='workbenches')renderHistory();}
  catch(e){message(e.message,true);}finally{workbenchLoading=false;}
}
function benchReadings(rows){return rows.slice(0,8).map(r=>`<div class="bench-reading"><strong>${esc(r.text)}</strong><span>${esc(stamp(r.captured_at))} · ${esc(r.operator||'未记录实验员')}</span><a href="${esc(r.image_url)}" target="_blank" rel="noopener">原图 ↗</a> <a href="${esc(r.result_url)}" target="_blank" rel="noopener">回执 ↗</a></div>`).join('')||'<p class="caption">暂无符合记录条件的面板读数</p>';}
function renderWorkbenches(){
  $('#historyPager').hidden=true;
  $('#historyScope').textContent='最近 200 次相关面板识别 · 每组展示最近 8 条读数；完整记录见“面板读数”';
  const groups=workbenchData?.workbenches||[];
  const key=JSON.stringify(['workbenches',groups]);if(historyRenderedKey===key)return;historyRenderedKey=key;
  $('#historyRows').innerHTML=groups.length?groups.map(g=>`<section class="bench-group"><h3>${esc(g.name)}</h3><p class="caption">${g.photo_count} 次识别 · ${g.reading_count} 条面板读数 · 按仪器分别记录，不相加</p><div class="bench-grid">${g.instruments.map(i=>`<div class="bench-instrument"><h4>${esc(i.name)} <small>${i.readings.length} 条</small></h4>${benchReadings(i.readings)}</div>`).join('')}${g.unassigned_readings.length?`<div class="bench-instrument"><h4>实验台待归属 <small>${g.unassigned_readings.length} 条</small></h4>${benchReadings(g.unassigned_readings)}</div>`:""}</div></section>`).join(''):'正在读取实验台记录…';
}
function renderHistory(){
  if(!state)return;
  const archive=state.archive||{};
  $('#archiveStatus').textContent=archive.enabled?`NAS 留存：${archive.status==='ready'?'已连接':archive.status==='retrying'?'等待重试，记录保留在本机':'正在连接'} · 已归档 ${archive.archived_receipts||0} 个版本 · 待同步 ${archive.pending_receipts||0} 个版本${archive.navigation_pending?' · 分类索引待同步':''}`:'NAS 留存未启用，记录保存在本机。';
  $('#archiveStatus').title=archive.root||'';
  const integrity=archive.integrity||{},check=integrity.last_report;
  $('#integrityStatus').textContent=integrity.status==='running'?'完整性巡检进行中':check?`最近巡检：${{completed:'校验通过',findings:'发现异常',unavailable:'存储暂不可用',interrupted:'检查中断'}[check.status]||check.status} · ${stamp(check.finished_at)} · ${check.issue_count} 项异常`:'等待首次完整性巡检';
  if(historyMode==='workbenches'){renderWorkbenches();return;}
  const events=historyMode==='readouts'?historyData:state.activity||[];
  $('#historyScope').textContent=historyMode==='readouts'?`第 ${historyStack.length} 页 · ${events.length} 条 · 按提交先后${historyLoading?' · 正在更新':''}`:`最近 ${events.length} 条操作`;
  $('#historyPager').hidden=historyMode!=='readouts';
  $('#historyPrevious').disabled=historyLoading||historyStack.length===1;
  $('#historyNext').disabled=historyLoading||!historyNext;
  const labels={photo:'照片 / 视频证据',scan:'扫码识别',binding_started:'仪器绑定',binding_ended:'结束绑定',binding_end_corrected:'绑定判定修正',binding_restored:'恢复绑定',scene_end_corrected:'场景判定修正',scene_restored:'恢复场景',scene_entered:'进入场景',scene_left:'结束场景',readout:'面板读数'};
  const renderKey=JSON.stringify([events,historyLoading&&events.length===0]);
  if(renderKey===historyRenderedKey)return;
  historyRenderedKey=renderKey;
  // Preserve expanded timing details while the background status refreshes.
  const opened=new Set([...document.querySelectorAll('#historyRows tr[data-event]')].filter(row=>row.querySelector('details[open]')).map(row=>row.dataset.event));
  $('#historyRows').innerHTML=events.length?`<div class="table-scroll"><table class="history-table"><thead><tr><th>时间 / 事件</th><th>对象与结果</th><th>实验员 / 相机</th><th>识别耗时</th><th>NAS 留存</th><th>凭证</th></tr></thead><tbody>${events.map(event=>`<tr data-event="${esc(event.event_id)}"><td>${esc(stamp(event.occurred_at))}<small>${esc(event.kind==='readout'&&event.input_mode==='video'?'视频面板读数':labels[event.kind]||event.kind)}</small>${event.captured_at?`<small>拍摄 ${esc(stamp(event.captured_at))}</small>`:''}</td><td><strong>${esc(event.target)}</strong><small>${esc(event.status)}</small>${event.detail?`<div class="history-reading">${esc(event.detail)}</div>`:''}${(event.quality_notes||[]).map(note=>`<small class="history-error">${esc(note)}</small>`).join('')}${event.fallback?.status==='failed'?`<small class="history-error">${event.fallback.error==='account_arrearage'?'账户欠费，进一步识别不可用':'进一步识别失败，见任务回执'}</small>`:''}</td><td>${esc(displayOperator(event.operator)||'当时未记录')}<small>${esc(displayCamera(event.camera_id))}</small></td><td>${historyLatency(event)}</td><td>${historyArchive(event)}</td><td>${event.image_url?`<a class="history-photo" href="${esc(event.image_url)}" target="_blank" rel="noopener"><img src="${esc(event.image_url)}" loading="lazy" decoding="async" alt="本条记录的照片">原图 ↗</a>`:''}${event.result_url?`<a href="${esc(event.result_url)}" target="_blank" rel="noopener">任务回执 ↗</a>`:''}${event.measurement_url?`<a href="${esc(event.measurement_url)}" target="_blank" rel="noopener">标准读数 ↗</a>`:''}</td></tr>`).join('')}</tbody></table></div>`:historyLoading?'正在读取记录…':'当前范围暂无相关记录。二维码识别、绑定和有明确面板归属的读数会显示在这里。';
  document.querySelectorAll('#historyRows tr[data-event]').forEach(row=>{if(opened.has(row.dataset.event)&&row.querySelector('details'))row.querySelector('details').open=true;});
}
function showPage(){
  const titles={workspace:'扫码与读数',registry:'仪器登记',history:'操作记录'};
  const page=location.hash.slice(1) in titles?location.hash.slice(1):'workspace';
  document.querySelectorAll('.page-view').forEach(view=>view.hidden=view.id!==page);
  document.querySelectorAll('.nav').forEach(link=>{const selected=link.getAttribute('href')==='#'+page;link.classList.toggle('active',selected);if(selected)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');});
  document.body.classList.toggle('workspace-screen',page==='workspace');
  $('#pageTitle').textContent=titles[page];
}
window.addEventListener('hashchange',()=>{showPage();window.scrollTo(0,0);});
showPage();
