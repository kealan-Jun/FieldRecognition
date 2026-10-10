/* Pure display conventions shared by online pages and standalone archives. */
(function (root) {
  "use strict";
  const timeZone = "Asia/Shanghai";
  const entities = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  };
  const escapeHtml = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (char) => entities[char]);
  const displayCamera = (value) =>
    ["DemoSampleCamera", "SampleCamera"].includes(value) ? "样张相机" : value;
  const displayOperator = (value) =>
    value === "Demo验证" ? "样张验证" : value;
  function timestamp(value, options = {}) {
    const { missing = "—", milliseconds = false, timeOnly = false } = options;
    if (!value) return missing;
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return missing;
    const format = {
      timeZone,
      hourCycle: "h23",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    };
    if (!timeOnly)
      Object.assign(format, {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      });
    if (milliseconds) format.fractionalSecondDigits = 3;
    return new Intl.DateTimeFormat("zh-CN", format).format(date);
  }
  function beijingInput(value) {
    const date = new Date(value);
    return Number.isFinite(date.getTime())
      ? new Date(date.getTime() + 8 * 3600000).toISOString().slice(0, 19)
      : "";
  }
  const labels = Object.freeze({
    camera: Object.freeze({
      streaming: "主码流在线",
      waiting_frame: "画面暂未更新，等待新画面",
      service_unavailable: "采集服务中断，等待恢复",
      discovering: "正在发现相机",
      reconnecting: "接收端连接失败",
      camera_offline: "相机离线",
      waiting_keyframe: "等待关键帧",
      not_started: "正在连接",
      worker_unavailable: "后台服务恢复中",
    }),
    automation: Object.freeze({
      starting: "正在启动",
      paused: "自动扫码已暂停",
      waiting_operator: "等待实验员登记",
      waiting_config: "等待相机配置",
      waiting_camera: "等待相机新画面",
      scanning: "后台自动扫码中",
      bound: "已绑定 · 等待语音照片",
      manual_scan: "正在手动扫码",
      retrying: "正在恢复连接",
    }),
    video: Object.freeze({
      paused: "视频识别已暂停",
      unconfigured: "等待配置相机",
      waiting_operator: "等待实验员登记",
      waiting_binding: "等待扫码绑定设备，暂不识别面板",
      waiting_camera: "等待新鲜画面",
      loading_model: "正在加载模型",
      model_error: "模型加载失败，后台将自动重试",
      inferring: "视频持续识别中",
      watching: "视频持续识别中",
      yielding_to_photo: "优先处理语音照片",
      error: "视频识别异常",
    }),
    photo: Object.freeze({
      disabled: "照片监控未开启",
      waiting_binding: "正在准备照片监控",
      watching: "正在等待新的语音拍照文件",
      waiting_photo: "等待这台相机的第一张语音照片",
      waiting_queue: "已有照片正在识别，稍后处理新照片",
      storage_unavailable: "拍照存储暂不可用，正在等待恢复",
      storage_unconfigured: "尚未配置拍照存储",
      watch_error: "照片监控暂不可用",
      invalid_camera_directory: "相机照片目录不可用",
    }),
    ocr: Object.freeze({
      not_loaded: "等待加载",
      queued: "等待后台加载",
      loading: "模型加载中",
      ready: "模型常驻 · 已就绪",
      error: "加载/识别失败",
      worker_unavailable: "后台服务恢复中",
      waiting_service: "等待推理服务恢复 · 照片已排队",
    }),
    job: Object.freeze({
      no_qr: "未解出二维码",
      matched: "二维码已识别",
      not_registered: "二维码未登记",
      queued: "排队中",
      running: "识别中",
      completed: "处理完成",
      failed: "处理失败",
      interrupted: "已中断",
      cancelled: "已取消",
    }),
  });
  function cameraStatus(camera, options = {}) {
    if (camera?.service_status?.online === false)
      return labels.camera.service_unavailable;
    const status = camera?.status || "unknown";
    return (
      options[status] ||
      labels.camera[status] ||
      (status === "unknown" ? "状态未知" : status)
    );
  }
  const api = {
    escapeHtml,
    timestamp,
    beijingInput,
    displayCamera,
    displayOperator,
    cameraStatus,
    labels,
    timeZone,
  };
  root.ViewUtils = api;
  if (typeof module !== "undefined") module.exports = api;
})(globalThis);
