/* Workbench composition and camera-scoped request boundary. */
(function (root) {
  "use strict";
  const pageContext = {
    ViewUtils: root.ViewUtils,
    ReadoutView: root.ReadoutView,
    BindingView: root.BindingView,
  };
  pageContext.$ = (s) => document.querySelector(s);
  pageContext.activeBindings = [];
  pageContext.videoState = undefined;
  pageContext.videoTimer = undefined;
  pageContext.state = undefined;
  pageContext.scan = undefined;
  pageContext.activeBinding = undefined;
  pageContext.picture = undefined;
  pageContext.stream = undefined;
  pageContext.crop = null;
  pageContext.drag = null;
  pageContext.jobTimer = undefined;
  pageContext.previewLive = false;
  pageContext.scanSession = null;
  pageContext.scanTimer = undefined;
  pageContext.cameraTimer = undefined;
  pageContext.scanStarting = false;
  pageContext.displayedJobKey = null;
  pageContext.operatorDirty = false;
  pageContext.automationScanId = null;
  pageContext.previewInitialized = false;
  pageContext.previewRetry = undefined;
  pageContext.historicalScan = false;
  pageContext.recognitionPreview = false;
  pageContext.cameraList = [];
  pageContext.selectedCameraId = localStorage.getItem("fieldCameraId") || "";
  pageContext.cameraSwitching = false;
  pageContext.cameraEpoch = 0;
  pageContext.camerasLoaded = false;
  pageContext.cameraMembers = null;
  pageContext.personnelRequest = null;
  pageContext.historyMode = "all";
  pageContext.historyStack = [null];
  pageContext.historyNext = null;
  pageContext.historyData = [];
  pageContext.historyLoading = false;
  pageContext.historyRequest = 0;
  pageContext.historyRenderedKey = null;
  pageContext.workbenchData = null;
  pageContext.workbenchLoading = false;
  pageContext.pendingButtons = new Set();
  pageContext.canvas = pageContext.$("#imageCanvas");
  pageContext.ctx = pageContext.canvas.getContext("2d");
  pageContext.esc = pageContext.ViewUtils.escapeHtml;
  pageContext.displayCamera = pageContext.ViewUtils.displayCamera;
  pageContext.displayOperator = pageContext.ViewUtils.displayOperator;
  pageContext.stamp = pageContext.ViewUtils.timestamp;
  pageContext.json = (data) => ({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  pageContext.completeReadout =
    /^\s*[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+-]?\d+)?\s*(?:%|°?[CF]|℃|℉|[μµu]?g|kg|mg|ml|mL|L|rpm|r\/min|[mkM]?[AVW]|[kM]?Hz|Pa|kPa|MPa|bar|mm|cm|m|s|min|h|pH|ppm)?\s*$/i;

  pageContext.checkCamera = function checkCamera(epoch) {
    if (epoch !== pageContext.cameraEpoch)
      throw new DOMException("当前相机已切换", "AbortError");
  };

  pageContext.message = function message(text, error = false) {
    pageContext.$("#message").hidden = false;
    pageContext.$("#message").className = error ? "error" : "";
    pageContext.$("#message").textContent = text;
  };

  pageContext.api = async function api(path, options = {}) {
    const epoch = pageContext.cameraEpoch;
    const request = { ...options, headers: new Headers(options.headers || {}) };
    if (
      pageContext.selectedCameraId &&
      path.startsWith("/api/") &&
      path !== "/api/cameras" &&
      !request.headers.has("X-Camera-Id")
    )
      request.headers.set("X-Camera-Id", pageContext.selectedCameraId);
    const r = await fetch(path, request);
    pageContext.checkCamera(epoch);
    let data;
    try {
      data = await r.json();
    } catch {
      const error = Error(
        r.ok
          ? "服务返回的数据格式异常，请稍后重试"
          : "后台服务暂不可用，正在等待恢复；已保存的数据和任务保留",
      );
      error.status = r.status;
      throw error;
    }
    pageContext.checkCamera(epoch);
    if (!r.ok) {
      const error = Error(
        typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(
              data.detail || data.error?.message || data.code || "请求失败",
            ),
      );
      error.status = r.status;
      throw error;
    }
    return data;
  };

  pageContext.cameraLabel = function cameraLabel(camera) {
    const name = String(
      camera.display_name || camera.camera_id || "未命名相机",
    );
    return name === camera.camera_id ? name : `${name} · ${camera.camera_id}`;
  };

  pageContext.renderCameraSelect = function renderCameraSelect() {
    const select = pageContext.$("#cameraSelect");
    if (!select) return;
    const options = pageContext.cameraList.length
      ? pageContext.cameraList.map(
          (camera) =>
            `<option value="${pageContext.esc(camera.camera_id)}">${pageContext.esc(pageContext.cameraLabel(camera))}</option>`,
        )
      : pageContext.selectedCameraId
        ? [
            `<option value="${pageContext.esc(pageContext.selectedCameraId)}">${pageContext.esc(pageContext.selectedCameraId)}</option>`,
          ]
        : [];
    select.innerHTML = options.length
      ? options.join("")
      : '<option value="">未发现可切换相机</option>';
    select.value = pageContext.selectedCameraId;
    select.disabled =
      pageContext.cameraSwitching ||
      pageContext.pendingButtons.size > 0 ||
      !options.length;
  };

  pageContext.loadCameras = async function loadCameras() {
    const result = await pageContext.api("/api/cameras");
    pageContext.cameraList = (result.items || []).filter(
      (camera) => camera.camera_id && Boolean(camera.enabled),
    );
    if (!pageContext.camerasLoaded) {
      if (
        !pageContext.cameraList.some(
          (camera) => camera.camera_id === pageContext.selectedCameraId,
        )
      )
        pageContext.selectedCameraId =
          pageContext.cameraList[0]?.camera_id || "";
      pageContext.camerasLoaded = true;
      if (pageContext.selectedCameraId)
        localStorage.setItem("fieldCameraId", pageContext.selectedCameraId);
      else localStorage.removeItem("fieldCameraId");
    }
    if (
      pageContext.selectedCameraId &&
      !pageContext.cameraList.some(
        (camera) => camera.camera_id === pageContext.selectedCameraId,
      )
    )
      pageContext.cameraList.push({ camera_id: pageContext.selectedCameraId });
    pageContext.renderCameraSelect();
  };

  pageContext.resetCameraView = function resetCameraView() {
    pageContext.cameraMembers = null;
    pageContext.personnelRequest = null;
    pageContext.$("#cameraMember").innerHTML =
      '<option value="">请选择本次使用人员</option>';
    pageContext.state = null;
    pageContext.scan = null;
    pageContext.activeBinding = null;
    pageContext.activeBindings = [];
    pageContext.picture = null;
    pageContext.crop = null;
    pageContext.drag = null;
    pageContext.historicalScan = false;
    pageContext.automationScanId = null;
    pageContext.displayedJobKey = null;
    pageContext.previewInitialized = false;
    pageContext.videoState = null;
    pageContext.operatorDirty = false;
    clearInterval(pageContext.jobTimer);
    pageContext.jobTimer = null;
    pageContext.stopWebcam();
    pageContext.closePreview();
    pageContext.ctx.clearRect(
      0,
      0,
      pageContext.canvas.width,
      pageContext.canvas.height,
    );
    pageContext.historyRequest++;
    pageContext.historyStack = [null];
    pageContext.historyNext = null;
    pageContext.historyData = [];
    pageContext.historyLoading = false;
    pageContext.historyRenderedKey = null;
    pageContext.workbenchData = null;
    pageContext.workbenchLoading = false;
    pageContext.$("#operator").value = "";
    pageContext.$("#operatorSettings").open = false;
    pageContext.$("#operatorRegistration").textContent = "";
    pageContext.$("#qrEvidence").hidden = true;
    pageContext.$("#qrEvidenceImage").removeAttribute("src");
    pageContext.$("#qrEvidenceLink").removeAttribute("href");
    pageContext.$("#qrEvidenceOverlay").replaceChildren();
    pageContext.$("#sceneInfo").replaceChildren();
    pageContext.$("#sceneResults").replaceChildren();
    pageContext.$("#videoReadings").replaceChildren();
    pageContext.$("#resetCrop").hidden = true;
    pageContext.$("#message").hidden = true;
    pageContext.$("#upload").value = "";
    for (const selector of [
      "#bindingInfo",
      "#scanResults",
      "#ocrResults",
      "#historyRows",
      "#operatorSummary",
      "#automationState",
      "#automationDetail",
      "#photoWatchState",
      "#videoOcrStatus",
      "#ocrState",
      "#cameraState",
    ])
      pageContext.$(selector).textContent = "正在读取所选相机…";
    pageContext.$("#scanBadge").textContent = "等待扫码";
    pageContext.$("#historyScope").textContent = "";
    pageContext.$("#historyPager").hidden = true;
    pageContext.$("#liveStatus").textContent =
      "正在切换相机，后台自动运行继续。";
    pageContext.$("#sourceLabel").textContent = pageContext.selectedCameraId;
  };

  pageContext.switchCamera = async function switchCamera(nextCameraId) {
    if (
      !nextCameraId ||
      nextCameraId === pageContext.selectedCameraId ||
      pageContext.cameraSwitching ||
      pageContext.pendingButtons.size
    )
      return;
    pageContext.cameraSwitching = true;
    pageContext.cameraEpoch++;
    clearTimeout(pageContext.cameraTimer);
    clearTimeout(pageContext.videoTimer);
    clearTimeout(pageContext.scanTimer);
    pageContext.renderCameraSelect();
    pageContext.updateButtons();
    try {
      if (pageContext.scanSession) {
        const id = pageContext.scanSession;
        try {
          await pageContext.api(`/api/camera/scan-sessions/${id}`, {
            method: "DELETE",
            signal: AbortSignal.timeout(8000),
          });
        } catch (error) {
          if (error.status !== 404) throw error;
        }
        pageContext.scanSession = null;
      }
      pageContext.selectedCameraId = nextCameraId;
      localStorage.setItem("fieldCameraId", pageContext.selectedCameraId);
      pageContext.resetCameraView();
      pageContext.renderCameraSelect();
      await pageContext.refresh(false);
      pageContext.message(
        `已切换到 ${pageContext.selectedCameraId}；其他相机继续后台运行。`,
      );
    } finally {
      pageContext.cameraSwitching = false;
      pageContext.renderCameraSelect();
      pageContext.updateButtons();
      pageContext.cameraTimer = setTimeout(pageContext.pollCamera, 2000);
      pageContext.pollVideo();
      if (pageContext.scanSession) pageContext.pollScan();
    }
  };

  pageContext.busy = function busy(button, work) {
    return async () => {
      if (pageContext.cameraSwitching) return;
      pageContext.pendingButtons.add(button);
      button.disabled = true;
      pageContext.updateButtons();
      try {
        await work();
      } catch (e) {
        if (e.name !== "AbortError") pageContext.message(e.message, true);
      } finally {
        pageContext.pendingButtons.delete(button);
        button.disabled = false;
        pageContext.updateButtons();
      }
    };
  };

  pageContext.updateButtons = function updateButtons() {
    const manualScanning =
      Boolean(pageContext.scanSession) || pageContext.scanStarting;
    const autoScanning =
      pageContext.state?.automation?.enabled &&
      ["scanning", "waiting_camera"].includes(
        pageContext.state.automation.session?.status,
      );
    const scanning = manualScanning || autoScanning;
    pageContext.$("#enterScene").hidden =
      manualScanning ||
      pageContext.historicalScan ||
      pageContext.state?.automation?.enabled ||
      !pageContext.scan?.scene_matches?.length;
    pageContext.$("#bind").hidden =
      manualScanning ||
      pageContext.historicalScan ||
      Boolean(pageContext.state?.automation?.enabled) ||
      !pageContext.scan?.matches?.length;
    pageContext.$("#bind").textContent =
      pageContext.scan?.matches?.length > 1 ? "绑定所选仪器" : "绑定这台仪器";
    pageContext.$("#enterScene").disabled =
      manualScanning || !pageContext.scan?.scene_matches?.length;
    pageContext.$("#bind").disabled =
      manualScanning || !pageContext.scan?.matches?.length;
    pageContext.$("#ocr").disabled =
      pageContext.historicalScan ||
      pageContext.previewLive ||
      !pageContext.scan ||
      Boolean(pageContext.jobTimer);
    pageContext.$("#endBinding").hidden =
      !pageContext.activeBindings.length &&
      !pageContext.state?.scene_visits?.some(
        (v) => v.camera_id === pageContext.currentCamera(),
      );
    pageContext.$("#endBinding").disabled = false;
    pageContext.$("#continuousScan").disabled =
      scanning ||
      pageContext.state?.automation?.enabled ||
      pageContext.state?.camera?.mode !== "gwhp_main";
    pageContext.$("#stopScan").hidden = !pageContext.scanSession;
    pageContext.$("#operator").disabled =
      scanning || Boolean(pageContext.activeBindings.length);
    pageContext.$("#autoEnable").disabled = scanning;
    pageContext.$("#autoPause").disabled =
      !pageContext.state?.automation?.enabled;
    for (const s of [
      "#neckCapture",
      "#upload",
      "#webcamOpen",
      "#shutter",
      '[data-sample="A"]',
      '[data-sample="B"]',
      '[data-sample="Scene01"]',
    ])
      pageContext.$(s).disabled = manualScanning;
    pageContext.$("#neckLive").disabled =
      manualScanning || pageContext.state?.camera?.mode !== "gwhp_main";
    pageContext.$("#recognitionPreview").disabled =
      pageContext.state?.camera?.mode !== "gwhp_main";
    pageContext.$("#videoOcrToggle").disabled =
      !pageContext.state || pageContext.cameraSwitching;
    pageContext.$("#cameraSelect").disabled =
      pageContext.cameraSwitching ||
      pageContext.pendingButtons.size > 0 ||
      !pageContext.cameraList.length;
    pageContext.$("#workspace").inert =
      pageContext.cameraSwitching || !pageContext.state;
    pageContext.$("#registry").inert =
      pageContext.cameraSwitching || !pageContext.state;
    pageContext.$("#history").inert =
      pageContext.cameraSwitching || !pageContext.state;
    for (const button of pageContext.pendingButtons) button.disabled = true;
  };

  pageContext.refresh = async function refresh(renderForms = true) {
    pageContext.state = await pageContext.api("/api/state");
    await pageContext.refreshPersonnel();
    if (
      !pageContext.previewInitialized &&
      pageContext.state.camera.mode === "gwhp_main"
    ) {
      pageContext.previewInitialized = true;
      if (!pageContext.picture && !pageContext.stream)
        pageContext.openPreview();
    }
    pageContext.$("#cameraState").className =
      "pill" +
      (pageContext.state.camera.status === "streaming" ? "" : " warning");
    pageContext.$("#cameraState").title = pageContext.state.camera.id;
    pageContext.$("#cameraState").textContent = pageContext.state.camera
      .configured
      ? `${pageContext.ViewUtils.cameraStatus(pageContext.state.camera, { discovering: "正在发现", camera_offline: "设备离线" })}`
      : `${pageContext.state.camera.id} · 等待连接`;
    pageContext.$("#ocrState").textContent =
      `PaddleOCR · ${(pageContext.state.ocr.device || "cpu").toUpperCase()} · ${pageContext.ViewUtils.labels.ocr[pageContext.state.ocr.status] || pageContext.state.ocr.status}${pageContext.state.panel_detector?.enabled ? " · " + (pageContext.state.panel_detector.status === "ready" ? "A/B 面板定位已就绪" : pageContext.state.panel_detector.status === "error" ? "面板定位异常" : "面板定位加载中") : ""}`;
    pageContext.selectBindings(pageContext.currentCamera());
    pageContext.renderBinding();
    pageContext.renderLatestJob();
    pageContext.renderPhotoWatch();
    pageContext.renderAutomation();
    const autoScan =
      pageContext.state.automation?.session?.scan ||
      pageContext.state.last_camera_scan;
    if (autoScan && pageContext.automationScanId !== autoScan.scan_id) {
      pageContext.automationScanId = autoScan.scan_id;
      await pageContext.showScan(autoScan, {
        keepLive:
          pageContext.state.automation?.session?.status !== "needs_selection",
        historical: !pageContext.state.automation?.session?.scan,
      });
    }
    if (renderForms) pageContext.forms();
    pageContext.renderHistory();
    if (
      pageContext.historyMode === "readouts" &&
      location.hash === "#history" &&
      !pageContext.historyLoading
    )
      pageContext.loadReadoutPage();
    if (
      pageContext.historyMode === "workbenches" &&
      location.hash === "#history"
    )
      pageContext.loadWorkbenches();
  };

  pageContext.showPage = function showPage() {
    const titles = {
      cameras: "多相机工作台",
      workspace: "扫码与读数",
      registry: "仪器登记",
      history: "操作记录",
    };
    const page =
      location.hash.slice(1) in titles ? location.hash.slice(1) : "cameras";
    document
      .querySelectorAll(".page-view")
      .forEach((view) => (view.hidden = view.id !== page));
    document.querySelectorAll(".nav").forEach((link) => {
      const selected = link.getAttribute("href") === "#" + page;
      link.classList.toggle("active", selected);
      if (selected) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    document.body.classList.toggle("workspace-screen", page === "workspace");
    pageContext.$("#pageTitle").textContent = titles[page];
  };

  for (const controller of [
    root.PreviewController,
    root.ReadoutController,
    root.PersonnelController,
    root.HistoryController,
  ])
    controller.install(pageContext);
  root.FieldWorkbench = pageContext;
  pageContext.$("#cameraSelect").onchange = () =>
    pageContext
      .switchCamera(pageContext.$("#cameraSelect").value)
      .catch((error) => pageContext.message(error.message, true));

  pageContext.$("#refresh").onclick = pageContext.busy(
    pageContext.$("#refresh"),
    () => pageContext.refresh(),
  );

  pageContext.$("#operator").value = "";

  pageContext
    .loadCameras()
    .then(() => pageContext.refresh())
    .then(() => {
      pageContext.renderBinding();
      pageContext.pollCamera();
      pageContext.pollVideo();
    })
    .catch((e) => {
      pageContext.message(e.message, true);
      pageContext.cameraTimer = setTimeout(pageContext.pollCamera, 2000);
    });

  window.addEventListener("pagehide", () => {
    pageContext.stopWebcam();
    pageContext.closePreview();
    clearTimeout(pageContext.cameraTimer);
    clearTimeout(pageContext.videoTimer);
    clearTimeout(pageContext.scanTimer);
    if (pageContext.scanSession)
      fetch(`/api/camera/scan-sessions/${pageContext.scanSession}`, {
        method: "DELETE",
        headers: { "X-Camera-Id": pageContext.selectedCameraId },
        keepalive: true,
      }).catch(() => {});
  });

  window.addEventListener("hashchange", () => {
    pageContext.showPage();
    window.scrollTo(0, 0);
  });

  pageContext.showPage();
})(globalThis);
