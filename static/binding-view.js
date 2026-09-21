/* Shared scene receipts and instrument status for each camera. */
(function (root) {
  const esc = value => String(value ?? '').replace(/[&<>"']/g,
    char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  const stamp = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false, timeZone:'Asia/Shanghai'}) : '未记录';
  const active = (item, now) => !item.ended_at && (!item.valid_until || Date.parse(item.valid_until) > now);

  function current(data, cameraId, now = Date.now()) {
    return {
      scenes: (data?.scene_visits || []).filter(item => item.camera_id === cameraId && active(item, now)),
      instruments: (data?.bindings || []).filter(item => item.camera_id === cameraId && active(item, now)),
    };
  }

  function evidence(item) {
    return /^\/api\/images\/[a-zA-Z0-9-]+$/.test(item.image_url || '')
      ? `<a href="${esc(item.image_url)}" target="_blank" rel="noopener">扫码凭证 ↗</a>` : '';
  }

  function scenesMarkup(visits) {
    if (!visits.length) return '<p class="caption">尚未绑定场景，可扫描场景二维码。</p>';
    return visits.map(visit => `<div class="bound-item" data-scene-visit="${esc(visit.visit_id)}">
      <b>场景已绑定 · ${esc(visit.scene?.name || '未命名场景')}</b>
      <small>实验员 · ${esc(visit.operator || '当时未记录')}</small>
      <small>绑定时间 · ${esc(stamp(visit.started_at))}（北京时间）</small>
      ${visit.valid_until ? `<small>有效至 · ${esc(stamp(visit.valid_until))}（北京时间）</small>` : ''}
      <small>场景编号 · ${esc(visit.scene?.id)}</small>${evidence(visit)}
    </div>`).join('');
  }

  function pendingInstruments(data, cameraId, now = Date.now()) {
    const scan = data?.automation?.session?.scan || data?.last_camera_scan;
    if (!scan || scan.camera_id !== cameraId) return '';
    // Daily authorization requires a scan from the current Beijing date.
    const scanTime = Date.parse(scan.received_at);
    const beijingDay = time => Math.floor((time + 8 * 3600000) / 86400000);
    if (!Number.isFinite(scanTime) || beijingDay(scanTime) !== beijingDay(now)) return '';
    const bindings = (data.bindings || []).filter(item => active(item, now));
    return (scan.matches || []).filter(match => !bindings.some(item =>
      item.camera_id === cameraId && item.instrument?.id === match.id) && !(data.bindings || []).some(item =>
      item.camera_id === cameraId && item.instrument?.id === match.id && item.ended_at &&
      Date.parse(item.ended_at) >= scanTime)).map(match => {
      const occupied = bindings.find(item => item.camera_id !== cameraId && item.instrument?.id === match.id);
      const reason = occupied
        ? `当前由 ${occupied.operator || '未记录人员'} · ${occupied.camera_id} 使用；该相机关机、采集服务异常或北京时间跨日后自动解除，再扫码即可绑定。`
        : '当前尚未绑定，请重新对准仪器码并查看自动运行提示。';
      return `<div class="bound-item"><b>仪器未绑定 · ${esc(match.name || match.id)}</b>
        <small>最近解码 · ${esc(stamp(scan.received_at))}（北京时间）</small><small>${esc(reason)}</small></div>`;
    }).join('');
  }

  const api = {current, scenesMarkup, pendingInstruments};
  root.BindingView = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
