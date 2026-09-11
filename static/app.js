const $ = s => document.querySelector(s);
let state, scan, activeBinding, picture, stream, crop = null, drag = null, jobTimer;
let previewLive = false, scanSession = null, scanTimer, cameraTimer, scanStarting = false, displayedJobKey = null;
const pendingButtons = new Set();
const canvas = $('#imageCanvas'), ctx = canvas.getContext('2d');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stamp = s => s ? new Date(s).toLocaleString('zh-CN',{hour12:false}) : '—';
function message(text,error=false){$('#message').hidden=false;$('#message').className=error?'error':'';$('#message').textContent=text;}
async function api(path,options={}) {const r=await fetch(path,options);const data=await r.json();if(!r.ok){const error=Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));error.status=r.status;throw error;}return data;}
const json = data=>({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
function busy(button,work){return async()=>{pendingButtons.add(button);button.disabled=true;try{await work();}catch(e){message(e.message,true);}finally{pendingButtons.delete(button);button.disabled=false;updateButtons();}};}
function updateButtons(){
  const scanning = Boolean(scanSession) || scanStarting;
  $('#enterScene').disabled=scanning||!scan?.scene_matches?.length;
  $('#bind').disabled=scanning||Boolean(activeBinding)||!scan?.matches?.length;
  $('#ocr').disabled=scanning||previewLive||!scan||!activeBinding||Boolean(jobTimer);
  $('#endBinding').hidden=!activeBinding; $('#endBinding').disabled=scanning;
  $('#continuousScan').disabled=scanning||state?.camera.mode!=='gwhp_main';
  $('#stopScan').hidden=!scanSession; $('#operator').disabled=scanning;
  for(const s of ['#neckCapture','#upload','#webcamOpen','#shutter', '[data-sample="A"]','[data-sample="B"]','[data-sample="Scene01"]']) $(s).disabled=scanning;
  $('#neckLive').disabled=scanning||state?.camera.mode!=='gwhp_main';
  for(const button of pendingButtons)button.disabled=true;
}

function closePreview(){
  previewLive=false; $('#neckPreview').removeAttribute('src'); $('#neckPreview').hidden=true;
  $('#liveBadge').hidden=true; $('#neckLive').textContent='打开实时视频';
  canvas.hidden=!picture; $('#empty').hidden=Boolean(picture);
  $('#sourceLabel').textContent=picture&&scan?`${scan.camera_id} · ${scan.width}×${scan.height}`:'等待拍摄';
  if(!picture)$('#cropHint').textContent='识别面板时，在照片上拖动框选显示区域。照片原件会保留。';
  updateButtons();
}
function openPreview(){
  stopWebcam(); previewLive=true; canvas.hidden=true; $('#empty').hidden=true;
  $('#neckPreview').src='/api/camera/preview.mjpg'; $('#neckPreview').hidden=false;
  $('#liveBadge').hidden=false; $('#neckLive').textContent='关闭实时视频';
  $('#sourceLabel').textContent=`${state.camera.id} · 实时视频`;
  $('#cropHint').textContent='对准面板后点击“拍摄面板 / 单张照片”，再在照片上框选读数。';
  updateButtons();
}
async function pollCamera(){
  try{
    const previousBinding=activeBinding?.binding_id;
    await refresh(false);
    if(previousBinding&&!activeBinding){
      $('#liveStatus').textContent='原绑定已结束。设备采集服务重新在线后，请重新连续扫码绑定。';
      message('原绑定已结束，请重新扫码绑定。');
    }
    const fresh=state.camera.status==='streaming'&&state.camera.frame_age_ms<=1000&&state.camera.service_status?.online!==false;
    $('#liveBadge').textContent=fresh?'实时视频':'等待相机新画面';
  }catch(e){$('#liveBadge').textContent='连接中断，等待恢复';}
  cameraTimer=setTimeout(pollCamera,2000);
}
$('#neckLive').onclick=()=>{if(previewLive){closePreview();draw();}else openPreview();};
$('#neckPreview').onerror=()=>{$('#liveBadge').textContent='视频连接中断，请关闭后重新打开';};

async function handleSession(result){
  $('#liveStatus').textContent=`${result.message}${result.frames_scanned ? ` · 已扫描 ${result.frames_scanned} 帧` : ''}`;
  if(['scanning','waiting_camera'].includes(result.status))return false;
  scanSession=null; clearTimeout(scanTimer); scanTimer=null;
  await refresh(false);
  if(result.scan)await showScan(result.scan,{keepLive:result.status==='bound'});
  activeBinding=state.bindings.find(b=>!b.ended_at&&b.camera_id===result.camera_id)||null;
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
function draw(){if(!picture)return;ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(picture,0,0);if(crop){ctx.fillStyle='#1e4a5222';ctx.fillRect(...crop);ctx.strokeStyle='#cf8337';ctx.lineWidth=Math.max(3,canvas.width/350);ctx.strokeRect(...crop);}$('#resetCrop').hidden=!crop;$('#cropHint').textContent=previewLive?'对准面板后点击“拍摄面板 / 单张照片”，再在照片上框选读数。':crop?`已选面板区域：${crop[2]} × ${crop[3]} 像素。识别结果保留对应原图位置。`:'拖动框选面板；未选框时会识别整张照片。';}
function point(e){const r=canvas.getBoundingClientRect();return [Math.max(0,Math.min(canvas.width,Math.round((e.clientX-r.left)*canvas.width/r.width))),Math.max(0,Math.min(canvas.height,Math.round((e.clientY-r.top)*canvas.height/r.height)))];}
canvas.onpointerdown=e=>{drag=point(e);canvas.setPointerCapture(e.pointerId);};
canvas.onpointermove=e=>{if(!drag)return;const p=point(e);crop=[Math.min(drag[0],p[0]),Math.min(drag[1],p[1]),Math.abs(p[0]-drag[0]),Math.abs(p[1]-drag[1])];draw();};
canvas.onpointerup=()=>{drag=null;if(crop&&(crop[2]<16||crop[3]<16))crop=null;draw();};
$('#resetCrop').onclick=()=>{crop=null;draw();};
function renderScan(){ $('#sceneResults').innerHTML=(scan.scene_matches||[]).map(s=>`<label class="match"><input type="radio" name="scene" value="${esc(s.id)}" ${(scan.scene_matches.length===1)?'checked':''}><strong>${esc(s.name)}</strong></label>`).join(''); $('#scanBadge').textContent=(scan.matches.length||scan.scene_matches?.length)?'已读出二维码':scan.status==='no_qr'?'二维码未能解码':'未登记';$('#scanResults').innerHTML=scan.matches.length?scan.matches.map((a,i)=>`<label class="match"><input type="radio" name="instrument" value="${esc(a.id)}" ${scan.matches.length===1?'checked':''}><div><strong>${esc(a.name)}</strong><small>${esc(a.scene||'尚未登记场景，请在下方填写')}</small><small>${esc(a.id)}</small></div></label>`).join(''):`<p>${esc(scan.unknown.join('；')||(scan.scene_matches?.length?'场景码已识别，请先确认进入场景。':'二维码未能解码。请让标签平整、正对镜头并避开反光；调整距离直到黑白格边缘清晰，再拍照。仅靠放大无法恢复失焦细节。若拍的是面板，可沿用已确认的仪器绑定进行 OCR。'))}</p>`; }
async function showScan(result,{keepLive=false}={}){if(!keepLive)closePreview();scan=result;crop=null;picture=new Image();await new Promise((resolve,reject)=>{picture.onload=resolve;picture.onerror=()=>reject(Error('图片预览失败'));picture.src=result.image_url;});canvas.width=picture.width;canvas.height=picture.height;canvas.hidden=previewLive;$('#empty').hidden=true;stopWebcam();$('#sourceLabel').textContent=`${result.camera_id} · ${result.width}×${result.height}`;renderScan();draw();activeBinding=state.bindings.find(b=>!b.ended_at&&b.camera_id===scan.camera_id)||null;renderBinding();updateButtons();}
async function upload(blob,source=activeBinding?.camera_id||'UploadedPhoto'){const form=new FormData();form.append('file',blob,'Capture.png');form.append('camera_id',source);await showScan(await api('/api/scans',{method:'POST',body:form}));}
$('#upload').onchange=async e=>{const file=e.target.files[0];if(!file)return;try{message('正在解码图片与二维码…');await upload(file);message('图片已留存，请核对身份或框选面板。');}catch(err){message(err.message,true);}e.target.value='';};
document.querySelectorAll('[data-sample]').forEach(b=>b.onclick=busy(b,async()=>{const r=await fetch(`/static/labels/${b.dataset.sample==='Scene01'?'Scene01':'Instrument'+b.dataset.sample}.png`);await upload(await r.blob(),'DemoSampleCamera');message('这是二维码样张解码结果；可登记场景并演示绑定。');}));
$('#neckCapture').onclick=busy($('#neckCapture'),async()=>{await showScan(await api('/api/camera/capture',{method:'POST'}));message('已取得挂脖设备照片。');});
function stopWebcam(){if(stream)stream.getTracks().forEach(t=>t.stop());stream=null;$('#webcam').hidden=true;$('#shutter').hidden=true;}
$('#webcamOpen').onclick=busy($('#webcamOpen'),async()=>{closePreview();if(!navigator.mediaDevices?.getUserMedia)throw Error('本机摄像头需要 localhost 或 HTTPS；挂脖设备取图和上传不受此限制。');stopWebcam();stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'},audio:false});$('#webcam').srcObject=stream;$('#webcam').hidden=false;$('#shutter').hidden=false;canvas.hidden=true;$('#empty').hidden=true;});
$('#shutter').onclick=busy($('#shutter'),async()=>{const v=$('#webcam'),c=document.createElement('canvas');c.width=v.videoWidth;c.height=v.videoHeight;if(!c.width)throw Error('摄像头尚未就绪');c.getContext('2d').drawImage(v,0,0);await upload(await new Promise(r=>c.toBlob(r,'image/png')),'BrowserCamera');});
function renderBinding(){ const visit=state?.scene_visits?.find(v=>v.camera_id===(previewLive?state.camera.id:scan?.camera_id||state.camera.id));$('#sceneInfo').textContent=visit?`当前场景：${visit.scene.name} · ${visit.camera_id}`:'连续扫码时先对准场景码，再对准仪器码'; $('#bindingInfo').innerHTML=activeBinding?`<b>当前：${esc(activeBinding.instrument.name)}</b><br>${esc(activeBinding.instrument.scene)} · ${esc(activeBinding.operator)}<br>${esc(activeBinding.camera_id)}<br>开始于 ${esc(stamp(activeBinding.started_at))}`:'当前没有有效绑定';updateButtons();}
$('#bind').onclick=busy($('#bind'),async()=>{const selected=$('input[name="instrument"]:checked');if(!selected)throw Error('请选择本次使用的仪器');const operator=$('#operator').value.trim();if(!operator)throw Error('请填写实验员姓名或编号');activeBinding=await api('/api/bindings',json({scan_id:scan.scan_id,instrument_id:selected.value,operator}));localStorage.setItem('fieldOperator',operator);await refresh(false);renderBinding();message('绑定已保存。现在可拍摄面板并框选识别区域。');});
$('#endBinding').onclick=busy($('#endBinding'),async()=>{await api(`/api/bindings/${activeBinding.binding_id}/end`,{method:'POST'});activeBinding=null;renderBinding();await refresh(false);message('本次绑定已结束。');});
function renderOcr(job){
  const target=$('#ocrResults');
  if(['queued','running'].includes(job.status)){
    const phase={local_ocr:'正在识别面板',waiting_readout:'暂未读到数字，继续等待识别结果',extended_reading:'正在进一步识别面板'}[job.phase]||'任务已入队';
    target.innerHTML=`<p>◌ ${phase}…</p><p class="caption">正在处理这次拍下的照片，请稍候。</p>`;return;
  }
  if(job.status!=='completed'){target.textContent=job.status==='cancelled'?'绑定已结束或发生变化，这次识别已取消。':`识别未完成，请稍后重新拍摄。`;return;}
  const lines=job.lines||[];
  target.innerHTML=`<p class="caption">${esc(job.instrument.name)} · ${esc(stamp(job.external_photo?.captured_at||job.submitted_at))}<br>照片识别结果，尚未人工核对</p>${lines.length?lines.map(line=>`<div class="ocr-line"><strong>${esc(line.text)}</strong></div>`).join(''):'<p>这次照片未读出数字，请调整角度、清晰度或选框后重新拍摄。</p>'}${job.outcome==='no_numeric_readout'&&lines.length?'<p class="caption">识别到了文字，但还没有完整的数字读数。</p>':''}<p><a href="${job.crop_image_url}" target="_blank" rel="noopener">查看面板照片 ↗</a> · <a href="/api/jobs/${job.job_id}" target="_blank" rel="noopener">结果 JSON ↗</a></p>`;
}
function watchJob(job){
  renderOcr(job);clearInterval(jobTimer);
  jobTimer=setInterval(async()=>{try{
    const result=await api(`/api/jobs/${job.job_id}`);
    if(activeBinding?.binding_id===result.binding_id)renderOcr(result);
    if(!['queued','running'].includes(result.status)){clearInterval(jobTimer);jobTimer=null;updateButtons();await refresh(false);}
  }catch(e){clearInterval(jobTimer);jobTimer=null;message(e.message,true);updateButtons();}},1000);
  updateButtons();
}
$('#ocr').onclick=busy($('#ocr'),async()=>{watchJob(await api('/api/ocr',json({binding_id:activeBinding.binding_id,capture_id:scan.capture_id,crop})));});
function renderLatestJob(){
  if(jobTimer)return;
  const latest=state.jobs.find(j=>j.binding_id===activeBinding?.binding_id);
  const key=latest?`${latest.job_id}:${latest.status}:${latest.phase}:${latest.finished_at}`:null;
  if(key!==displayedJobKey){
    displayedJobKey=key;
    if(latest)renderOcr(latest);else $('#ocrResults').textContent='让 Agent 把已保存的拍照结果交给识别工具，或上传照片后点击识别。';
  }
}
function renderPhotoWatch(){
  const watch=state.photo_watch;
  if(!watch){$('#photoWatchState').textContent='';return;}
  const labels={disabled:'照片监控未开启',waiting_binding:'绑定后开始等待这台相机的语音照片',watching:'正在等待新的语音拍照文件',waiting_photo:'等待这台相机的第一张语音照片',waiting_queue:'已有照片正在识别，稍后处理新照片',storage_unavailable:'拍照存储暂不可用，正在等待恢复',storage_unconfigured:'尚未配置拍照存储',watch_error:'照片监控暂不可用',invalid_camera_directory:'相机照片目录不可用'};
  $('#photoWatchState').textContent=watch.last_photo_status==='rejected'?`照片未能处理：${watch.detail}`:labels[watch.status]||'等待语音拍照';
}
function forms(){ $('#instrumentForms').innerHTML=state.instruments.map((a,i)=>`<form class="instrument-form" data-id="${esc(a.id)}"><strong>${esc(a.name)}</strong><div class="id">${esc(a.id)}</div><div class="row"><label class="field">仪器名称<input name="name" value="${esc(a.name)}" required maxlength="80"></label><label class="field">所属场景 / 实验区域<input name="scene" value="${esc(a.scene)}" placeholder="例如：A 实验室 / 离心区" required maxlength="100"></label></div><label class="field">型号（可选）<input name="model" value="${esc(a.model)}" maxlength="100"></label><button class="secondary" type="submit">保存登记</button> <a href="/static/labels/Instrument${a.id==='e9434a0a-3319-414a-b988-4cc6884edce4'?'A':'B'}.png" target="_blank" rel="noopener">查看标签 ↗</a></form>`).join('');document.querySelectorAll('.instrument-form').forEach(form=>form.onsubmit=async e=>{e.preventDefault();const button=form.querySelector('button');button.disabled=true;try{const values=Object.fromEntries(new FormData(form));await api(`/api/instruments/${form.dataset.id}`,{...json(values),method:'PUT'});await refresh(false);if(scan){scan.matches=scan.matches.map(m=>({...m,...state.instruments.find(a=>a.id===m.id)}));renderScan();}message('仪器与场景登记已保存。');}catch(err){message(err.message,true);}finally{button.disabled=false;}}); }
async function refresh(renderForms=true){state=await api('/api/state');$('#cameraState').className='pill'+(state.camera.status==='streaming'?'':' warning');$('#cameraState').textContent=state.camera.configured?`${state.camera.id} · ${{streaming:'主码流在线',discovering:'正在发现',reconnecting:'接收端连接失败',camera_offline:'设备离线',waiting_keyframe:'等待关键帧',not_started:'正在连接'}[state.camera.status]||'已配置取图'}`:`${state.camera.id} · 等待连接`;$('#ocrState').textContent=`PaddleOCR · CPU · ${{not_loaded:'绑定后自动加载',queued:'等待后台加载',loading:'模型加载中',ready:'模型常驻 · 已就绪',error:'加载/识别失败'}[state.ocr.status]||state.ocr.status}`;activeBinding=state.bindings.find(b=>!b.ended_at&&b.camera_id===(previewLive?state.camera.id:scan?.camera_id||state.camera.id))||null;renderBinding();renderLatestJob();renderPhotoWatch();if(renderForms)forms();$('#historyRows').innerHTML=state.bindings.length?`<div style="overflow:auto"><table><thead><tr><th>仪器 / 场景</th><th>实验员 / 相机</th><th>开始时间</th><th>状态</th><th>证据</th></tr></thead><tbody>${state.bindings.map(b=>`<tr><td>${esc(b.instrument.name)}<br><small>${esc(b.instrument.scene)}</small></td><td>${esc(b.operator)}<br><small>${esc(b.camera_id)}</small></td><td>${esc(stamp(b.started_at))}</td><td>${b.ended_at?'已结束':'使用中'}</td><td><a href="${b.image_url}" target="_blank" rel="noopener">扫码原图</a></td></tr>`).join('')}</tbody></table></div>`:'尚无绑定记录';}
$('#refresh').onclick=busy($('#refresh'),()=>refresh());
$('#export').onclick=busy($('#export'),async()=>{const data=await api('/api/export');const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='FieldRecognitionSession.json';a.click();URL.revokeObjectURL(url);});
$('#operator').value=localStorage.getItem('fieldOperator')||'';
refresh().then(()=>{renderBinding();pollCamera();}).catch(e=>{message(e.message,true);cameraTimer=setTimeout(pollCamera,2000);});
window.addEventListener('pagehide',()=>{stopWebcam();closePreview();clearTimeout(cameraTimer);clearTimeout(scanTimer);if(scanSession)fetch(`/api/camera/scan-sessions/${scanSession}`,{method:'DELETE',keepalive:true}).catch(()=>{});});

$('#enterScene').onclick=busy($('#enterScene'),async()=>{const chosen=$('input[name="scene"]:checked');if(!chosen)throw Error('请选择场景');await api('/api/scene/enter',json({scan_id:scan.scan_id,scene_id:chosen.value}));await refresh(false);activeBinding=state.bindings.find(b=>!b.ended_at&&b.camera_id===scan.camera_id)||null;renderBinding();message('已进入场景，请拍摄仪器二维码并绑定使用。');});
