/* Preview interactions; all mutable workbench state lives in the supplied page context. */
(function (root) {
  "use strict";
  function install(pageContext) {
    pageContext.previewUrl = function previewUrl() {
      const query = new URLSearchParams();
      if (pageContext.selectedCameraId)
        query.set("camera_id", pageContext.selectedCameraId);
      if (pageContext.recognitionPreview) query.set("recognition", "true");
      const suffix = query.toString();
      return "/api/camera/preview.mjpg" + (suffix ? "?" + suffix : "");
    };

    pageContext.closePreview = function closePreview() {
      clearTimeout(pageContext.previewRetry);
      pageContext.previewLive = false;
      pageContext.$("#neckPreview").removeAttribute("src");
      pageContext.$("#neckPreview").hidden = true;
      pageContext.$("#liveBadge").hidden = true;
      pageContext.$("#neckLive").textContent = "打开实时视频";
      pageContext.canvas.hidden = !pageContext.picture;
      pageContext.$("#empty").hidden = Boolean(pageContext.picture);
      pageContext.$("#sourceLabel").textContent =
        pageContext.picture && pageContext.scan
          ? `${pageContext.scan.camera_id} · ${pageContext.scan.width}×${pageContext.scan.height}`
          : "等待拍摄";
      if (!pageContext.picture)
        pageContext.$("#cropHint").textContent =
          "识别面板时，在照片上拖动框选显示区域。照片原件会保留。";
      pageContext.updateButtons();
    };

    pageContext.openPreview = function openPreview() {
      clearTimeout(pageContext.previewRetry);
      pageContext.stopWebcam();
      pageContext.previewLive = true;
      pageContext.canvas.hidden = true;
      pageContext.$("#empty").hidden = true;
      pageContext.$("#neckPreview").src = pageContext.previewUrl();
      pageContext.$("#neckPreview").hidden = false;
      pageContext.$("#liveBadge").hidden = false;
      pageContext.$("#neckLive").textContent = "关闭实时视频";
      pageContext.$("#sourceLabel").textContent =
        `${pageContext.state.camera.id} · 实时视频`;
      pageContext.$("#cropHint").textContent =
        "视频持续扫码；只读取已绑定设备的面板。";
      pageContext.updateButtons();
    };

    pageContext.pollCamera = async function pollCamera() {
      const epoch = pageContext.cameraEpoch;
      try {
        const previousBinding = pageContext.activeBinding?.binding_id;
        if (!pageContext.camerasLoaded) await pageContext.loadCameras();
        await pageContext.refresh(false);
        pageContext.checkCamera(epoch);
        if (previousBinding && !pageContext.activeBinding) {
          const text = pageContext.state.automation?.enabled
            ? "原绑定已结束，相机恢复后将自动重新扫码。"
            : "原绑定已结束，可在登记区重新开启自动运行。";
          pageContext.$("#liveStatus").textContent = text;
          pageContext.message(text);
        }
        const fresh =
          pageContext.state.camera.status === "streaming" &&
          pageContext.state.camera.frame_age_ms <= 1000 &&
          pageContext.state.camera.service_status?.online !== false;
        pageContext.$("#liveBadge").textContent = fresh
          ? pageContext.recognitionPreview
            ? "识别叠加 · 抽样画面"
            : "实时视频"
          : "等待相机新画面";
      } catch (e) {
        if (epoch === pageContext.cameraEpoch) {
          pageContext.$("#liveBadge").textContent = "连接中断，等待恢复";
          pageContext.$("#cameraState").textContent = "连接中断，等待恢复";
        }
      }
      if (epoch === pageContext.cameraEpoch && !pageContext.cameraSwitching)
        pageContext.cameraTimer = setTimeout(pageContext.pollCamera, 2000);
    };

    pageContext.handleSession = async function handleSession(result) {
      const epoch = pageContext.cameraEpoch;
      pageContext.$("#liveStatus").textContent =
        `${result.message}${result.frames_scanned ? ` · 已扫描 ${result.frames_scanned} 帧` : ""}`;
      if (["scanning", "waiting_camera"].includes(result.status)) return false;
      pageContext.scanSession = null;
      clearTimeout(pageContext.scanTimer);
      pageContext.scanTimer = null;
      await pageContext.refresh(false);
      pageContext.checkCamera(epoch);
      if (result.scan)
        await pageContext.showScan(result.scan, {
          keepLive: result.status === "bound",
        });
      pageContext.checkCamera(epoch);
      pageContext.selectBindings(result.camera_id);
      pageContext.renderBinding();
      pageContext.updateButtons();
      pageContext.message(
        result.message,
        ["failed", "needs_selection"].includes(result.status),
      );
      return true;
    };

    pageContext.pollScan = async function pollScan() {
      const epoch = pageContext.cameraEpoch,
        id = pageContext.scanSession;
      if (!id) return;
      try {
        const result = await pageContext.api(`/api/camera/scan-sessions/${id}`);
        if (epoch !== pageContext.cameraEpoch || pageContext.scanSession !== id)
          return;
        if (await pageContext.handleSession(result)) return;
      } catch (e) {
        if (epoch !== pageContext.cameraEpoch || pageContext.scanSession !== id)
          return;
        if (e.status === 404) {
          pageContext.scanSession = null;
          pageContext.updateButtons();
          pageContext.message(
            "扫码会话已结束，请重新开始；已有绑定会从服务端恢复。",
            true,
          );
          return;
        }
        pageContext.$("#liveStatus").textContent =
          "扫码状态暂不可用，正在重连；页面断开超过 20 秒后服务端停止扫码。";
      }
      if (
        epoch === pageContext.cameraEpoch &&
        pageContext.scanSession === id &&
        !pageContext.cameraSwitching
      )
        pageContext.scanTimer = setTimeout(pageContext.pollScan, 500);
    };

    pageContext.draw = function draw() {
      if (!pageContext.picture) return;
      pageContext.ctx.clearRect(
        0,
        0,
        pageContext.canvas.width,
        pageContext.canvas.height,
      );
      pageContext.ctx.drawImage(pageContext.picture, 0, 0);
      if (pageContext.crop) {
        pageContext.ctx.fillStyle = "#1e4a5222";
        pageContext.ctx.fillRect(...pageContext.crop);
        pageContext.ctx.strokeStyle = "#cf8337";
        pageContext.ctx.lineWidth = Math.max(3, pageContext.canvas.width / 350);
        pageContext.ctx.strokeRect(...pageContext.crop);
      }
      pageContext.$("#resetCrop").hidden = !pageContext.crop;
      pageContext.$("#cropHint").textContent = pageContext.previewLive
        ? "视频持续扫码；只读取已绑定设备的面板。"
        : pageContext.crop
          ? `已选面板区域：${pageContext.crop[2]} × ${pageContext.crop[3]} 像素。识别结果保留对应原图位置。`
          : "拖动框选面板；未选框时会识别整张照片。";
    };

    pageContext.point = function point(e) {
      const r = pageContext.canvas.getBoundingClientRect();
      return [
        Math.max(
          0,
          Math.min(
            pageContext.canvas.width,
            Math.round(
              ((e.clientX - r.left) * pageContext.canvas.width) / r.width,
            ),
          ),
        ),
        Math.max(
          0,
          Math.min(
            pageContext.canvas.height,
            Math.round(
              ((e.clientY - r.top) * pageContext.canvas.height) / r.height,
            ),
          ),
        ),
      ];
    };

    pageContext.renderQrEvidence = function renderQrEvidence() {
      const hits = [
        ...(pageContext.scan?.matches || []),
        ...(pageContext.scan?.scene_matches || []),
      ];
      const figure = pageContext.$("#qrEvidence");
      figure.hidden = !hits.length;
      if (!hits.length) return;
      const receipt = [
        ...(pageContext.state?.bindings || []),
        ...(pageContext.state?.scene_visits || []),
      ].find(
        (item) =>
          item.scan_id === pageContext.scan.scan_id &&
          item.camera_id === pageContext.scan.camera_id,
      );
      const label = receipt
        ? receipt.ended_at
          ? "历史绑定凭证"
          : receipt.visit_id
            ? "场景绑定成功帧"
            : "仪器绑定成功帧"
        : pageContext.historicalScan
          ? "历史解码凭证"
          : "识别命中帧";
      pageContext.$("#qrEvidenceTitle").textContent =
        hits.map((item) => item.name).join("、") + " · " + label;
      pageContext.$("#qrEvidenceTime").textContent =
        "收到画面：" +
        pageContext.stamp(pageContext.scan.received_at) +
        " · " +
        pageContext.scan.camera_id;
      pageContext.$("#qrEvidenceLink").href = pageContext.scan.image_url;
      pageContext.$("#qrEvidenceImage").src = pageContext.scan.image_url;
      const overlay = pageContext.$("#qrEvidenceOverlay");
      overlay.setAttribute(
        "viewBox",
        `0 0 ${pageContext.scan.width} ${pageContext.scan.height}`,
      );
      overlay.replaceChildren();
      for (const item of hits) {
        if (
          !Array.isArray(item.polygon) ||
          !item.polygon.every(
            (point) =>
              Array.isArray(point) &&
              point.length === 2 &&
              point.every(Number.isFinite),
          )
        )
          continue;
        const polygon = document.createElementNS(
          "http://www.w3.org/2000/svg",
          "polygon",
        );
        polygon.setAttribute(
          "points",
          item.polygon.map((point) => point.join(",")).join(" "),
        );
        overlay.appendChild(polygon);
      }
    };

    pageContext.renderScan = function renderScan() {
      pageContext.renderQrEvidence();
      pageContext.$("#sceneResults").innerHTML = (
        pageContext.scan.scene_matches || []
      )
        .map(
          (s) =>
            `<label class="match"><input type="radio" name="scene" value="${pageContext.esc(s.id)}" ${pageContext.scan.scene_matches.length === 1 ? "checked" : ""}><strong>${pageContext.esc(s.name)}</strong></label>`,
        )
        .join("");
      pageContext.$("#scanBadge").textContent = pageContext.historicalScan
        ? "历史识别"
        : pageContext.scan.matches.length ||
            pageContext.scan.scene_matches?.length
          ? "已读出二维码"
          : pageContext.scan.status === "no_qr"
            ? "二维码未能解码"
            : "未登记";
      pageContext.$("#scanResults").innerHTML = pageContext.scan.matches.length
        ? pageContext.scan.matches
            .map(
              (a, i) =>
                `<label class="match">${!pageContext.historicalScan && pageContext.scan.matches.length > 1 ? `<input type="radio" name="instrument" value="${pageContext.esc(a.id)}">` : ""}<div><strong>${pageContext.esc(a.name)}</strong><small>${pageContext.esc(a.scene || "尚未登记场景，请在下方填写")}</small><small>${pageContext.esc(a.id)}</small></div></label>`,
            )
            .join("")
        : `<p>${pageContext.esc(pageContext.scan.unknown.join("；") || (pageContext.scan.scene_matches?.length ? "场景码已识别，请先确认进入场景。" : "二维码未能解码。请让标签平整、正对镜头并避开反光；调整距离直到黑白格边缘清晰，再拍照。仅靠放大无法恢复失焦细节。若拍的是面板，可沿用已确认的仪器绑定进行 OCR。"))}</p>`;
    };

    pageContext.showScan = async function showScan(
      result,
      { keepLive = false, historical = false } = {},
    ) {
      const epoch = pageContext.cameraEpoch,
        image = new Image();
      await new Promise((resolve, reject) => {
        image.onload = resolve;
        image.onerror = () => reject(Error("图片预览失败"));
        image.src = result.image_url;
      });
      pageContext.checkCamera(epoch);
      if (!keepLive) pageContext.closePreview();
      pageContext.historicalScan = historical;
      pageContext.scan = result;
      pageContext.crop = null;
      pageContext.picture = image;
      pageContext.canvas.width = pageContext.picture.width;
      pageContext.canvas.height = pageContext.picture.height;
      pageContext.canvas.hidden = pageContext.previewLive;
      pageContext.$("#empty").hidden = true;
      pageContext.stopWebcam();
      pageContext.$("#sourceLabel").textContent = pageContext.previewLive
        ? `${pageContext.state.camera.id} · 实时视频`
        : `${result.camera_id} · ${result.width}×${result.height}`;
      pageContext.renderScan();
      pageContext.draw();
      pageContext.selectBindings(pageContext.scan.camera_id);
      pageContext.renderBinding();
      pageContext.updateButtons();
    };

    pageContext.upload = async function upload(
      blob,
      source = pageContext.activeBinding?.camera_id ||
        pageContext.currentCamera() ||
        "UploadedPhoto",
    ) {
      const form = new FormData();
      form.append("file", blob, "Capture.png");
      form.append("camera_id", source);
      await pageContext.showScan(
        await pageContext.api("/api/scans", { method: "POST", body: form }),
      );
    };

    pageContext.stopWebcam = function stopWebcam() {
      if (pageContext.stream)
        pageContext.stream.getTracks().forEach((t) => t.stop());
      pageContext.stream = null;
      pageContext.$("#webcam").hidden = true;
      pageContext.$("#shutter").hidden = true;
    };

    pageContext.$("#recognitionPreview").onclick = () => {
      pageContext.recognitionPreview = !pageContext.recognitionPreview;
      pageContext
        .$("#recognitionPreview")
        .setAttribute("aria-pressed", String(pageContext.recognitionPreview));
      pageContext.$("#recognitionPreview").textContent =
        pageContext.recognitionPreview ? "返回流畅视频" : "查看识别叠加";
      if (pageContext.previewLive)
        pageContext.$("#neckPreview").src = pageContext.previewUrl();
      else pageContext.openPreview();
      pageContext.$("#liveBadge").textContent = pageContext.recognitionPreview
        ? "识别叠加 · 抽样画面"
        : "实时视频";
    };

    pageContext.$("#neckLive").onclick = () => {
      if (pageContext.previewLive) {
        pageContext.closePreview();
        pageContext.draw();
      } else pageContext.openPreview();
    };

    pageContext.$("#neckPreview").onerror = () => {
      if (!pageContext.previewLive) return;
      pageContext.$("#liveBadge").textContent = "视频连接中断，正在重连";
      clearTimeout(pageContext.previewRetry);
      pageContext.previewRetry = setTimeout(() => {
        if (pageContext.previewLive) {
          const url = new URL(pageContext.previewUrl(), location.href);
          url.searchParams.set("retry", Date.now());
          pageContext.$("#neckPreview").src = url.href;
        }
      }, 2000);
    };

    pageContext.$("#continuousScan").onclick = pageContext.busy(
      pageContext.$("#continuousScan"),
      async () => {
        const operator = pageContext.$("#operator").value.trim();
        if (!operator) throw Error("请先填写右侧实验员姓名或编号");
        pageContext.scanStarting = true;
        pageContext.updateButtons();
        try {
          const result = await pageContext.api(
            "/api/camera/scan-sessions",
            pageContext.json({ operator }),
          );
          localStorage.setItem("fieldOperator", operator);
          pageContext.openPreview();
          pageContext.scanSession = result.session_id;
          if (!(await pageContext.handleSession(result)))
            pageContext.pollScan();
        } finally {
          pageContext.scanStarting = false;
          pageContext.updateButtons();
        }
      },
    );

    pageContext.$("#stopScan").onclick = pageContext.busy(
      pageContext.$("#stopScan"),
      async () => {
        const id = pageContext.scanSession;
        if (!id) return;
        const result = await pageContext.api(
          `/api/camera/scan-sessions/${id}`,
          { method: "DELETE" },
        );
        await pageContext.handleSession(result);
      },
    );

    pageContext.canvas.onpointerdown = (e) => {
      pageContext.drag = pageContext.point(e);
      pageContext.canvas.setPointerCapture(e.pointerId);
    };

    pageContext.canvas.onpointermove = (e) => {
      if (!pageContext.drag) return;
      const p = pageContext.point(e);
      pageContext.crop = [
        Math.min(pageContext.drag[0], p[0]),
        Math.min(pageContext.drag[1], p[1]),
        Math.abs(p[0] - pageContext.drag[0]),
        Math.abs(p[1] - pageContext.drag[1]),
      ];
      pageContext.draw();
    };

    pageContext.canvas.onpointerup = () => {
      pageContext.drag = null;
      if (
        pageContext.crop &&
        (pageContext.crop[2] < 16 || pageContext.crop[3] < 16)
      )
        pageContext.crop = null;
      pageContext.draw();
    };

    pageContext.$("#resetCrop").onclick = () => {
      pageContext.crop = null;
      pageContext.draw();
    };

    pageContext.$("#upload").onchange = () =>
      pageContext.busy(pageContext.$("#upload"), async () => {
        const file = pageContext.$("#upload").files[0];
        if (!file) return;
        try {
          pageContext.message("正在解码图片与二维码…");
          await pageContext.upload(file);
          pageContext.message("图片已留存，可读取面板。");
        } finally {
          pageContext.$("#upload").value = "";
        }
      })();

    document.querySelectorAll("[data-sample]").forEach(
      (b) =>
        (b.onclick = pageContext.busy(b, async () => {
          const r = await fetch(
            `/static/labels/${b.dataset.sample === "Scene01" ? "Scene01" : "Instrument" + b.dataset.sample}.png`,
          );
          await pageContext.upload(await r.blob(), "SampleCamera");
          pageContext.message(
            "这是二维码样张解码结果，可用于验证扫码与绑定流程。",
          );
        })),
    );

    pageContext.$("#neckCapture").onclick = pageContext.busy(
      pageContext.$("#neckCapture"),
      async () => {
        await pageContext.showScan(
          await pageContext.api("/api/camera/capture", { method: "POST" }),
        );
        pageContext.message("已取得挂脖设备照片。");
      },
    );

    pageContext.$("#webcamOpen").onclick = pageContext.busy(
      pageContext.$("#webcamOpen"),
      async () => {
        pageContext.closePreview();
        if (!navigator.mediaDevices?.getUserMedia)
          throw Error(
            "本机摄像头需要 localhost 或 HTTPS；挂脖设备取图和上传不受此限制。",
          );
        pageContext.stopWebcam();
        pageContext.stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: "environment" },
          audio: false,
        });
        pageContext.$("#webcam").srcObject = pageContext.stream;
        pageContext.$("#webcam").hidden = false;
        pageContext.$("#shutter").hidden = false;
        pageContext.canvas.hidden = true;
        pageContext.$("#empty").hidden = true;
      },
    );

    pageContext.$("#shutter").onclick = pageContext.busy(
      pageContext.$("#shutter"),
      async () => {
        const v = pageContext.$("#webcam"),
          c = document.createElement("canvas");
        c.width = v.videoWidth;
        c.height = v.videoHeight;
        if (!c.width) throw Error("摄像头尚未就绪");
        c.getContext("2d").drawImage(v, 0, 0);
        await pageContext.upload(
          await new Promise((r) => c.toBlob(r, "image/png")),
          "BrowserCamera",
        );
      },
    );
  }
  root.PreviewController = { install };
  if (typeof module !== "undefined") module.exports = { install };
})(globalThis);
