'use strict';
const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const beijing = value => value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',fractionalSecondDigits:3}) : '采集时间未知';
const localInput = date => new Date(date.getTime()+8*3600000).toISOString().slice(0,19);
const labels = {synchronized_frame:'同步视频帧时间',filename_time:'源文件名时间，未校验硬件时钟',source_declared:'来源回执声明的时间',unknown:'缺少可用采集时间'};
let currentPage = null, offset = 0, requestId = 0, loading = false;
function buttons(){
  $('search').disabled=loading;
  $('previous').disabled=loading||offset===0;
  $('next').disabled=loading||currentPage?.next_offset==null;
  $('export').disabled=loading||!currentPage;
}
async function jsonResponse(url){
  const response=await fetch(url,{cache:'no-store'});
  const body=await response.json();
  if(!response.ok)throw Error(typeof body.detail==='string'?body.detail:body.error?.message||'查询暂不可用，请稍后重试');
  return body;
}
function sourceCard(source){
  const time=source.acquisition.time;
  const readings=source.measurements.flatMap(m=>m.record.values.map(v=>
    `<div class="reading">${escapeHtml(v.name)}：${escapeHtml(v.display_state??v.value??'未读清')} ${escapeHtml(v.unit||'')}</div><small>${escapeHtml(m.instrument_name||m.record.device_model||m.instrument_id)}</small>`)).join('');
  // Only generated same-origin image/API links are navigable; source_ref is text.
  const image=/^\/api\/images\/[A-Za-z0-9_-]+$/.test(source.image_url||'')?source.image_url:null;
  const job=encodeURIComponent(source.job_id);
  return `<article class="source">${image?`<a href="${escapeHtml(image)}" target="_blank" rel="noopener"><img loading="lazy" src="${escapeHtml(image)}" alt="本次观测保存的画面"></a>`:''}
    <strong>${escapeHtml(beijing(time.captured_at))}</strong><small class="muted">${escapeHtml(labels[time.quality]||time.quality)}</small>
    ${readings||'<p class="warning">暂无可归属的规范读数，画面与处理回执仍可追溯。</p>'}
    <small>当时人员：${escapeHtml(source.operator||source.wearer_id||'未记录')}</small>
    <small>归档：${source.archive.status==='archived'?'已记录归档回执':'等待归档'}</small>
    <p>${source.original_url?`<a href="/api/captures/${encodeURIComponent(source.capture_id)}/original" target="_blank" rel="noopener">原始图像 ↗</a> · `:''}<a href="/api/jobs/${job}" target="_blank" rel="noopener">识别与绑定回执 ↗</a> · <a href="/api/jobs/${job}/versions" target="_blank" rel="noopener">识别版本 ↗</a></p>
    <details><summary>图片哈希和来源</summary><pre>${escapeHtml(JSON.stringify({capture_id:source.capture_id,image_sha256:source.image_sha256,source_ref:source.source_ref,source_sha256:source.source_sha256,frame_metadata:source.frame_metadata},null,2))}</pre></details></article>`;
}
function render(page){
  $('events').innerHTML=page.items.map(event=>`<article class="event"><header><strong>${escapeHtml(beijing(event.start_at))}</strong><span class="tag">${event.stream==='video'?'视频自动留证':'语音拍照'}</span></header>
    ${event.stream==='voice'?`<p class="${event.batch_status==='complete'?'muted':'warning'}">${event.expected_photos?`本次连拍已接收 ${event.received_photos}/${event.expected_photos} 张`:'批次关联未确认，保留逐张证据'}${event.record_scope==='production_confirmed'?' · 已正式确认':' · 现场采集证据'}</p>`:'<p class="muted">自动保存的现场观测；无需触发语音拍照。</p>'}
    <div class="sources">${event.sources.map(sourceCard).join('')}</div></article>`).join('');
  $('status').className='muted';
  $('status').textContent=`共 ${page.total} 个事件，当前 ${page.items.length} 个，按采集时间排序。${page.unknown_time_events?`另有 ${page.unknown_time_events} 个历史事件缺少采集时间，未放入此区间。`:''}`;
}
async function load(){
  const id=++requestId;loading=true;buttons();$('status').textContent='正在读取证据…';
  try{
    if(!$('camera').value||!$('start').value||!$('end').value)throw Error('请选择相机和完整时间范围');
    const params=new URLSearchParams({camera_id:$('camera').value,start:$('start').value+'+08:00',end:$('end').value+'+08:00',stream:$('stream').value,limit:'20',offset:String(offset)});
    const page=await jsonResponse('/api/evidence-timeline?'+params);
    if(id!==requestId)return;
    currentPage=page;render(page);
  }catch(error){
    if(id!==requestId)return;
    currentPage=null;$('events').replaceChildren();$('status').className='error';$('status').textContent=error.message;
  }finally{if(id===requestId){loading=false;buttons();}}
}
$('query').onsubmit=event=>{event.preventDefault();offset=0;load();};
$('previous').onclick=()=>{offset=Math.max(0,offset-20);load();};
$('next').onclick=()=>{offset=currentPage.next_offset;load();};
for(const id of ['camera','start','end','stream'])$(id).onchange=()=>{requestId++;offset=0;currentPage=null;loading=false;$('events').replaceChildren();$('status').textContent='筛选条件已变化，请重新查询。';buttons();};
$('export').onclick=()=>{
  if(!currentPage)return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(currentPage,null,2)],{type:'application/json'}));
  const a=document.createElement('a');a.href=url;a.download='FieldEvidenceTimeline.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
const now=new Date();$('end').value=localInput(now);$('start').value=localInput(new Date(now.getTime()-3600000));
(async()=>{
  try{
    const response=await jsonResponse('/api/cameras');
    const cameras=response.cameras||response.items||[];
    $('camera').replaceChildren(...cameras.map(c=>new Option(c.display_name||c.camera_id,c.camera_id)));
    const selected=localStorage.getItem('fieldCameraId');if(cameras.some(c=>c.camera_id===selected))$('camera').value=selected;
    if(!cameras.length)$('status').textContent='暂无登记相机。';
  }catch(error){$('status').className='error';$('status').textContent=error.message;}
})();
