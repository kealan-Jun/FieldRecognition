/* Parallel camera overview. Each card owns its state request and preview. */
(function () {
  const pageContext = globalThis.FieldWorkbench;
  const utils = globalThis.ViewUtils;
  const { escapeHtml: esc, timestamp: stamp } = utils;
  const automationLabels = {
    ...utils.labels.automation,
    bound: "已绑定，等待产出",
  };
  const overview = { timer: null, running: false, request: 0 };

  const overviewStatus = utils.cameraStatus;

  function connectionClass(camera) {
    return camera?.status === "streaming" &&
      camera?.service_status?.online !== false
      ? ""
      : " warning";
  }

  function latestOutput(data, cameraId) {
    const job = pageContext.ReadoutView.selectLatest(data, cameraId);
    if (!job) return '<p class="caption">暂无照片或 OCR 产出</p>';
    const fields = job.display_fields?.length
      ? job.display_fields
      : job.readings?.length
        ? job.readings
        : job.lines || [];
    const lines = fields
      .slice(0, 4)
      .map((line) => {
        const text =
          pageContext.ReadoutView.lineText(line) ||
          line.text ||
          line.value ||
          "未取得有效数值";
        return `<li><strong>${esc(text)}</strong><small>${esc(line.instrument_name || line.instrument?.name || line.name || "面板读数")}</small></li>`;
      })
      .join("");
    const status =
      job.status === "completed"
        ? "已完成"
        : job.status === "running"
          ? "识别中"
          : job.status === "queued"
            ? "排队中"
            : "未完成";
    const captured =
      pageContext.ReadoutView.capturedAt(job) || job.submitted_at;
    return `<p class="caption">${esc(status)} · ${esc(stamp(captured))}</p>${lines ? `<ul class="camera-events">${lines}</ul>` : '<p class="caption">已收到照片，暂未取得规范读数</p>'}<a class="caption" href="#workspace" data-camera-detail="${esc(cameraId)}">进入相机详情 ↗</a>`;
  }

  function cardMarkup(camera) {
    const id = esc(camera.camera_id);
    return `<article class="card camera-card" data-camera-card="${id}">
      <div class="card-title"><div><h2>${esc(camera.display_name || camera.camera_id)}</h2><span class="small">${id}</span></div><span class="pill camera-connection" data-field="connection"></span></div>
      <img class="camera-card-preview" data-field="preview" src="/api/camera/preview.mjpg?camera_id=${encodeURIComponent(camera.camera_id)}" alt="${esc(camera.display_name || camera.camera_id)}实时画面" loading="lazy">
      <section class="camera-result"><h3>当前使用</h3><div data-field="operator" class="caption">正在读取</div>
        <h3>场景绑定</h3><div data-field="scenes" class="bound-list"><p class="caption">正在读取场景绑定</p></div>
        <h3>仪器绑定</h3><div data-field="bindings" class="bound-list"><p class="caption">正在读取仪器绑定</p></div>
        <h3>自动运行</h3><div data-field="automation" class="caption">正在读取</div>
        <h3>最近产出</h3><div data-field="output" class="camera-output"><p class="caption">正在读取 OCR 和照片结果</p></div>
      </section>
    </article>`;
  }

  function renderCards(cameras) {
    const host = document.querySelector("#cameraCards");
    if (!host) return;
    const ids = new Set(cameras.map((camera) => camera.camera_id));
    for (const card of [...host.querySelectorAll("[data-camera-card]")]) {
      if (!ids.has(card.dataset.cameraCard)) card.remove();
    }
    for (const camera of cameras) {
      if (
        !host.querySelector(
          `[data-camera-card="${CSS.escape(camera.camera_id)}"]`,
        )
      ) {
        host.insertAdjacentHTML("beforeend", cardMarkup(camera));
      }
    }
  }

  function updateCard(camera, data, error) {
    const card = document.querySelector(
      `[data-camera-card="${CSS.escape(camera.camera_id)}"]`,
    );
    if (!card) return;
    const connection = card.querySelector('[data-field="connection"]');
    const cameraState = data?.camera || {
      status: "worker_unavailable",
      service_status: { online: false },
    };
    connection.className = `pill camera-connection${connectionClass(cameraState)}`;
    connection.textContent = error
      ? "状态暂不可用"
      : overviewStatus(cameraState);
    card.dataset.stale = error ? "true" : "false";
    const preview = card.querySelector('[data-field="preview"]');
    preview.title = error
      ? "正在等待相机状态恢复"
      : overviewStatus(cameraState);
    if (error) {
      card.querySelector('[data-field="operator"]').textContent =
        "相机状态暂不可用，后台仍会继续重试";
      card.querySelector('[data-field="scenes"]').innerHTML =
        '<p class="caption">等待相机服务恢复后读取场景绑定</p>';
      card.querySelector('[data-field="bindings"]').innerHTML =
        '<p class="caption">等待相机服务恢复后读取绑定</p>';
      card.querySelector('[data-field="automation"]').textContent =
        "等待后台服务恢复";
      card.querySelector('[data-field="output"]').innerHTML =
        '<p class="caption">暂时无法读取产出，历史数据保留</p>';
      return;
    }
    const automation = data.automation || {};
    card.querySelector('[data-field="operator"]').textContent =
      automation.operator
        ? `实验员 · ${automation.operator}`
        : "尚未登记实验员";
    const { scenes, instruments: bindings } = pageContext.BindingView.current(
      data,
      camera.camera_id,
    );
    card.querySelector('[data-field="scenes"]').innerHTML =
      pageContext.BindingView.scenesMarkup(scenes);
    const pending = pageContext.BindingView.pendingInstruments(
      data,
      camera.camera_id,
    );
    card.querySelector('[data-field="bindings"]').innerHTML =
      (bindings.length
        ? bindings
            .map(
              (binding) =>
                `<div class="bound-item"><b>仪器已绑定 · ${esc(binding.instrument?.name || "未命名设备")}</b><small>${esc(binding.instrument?.device_category || "instrument")} · ${esc(binding.instrument?.scene || "未分类场景")}</small><small>开始 ${esc(stamp(binding.started_at))}</small></div>`,
            )
            .join("")
        : pending
          ? ""
          : `<p class="caption">${scenes.length ? "场景绑定已完成；尚未绑定仪器，可继续扫描仪器码。" : "尚未绑定仪器，可扫描仪器二维码。"}</p>`) +
      pending;
    const autoStatus =
      automationLabels[automation.status] ||
      automation.status ||
      "等待后台状态";
    const autoMessage = automation.message || automation.error_type || "";
    card.querySelector('[data-field="automation"]').innerHTML =
      `<strong>${esc(autoStatus)}</strong>${autoMessage ? `<small>${esc(autoMessage)}</small>` : ""}`;
    card.querySelector('[data-field="output"]').innerHTML = latestOutput(
      data,
      camera.camera_id,
    );
  }

  async function fetchState(camera, requestId) {
    const query = new URLSearchParams({ camera_id: camera.camera_id });
    try {
      const response = await fetch(`/api/state?${query}`, {
        headers: { "X-Camera-Id": camera.camera_id },
        cache: "no-store",
        signal: AbortSignal.timeout(8000),
      });
      const data = await response.json();
      if (requestId !== overview.request) return;
      if (!response.ok)
        throw Error(data.detail || data.error?.message || "状态读取失败");
      updateCard(camera, data, null);
      return true;
    } catch (error) {
      if (requestId === overview.request) updateCard(camera, null, error);
      return false;
    }
  }

  async function refreshOverview() {
    if (overview.running) return;
    overview.running = true;
    const requestId = ++overview.request;
    try {
      const response = await fetch("/api/cameras", {
        cache: "no-store",
        signal: AbortSignal.timeout(8000),
      });
      const result = await response.json();
      if (!response.ok)
        throw Error(
          result.detail || result.error?.message || "相机列表读取失败",
        );
      const cameras = (result.items || []).filter(
        (camera) => camera.camera_id && camera.enabled !== false,
      );
      if (typeof pageContext.cameraList !== "undefined") {
        const changed =
          JSON.stringify(
            pageContext.cameraList.map((item) => item.camera_id),
          ) !== JSON.stringify(cameras.map((item) => item.camera_id));
        pageContext.cameraList = cameras;
        if (changed && typeof pageContext.renderCameraSelect === "function")
          pageContext.renderCameraSelect();
      }
      renderCards(cameras);
      const status = document.querySelector("#cameraOverviewStatus");
      if (status)
        status.textContent = cameras.length
          ? `共 ${cameras.length} 台已登记相机；每台相机独立运行和留存。`
          : "尚未发现已登记相机，请检查相机注册和后台服务。";
      await Promise.all(cameras.map((camera) => fetchState(camera, requestId)));
    } catch (error) {
      const status = document.querySelector("#cameraOverviewStatus");
      if (status) status.textContent = `相机总览暂不可用：${error.message}`;
    } finally {
      overview.running = false;
      overview.timer = setTimeout(refreshOverview, 3000);
    }
  }

  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-camera-detail]");
    if (!link) return;
    const id = link.dataset.cameraDetail;
    if (
      typeof pageContext.switchCamera === "function" &&
      id &&
      id !== pageContext.selectedCameraId
    ) {
      event.preventDefault();
      pageContext
        .switchCamera(id)
        .then(() => {
          location.hash = "#workspace";
        })
        .catch((error) => pageContext.message(error.message, true));
    }
  });
  window.addEventListener("pagehide", () => clearTimeout(overview.timer));
  window.addEventListener("DOMContentLoaded", refreshOverview);
})();
