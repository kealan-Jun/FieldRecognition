/* Pure selection also used by regression tests. No network requests are needed. */
function selectArchive(items, filters) {
  let values=items;
  if(filters.versions!=='all'){
    const latest=new Map();
    for(const item of items){const key=JSON.stringify([item.entity,item.entity_id]);if(!latest.has(key)||latest.get(key).sequence<item.sequence)latest.set(key,item);}
    values=[...latest.values()];
  }
  return values.filter(i=>(!filters.operator||(filters.operator==='__missing__'?!i.operator:i.operator===filters.operator))
    &&(!filters.instrument||(filters.instrument==='__missing__'?!i.instruments.length:i.instruments.some(a=>a.id===filters.instrument)))
    &&(!filters.workbench||i.workbench?.name===filters.workbench||(i.workbenches||[]).some(b=>b.name===filters.workbench))
    &&(!filters.from||i.day>=filters.from)&&(!filters.to||i.day<=filters.to)
    &&(!filters.camera||i.camera_id===filters.camera)&&(!filters.entity||i.entity===filters.entity))
    .sort((a,b)=>Date.parse(b.occurred_at)-Date.parse(a.occurred_at)||b.sequence-a.sequence);
}
if(typeof module!=='undefined'&&module.exports)module.exports={selectArchive};
if(typeof document!=='undefined'){
  const index=JSON.parse(document.getElementById('archiveData').textContent),$=id=>document.getElementById(id);
  const labels={jobs:'面板读数',scans:'扫码 / 照片',bindings:'仪器绑定',scene_visits:'场景关系',automation_settings:'实验员登记 / 运行设置'};
  const displayOperator=s=>s==='Demo验证'?'样张验证':s;
  const displayCamera=s=>s==='DemoSampleCamera'||s==='SampleCamera'?'样张相机':s;
  const states={no_qr:'未解出二维码',matched:'二维码已识别',not_registered:'二维码未登记',queued:'排队中',running:'识别中',completed:'处理完成',failed:'处理失败',interrupted:'已中断',cancelled:'已取消'};
  const fmt=t=>t?new Date(t).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const archivePath=path=>typeof path==='string'&&/^(Receipts|Objects|Integrity|\.System|\d{4}-\d{2}-\d{2}_[A-Za-z0-9_-]+)\/[a-zA-Z0-9_./-]+$/.test(path)&&!path.split('/').includes('..')?(index.archive_base==='../'?'../':'')+path:null;
  const safeLink=(path,label)=>archivePath(path)?`<a href="${esc(archivePath(path))}" target="_blank" rel="noopener">${label} ↗</a>`:'';
  function option(id,value,label){const el=document.createElement('option');el.value=value;el.textContent=label;$(id).append(el);}
  for(const name of [...new Set(index.items.map(i=>i.operator).filter(Boolean))].sort())option('operator',name,displayOperator(name));
  option('operator','__missing__','当时未记录');
  const instruments=new Map();for(const i of index.items)for(const a of i.instruments)if(!instruments.has(a.id))instruments.set(a.id,a.name);
  for(const [id,name] of instruments)option('instrument',id,name);
  option('instrument','__missing__','未关联仪器');
  for(const name of [...new Set(index.items.flatMap(i=>[i.workbench?.name,...(i.workbenches||[]).map(b=>b.name)]).filter(Boolean))].sort())option('workbench',name,name);
  for(const camera of [...new Set(index.items.map(i=>i.camera_id).filter(Boolean))].sort())option('camera',camera,displayCamera(camera));
  $('overview').textContent=`${index.unique_records} 条业务记录 · ${index.listed_receipts} 个留存版本 · 更新于 ${fmt(index.updated_at)}`;
  const integrity=index.integrity||{},report=integrity.last_report;
  $('checkSummary').textContent=report?`${{completed:'校验通过',findings:'发现异常',unavailable:'未完成：存储暂不可用',interrupted:'巡检被中断'}[report.status]||report.status} · ${fmt(report.finished_at)} · 回执 ${report.verified_receipts}/${report.expected_receipts} · 图片 ${report.verified_objects} · 异常 ${report.issue_count}`:'等待首次巡检';
  $('integrity').classList.toggle('warn',!!report&&report.status!=='completed');
  $('checkSchedule').textContent=`每 ${Math.round((integrity.interval_seconds||86400)/3600)} 小时检查一次${integrity.next_check_at?' · 下次计划 '+fmt(integrity.next_check_at):' · 本机服务运行后自动开始'}`;
  if(archivePath(integrity.report_path)){$('checkReport').hidden=false;$('checkReport').href=archivePath(integrity.report_path);}
  if(integrity.issues?.length){
    $('checkIssues').hidden=false;
    const codes={missing:'文件缺失',sha256_mismatch:'SHA-256 不一致',size_mismatch:'文件大小不一致',snapshot_mismatch:'回执与留存记录不一致',changed_during_check:'校验期间文件变化',conflicting_object_metadata:'图片元数据冲突',invalid_or_unreadable:'文件无效或无法读取'};
    $('issueList').innerHTML=integrity.issues.map(i=>`<li>${esc(codes[i.code]||i.code)} · ${esc(i.path)}${i.sequence?' · 版本 '+i.sequence:''}</li>`).join('');
  }
  let page=0,selected=[],filters={};const pageSize=50;
  function render(){
    filters=Object.fromEntries(['operator','instrument','workbench','from','to','camera','entity','versions'].map(id=>[id,$(id).value]));
    const invalid=filters.from&&filters.to&&filters.from>filters.to;
    $('filterError').hidden=!invalid;selected=invalid?[]:selectArchive(index.items,filters);
    const pages=Math.max(1,Math.ceil(selected.length/pageSize));page=Math.min(page,pages-1);
    $('resultCount').textContent=`找到 ${selected.length} ${filters.versions==='all'?'个版本':'条记录'}`;
    $('page').textContent=`${page+1} / ${pages}`;$('previous').disabled=page===0;$('next').disabled=page+1>=pages;$('export').disabled=!selected.length;
    $('empty').hidden=!!selected.length;
    $('rows').innerHTML=selected.slice(page*pageSize,(page+1)*pageSize).map(i=>`<tr><td>${esc(i.local_time)}<small>${esc(labels[i.entity]||i.entity)}</small></td><td>${esc(displayOperator(i.operator)||'当时未记录')}<small>${esc(displayCamera(i.camera_id))}</small></td><td>${esc(i.target||'未关联仪器 / 场景')}</td><td>${esc(states[i.status]||i.status)}<small>${esc(i.readings)}</small></td><td>${esc(fmt(i.archived_at))}<small>版本序号 ${i.sequence}</small></td><td>${safeLink(i.receipt,'完整回执')}${safeLink(i.image,'照片')}</td></tr>`).join('');
  }
  $('filters').onsubmit=e=>e.preventDefault();$('filters').onchange=()=>{page=0;render();};
  $('filters').onreset=()=>setTimeout(()=>{page=0;render();},0);
  $('previous').onclick=()=>{page--;render();};$('next').onclick=()=>{page++;render();};$('reload').onclick=()=>location.reload();
  $('export').onclick=()=>{const blob=new Blob([JSON.stringify({schema:'field-recognition-filtered-index/1',source_instance:index.source_instance,index_updated_at:index.updated_at,timezone:index.timezone,filters,items:selected},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='FieldRecognition-Selected.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  render();
}
