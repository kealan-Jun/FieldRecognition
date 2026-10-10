/* Readout interactions; all mutable workbench state lives in the supplied page context. */
(function (root) {
  "use strict";
  function install(pageContext) {
    pageContext.renderVideoOcr = function renderVideoOcr(v) {
      pageContext.videoState = v;
      if (!v) return;
      const status = pageContext.ViewUtils.labels.video[v.status] || "正在准备";
      pageContext.$("#videoOcrStatus").textContent =
        `${status}${v.pending_panels && ["watching", "inferring"].includes(v.status) ? " · " + v.pending_panels + " 个面板自动校验中" : ""} · 已推理 ${v.frames_inferred || 0} 帧 · 留存 ${v.evidence_saved || 0} 帧${v.last_result_at ? " · 更新 " + pageContext.ViewUtils.timestamp(v.last_result_at, { timeOnly: true }) : ""}`;
      pageContext.$("#videoOcrToggle").textContent = v.enabled
        ? "暂停视频识别"
        : "开启视频识别";
      pageContext.$("#videoReadings").innerHTML =
        (["waiting_camera", "waiting_binding", "paused", "error"].includes(
          v.status,
        )
          ? []
          : v.latest_lines || []
        )
          .map(
            (line) =>
              `<div class="live-reading"><strong>${pageContext.esc(pageContext.ReadoutView.lineText(line))}</strong><small>${pageContext.esc(line.instrument?.name || "设备未定位")} · 自动读数</small></div>`,
          )
          .join("") || '<span class="caption">等待画面中出现清晰读数</span>';
    };

    pageContext.pollVideo = async function pollVideo() {
      const epoch = pageContext.cameraEpoch;
      try {
        if (pageContext.camerasLoaded)
          pageContext.renderVideoOcr(await pageContext.api("/api/video-ocr"));
      } catch (e) {
        if (epoch === pageContext.cameraEpoch)
          pageContext.$("#videoOcrStatus").textContent =
            "视频识别状态连接中，正在重试";
      }
      if (epoch === pageContext.cameraEpoch && !pageContext.cameraSwitching)
        pageContext.videoTimer = setTimeout(pageContext.pollVideo, 500);
    };

    pageContext.readoutPhoto = function readoutPhoto(job) {
      const url = job.image_url || job.crop_image_url;
      return url
        ? `<a class="readout-photo" href="${pageContext.esc(url)}" target="_blank" rel="noopener"><img src="${pageContext.esc(url)}" alt="这条结果对应的原图"><span>对应原图 ↗</span></a>`
        : "";
    };

    pageContext.readoutCaption = function readoutCaption(job) {
      const old = pageContext.ReadoutView.historical(
        job,
        pageContext.state?.bindings || [],
      );
      return `<p class="caption">${old ? "历史结果 · " : ""}${pageContext.esc(job.instrument?.name || "照片识别")}<br>拍摄 ${pageContext.esc(pageContext.stamp(pageContext.ReadoutView.capturedAt(job)))}${job.finished_at ? "<br>处理完成 " + pageContext.esc(pageContext.stamp(job.finished_at)) : ""}${old ? "<br>归属使用拍摄时的绑定，当前绑定不会改写旧照片。" : ""}</p>`;
    };

    pageContext.readoutLatency = function readoutLatency(job) {
      const t = job.timing || {},
        d = t.durations_ms || {},
        v = pageContext.ReadoutView.timing(job);
      return `<div class="readout-latency" title="写入时间取自 NAS 文件修改时间，未独立校时；等待与识别分开记录"><b>${v.label} ${pageContext.ReadoutView.duration(v.total)}</b>${v.delayedIngest ? `<span>读取前等待 ${pageContext.ReadoutView.duration(v.ingestWait)}${v.retries ? " · 重试 " + v.retries + " 次" : ""}；这段等待不属于 OCR 推理。</span>` : ""}<span>任务处理 ${pageContext.ReadoutView.duration(v.processing)} · OCR ${pageContext.ReadoutView.duration(d.ocr_ms)}${d.vision_ms !== null && d.vision_ms !== undefined ? " · 进一步识别 " + pageContext.ReadoutView.duration(d.vision_ms) : ""}</span>${t.is_backfill ? "<span>历史照片补处理，不代表实时延迟。</span>" : ""}</div>`;
    };

    pageContext.readoutIssue = function readoutIssue(job) {
      const fallback = job.fallback || {};
      if (fallback.status === "skipped")
        return `<p class="caption">${fallback.reason === "busy" ? "进一步识别正在处理其他照片，本次未调用。" : "进一步识别处于调用间隔内，本次未调用。"}原因已留存。</p>`;
      if (fallback.status !== "failed") return "";
      const reason =
        fallback.error === "account_arrearage"
          ? "阿里云账户欠费或余额状态受限，请恢复账户后重试。"
          : `进一步识别未完成${fallback.http_status ? "（HTTP " + fallback.http_status + "）" : ""}，具体原因已留存。`;
      return `<p class="readout-issue">${pageContext.esc(reason)}</p>`;
    };

    pageContext.reprocessControl = function reprocessControl(job) {
      if (!["completed", "failed"].includes(job.status)) return "";
      return `<details><summary>重新识别 · 版本 ${pageContext.esc(job.recognition_revision || 1)}</summary><form data-reprocess-job="${pageContext.esc(job.job_id)}"><label>操作人<input name="actor" required maxlength="100" value="${pageContext.esc(pageContext.state?.automation?.operator || "")}"></label><label>重识别原因<input name="reason" required maxlength="1000" placeholder="例如：数字区域规则已更新"></label><button type="submit">创建识别新版本</button><p class="caption">保留原照片、归属和旧结果。正式记录的修订需再次确认。</p></form><a href="/api/jobs/${pageContext.esc(job.job_id)}/versions" target="_blank" rel="noopener">查看版本记录 ↗</a></details>`;
    };

    pageContext.renderOcr = function renderOcr(job) {
      const target = pageContext.$("#ocrResults");
      if (job.record_scope === "draft") {
        target.innerHTML =
          pageContext.readoutPhoto(job) +
          pageContext.readoutCaption(job) +
          `<p>拍照 OCR ${["queued", "running"].includes(job.status) ? "正在处理" : "已生成测量草稿"}，确认后提交实验记录。</p><a href="/photo-measurements">查看草稿与校正 ↗</a>` +
          pageContext.reprocessControl(job);
        return;
      }
      if (["queued", "running"].includes(job.status)) {
        const phase =
          {
            local_ocr: "正在识别面板",
            waiting_readout: "暂未读到数字，继续等待识别结果",
            extended_reading: "正在进一步识别面板",
          }[job.phase] || "任务已入队";
        target.innerHTML =
          pageContext.readoutPhoto(job) +
          pageContext.readoutCaption(job) +
          `<p>◌ ${phase}…</p>`;
        return;
      }
      if (job.status !== "completed") {
        target.innerHTML =
          pageContext.readoutPhoto(job) +
          pageContext.readoutCaption(job) +
          `<p>${job.status === "cancelled" ? "这次识别已取消，原因已保存在回执中。" : "识别未完成，请稍后重新拍摄。"}</p>${pageContext.readoutLatency(job)}${pageContext.readoutIssue(job)}<a href="/api/jobs/${job.job_id}" target="_blank" rel="noopener">结果 JSON ↗</a>`;
        return;
      }
      if (job.recognition_skipped) {
        target.innerHTML =
          pageContext.readoutPhoto(job) +
          pageContext.readoutCaption(job) +
          `<p>${job.skip_reason === "no_active_instrument_binding" ? "拍照时没有有效仪器绑定，已跳过读数识别。" : "未检测到已绑定设备的面板，已跳过读数识别。"}</p>`;
        return;
      }
      const fields = job.display_fields || [];
      const fieldText = (f) =>
        f.display_state ||
        (f.value === null
          ? "未取得有效数值"
          : String(f.display_value ?? f.value) + (f.unit ? " " + f.unit : ""));
      target.innerHTML =
        pageContext.readoutPhoto(job) +
        pageContext.readoutCaption(job) +
        pageContext.readoutLatency(job) +
        fields
          .map(
            (f) =>
              `<div class="ocr-line"><small>${pageContext.esc(f.instrument_name || "")} ${pageContext.esc(f.name)}</small><strong>${pageContext.esc(fieldText(f))}</strong>${f.panel_image_url ? `<a href="${pageContext.esc(f.panel_image_url)}" target="_blank" rel="noopener">本面板照片 ↗</a>` : ""}${f.issues?.length ? "<small>数值与登记规则冲突，需校正。</small>" : ""}${f.raw_text ? `<small>识别原文：${pageContext.esc(f.raw_text)}</small>` : ""}</div>`,
          )
          .join("") +
        (!fields.length
          ? "<p>这张照片未取得可归属的规范读数。原图与识别原文已留在任务回执。</p>"
          : "") +
        pageContext.readoutIssue(job) +
        `<p><a href="${pageContext.esc(job.crop_image_url || job.image_url)}" target="_blank" rel="noopener">查看面板照片 ↗</a> · <a href="/api/jobs/${pageContext.esc(job.job_id)}" target="_blank" rel="noopener">结果 JSON ↗</a></p>` +
        pageContext.reprocessControl(job);
    };

    pageContext.watchJob = function watchJob(job) {
      const epoch = pageContext.cameraEpoch;
      pageContext.renderOcr(job);
      clearInterval(pageContext.jobTimer);
      pageContext.jobTimer = setInterval(async () => {
        try {
          const result = await pageContext.api(`/api/jobs/${job.job_id}`);
          pageContext.renderOcr(result);
          if (!["queued", "running"].includes(result.status)) {
            clearInterval(pageContext.jobTimer);
            pageContext.jobTimer = null;
            pageContext.updateButtons();
            await pageContext.refresh(false);
          }
        } catch (e) {
          if (epoch === pageContext.cameraEpoch) {
            clearInterval(pageContext.jobTimer);
            pageContext.jobTimer = null;
            pageContext.message(e.message, true);
            pageContext.updateButtons();
          }
        }
      }, 1000);
      pageContext.updateButtons();
    };

    pageContext.renderLatestJob = function renderLatestJob() {
      if (pageContext.jobTimer) return;
      const camera = pageContext.previewLive
        ? pageContext.state.camera.id
        : pageContext.scan?.camera_id || pageContext.state.camera.id;
      const latest = pageContext.ReadoutView.selectLatest(
        pageContext.state,
        camera,
      );
      const key = latest
        ? `${latest.job_id}:${latest.status}:${latest.phase}:${latest.finished_at}:${pageContext.ReadoutView.historical(latest, pageContext.state.bindings)}`
        : `no-related:${camera}`;
      if (key !== pageContext.displayedJobKey) {
        pageContext.displayedJobKey = key;
        if (latest) pageContext.renderOcr(latest);
        else
          pageContext.$("#ocrResults").textContent =
            "暂无有效面板读数。正在等待已绑定设备的清晰画面或语音照片。";
      }
    };

    pageContext.renderPhotoWatch = function renderPhotoWatch() {
      pageContext.$("#recordPolicy").textContent =
        pageContext.state.record_policy?.mode === "production"
          ? "生产拍照草稿 · 确认提交 ↗"
          : "测试用途 · 查看测量草稿 ↗";
      const watch = pageContext.state.photo_watch;
      if (!watch) {
        pageContext.$("#photoWatchState").textContent = "";
        return;
      }
      const labels = pageContext.ViewUtils.labels.photo;
      pageContext.$("#photoWatchState").textContent =
        watch.last_photo_status === "rejected"
          ? `照片未能处理：${watch.detail}`
          : `${labels[watch.status] || "等待语音拍照"}${watch.submitted_files !== undefined ? " · 已接收 " + watch.submitted_files + " 张" : ""}${watch.pending_files ? " · 待处理 " + watch.pending_files + " 张" : ""}${watch.retrying_files ? " · 单独重试 " + watch.retrying_files + " 张" : ""}${watch.oldest_pending_seconds >= 30 ? " · 最早等待 " + pageContext.ReadoutView.duration(watch.oldest_pending_seconds * 1000) : ""}`;
    };

    pageContext.$("#videoOcrToggle").onclick = pageContext.busy(
      pageContext.$("#videoOcrToggle"),
      async () => {
        pageContext.renderVideoOcr(
          await pageContext.api("/api/video-ocr", {
            ...pageContext.json({ enabled: !pageContext.videoState?.enabled }),
            method: "PUT",
          }),
        );
      },
    );

    document.addEventListener("submit", async (event) => {
      const form = event.target.closest("form[data-reprocess-job]");
      if (!form) return;
      event.preventDefault();
      const button = form.querySelector("button");
      if (button.disabled || pageContext.cameraSwitching) return;
      button.disabled = true;
      pageContext.pendingButtons.add(button);
      pageContext.updateButtons();
      const data = new FormData(form),
        payload = { actor: data.get("actor"), reason: data.get("reason") };
      const signature = JSON.stringify(payload);
      if (form.dataset.requestSignature !== signature) {
        form.dataset.requestId = crypto.randomUUID();
        form.dataset.requestSignature = signature;
      }
      try {
        const job = await pageContext.api(
          `/api/jobs/${form.dataset.reprocessJob}/reprocess`,
          pageContext.json({ ...payload, request_id: form.dataset.requestId }),
        );
        form.innerHTML = `<p>识别新版本已入队。<a href="/api/jobs/${pageContext.esc(job.job_id)}" target="_blank" rel="noopener">查看任务 ↗</a></p>`;
        pageContext.message("重识别已入队，原结果和照片已保留。");
        pageContext.watchJob(job);
        await pageContext.refresh(false);
      } catch (e) {
        pageContext.message(e.message, true);
        button.disabled = false;
      } finally {
        pageContext.pendingButtons.delete(button);
        pageContext.updateButtons();
      }
    });

    pageContext.$("#ocr").onclick = pageContext.busy(
      pageContext.$("#ocr"),
      async () => {
        pageContext.watchJob(
          await pageContext.api(
            "/api/ocr",
            pageContext.json({
              binding_id: pageContext.crop
                ? pageContext.activeBinding?.binding_id || null
                : null,
              auto_associate: true,
              capture_id: pageContext.scan.capture_id,
              crop: pageContext.crop,
            }),
          ),
        );
      },
    );
  }
  root.ReadoutController = { install };
  if (typeof module !== "undefined") module.exports = { install };
})(globalThis);
