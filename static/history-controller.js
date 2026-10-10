/* History interactions; all mutable workbench state lives in the supplied page context. */
(function (root) {
  "use strict";
  function install(pageContext) {
    pageContext.duration = function duration(value) {
      return pageContext.ReadoutView.duration(value);
    };

    pageContext.historyLatency = function historyLatency(event) {
      if (event.kind !== "readout") return "—";
      const timing = event.timing || {},
        d = timing.durations_ms || {};
      const total =
        event.input_mode === "video"
          ? d.frame_to_result_ms
          : timing.is_backfill
            ? d.detect_to_result_ms
            : d.write_to_result_ms;
      const waitNote =
        d.ingest_wait_ms >= 30000
          ? `<small>读取前等待 ${pageContext.duration(d.ingest_wait_ms)}</small>`
          : "";
      const parts = [
        ["文件发现", d.write_to_detect_ms],
        ["稳定等待", d.file_stability_ms],
        ["读取", d.file_read_ms],
        ["读取前等待", d.ingest_wait_ms],
        ["导入", d.import_ms],
        ["任务排队", d.queue_wait_ms],
        ["OCR 等待", d.ocr_wait_ms],
        ["OCR", d.ocr_ms],
        ["进一步识别", d.vision_ms],
        ["写入至归档", event.archive?.write_to_archive_ms],
      ];
      return `<strong>${event.input_mode === "video" ? "视频帧至结果" : timing.is_backfill ? "补处理" : "写入至结果"} ${pageContext.duration(total)}</strong>${waitNote}<small>OCR ${pageContext.duration(d.ocr_ms)}</small><details class="history-timing"><summary>阶段耗时</summary><dl>${parts.map(([label, value]) => `<div><dt>${label}</dt><dd>${pageContext.duration(value)}</dd></div>`).join("")}</dl><small>${timing.is_backfill ? "历史补处理，不计入实时性能。" : "写入时间取自文件 mtime，时钟尚未独立校准。"}</small></details>`;
    };

    pageContext.historyArchive = function historyArchive(event) {
      const a = event.archive || {};
      return `<span class="pill ${a.status === "archived" ? "" : "muted"}">${a.status === "archived" ? "已归档" : a.status === "pending" ? "待同步" : "未记录"}</span>${a.archived_at ? `<small>${pageContext.esc(pageContext.stamp(a.archived_at))}</small>` : ""}`;
    };

    pageContext.loadReadoutPage = async function loadReadoutPage() {
      const request = ++pageContext.historyRequest,
        cursor = pageContext.historyStack.at(-1);
      pageContext.historyLoading = true;
      pageContext.renderHistory();
      try {
        const page = await pageContext.api(
          "/api/readouts?related_only=true&limit=20" +
            (cursor ? "&before=" + cursor : ""),
        );
        if (request !== pageContext.historyRequest) return;
        pageContext.historyData = page.items;
        pageContext.historyNext = page.next_cursor;
      } catch (error) {
        if (request === pageContext.historyRequest)
          pageContext.message(error.message, true);
      } finally {
        if (request === pageContext.historyRequest) {
          pageContext.historyLoading = false;
          pageContext.renderHistory();
        }
      }
    };

    pageContext.loadWorkbenches = async function loadWorkbenches() {
      if (pageContext.workbenchLoading) return;
      const epoch = pageContext.cameraEpoch;
      pageContext.workbenchLoading = true;
      try {
        pageContext.workbenchData = await pageContext.api(
          "/api/workbenches?related_only=true",
        );
        if (pageContext.historyMode === "workbenches")
          pageContext.renderHistory();
      } catch (e) {
        if (epoch === pageContext.cameraEpoch && e.name !== "AbortError")
          pageContext.message(e.message, true);
      } finally {
        if (epoch === pageContext.cameraEpoch)
          pageContext.workbenchLoading = false;
      }
    };

    pageContext.benchReadings = function benchReadings(rows) {
      return (
        rows
          .slice(0, 8)
          .map(
            (r) =>
              `<div class="bench-reading"><strong>${pageContext.esc(pageContext.ReadoutView.lineText(r))}</strong><span>${pageContext.esc(pageContext.stamp(r.captured_at))} · ${pageContext.esc(r.operator || "未记录实验员")}</span><a href="${pageContext.esc(r.image_url)}" target="_blank" rel="noopener">原图 ↗</a> <a href="${pageContext.esc(r.result_url)}" target="_blank" rel="noopener">回执 ↗</a></div>`,
          )
          .join("") || '<p class="caption">暂无符合记录条件的面板读数</p>'
      );
    };

    pageContext.renderWorkbenches = function renderWorkbenches() {
      pageContext.$("#historyPager").hidden = true;
      pageContext.$("#historyScope").textContent =
        "最近 200 次相关面板识别 · 每组展示最近 8 条读数；完整记录见“面板读数”";
      const groups = pageContext.workbenchData?.workbenches || [];
      const key = JSON.stringify(["workbenches", groups]);
      if (pageContext.historyRenderedKey === key) return;
      pageContext.historyRenderedKey = key;
      pageContext.$("#historyRows").innerHTML = groups.length
        ? groups
            .map(
              (g) =>
                `<section class="bench-group"><h3>${pageContext.esc(g.name)}</h3><p class="caption">${g.photo_count} 次识别 · ${g.reading_count} 条面板读数 · 按仪器分别记录，不相加</p><div class="bench-grid">${g.instruments.map((i) => `<div class="bench-instrument"><h4>${pageContext.esc(i.name)} <small>${i.readings.length} 条</small></h4>${pageContext.benchReadings(i.readings)}</div>`).join("")}${g.unassigned_readings.length ? `<div class="bench-instrument"><h4>实验台待归属 <small>${g.unassigned_readings.length} 条</small></h4>${pageContext.benchReadings(g.unassigned_readings)}</div>` : ""}</div></section>`,
            )
            .join("")
        : "正在读取实验台记录…";
    };

    pageContext.renderHistory = function renderHistory() {
      if (!pageContext.state) return;
      const archive = pageContext.state.archive || {};
      const archiveState =
        {
          ready: "已连接",
          retrying: "等待重试，记录保留在本机",
          indexing: "正在整理分类索引",
          waiting_integrity: "正在校验归档",
        }[archive.status] || "正在连接";
      const archiveProgress =
        archive.status === "indexing" && archive.progress_total
          ? `（${archive.progress_current || 0}/${archive.progress_total}）`
          : "";
      pageContext.$("#archiveStatus").textContent = archive.enabled
        ? `NAS 留存：${archiveState}${archiveProgress} · 已归档 ${archive.archived_receipts || 0} 个版本 · 待同步 ${archive.pending_receipts || 0} 个版本${archive.navigation_pending ? " · 分类索引待同步" : ""}`
        : "NAS 留存未启用，记录保存在本机。";
      pageContext.$("#archiveStatus").title = archive.root || "";
      const integrity = archive.integrity || {},
        check = integrity.last_report;
      pageContext.$("#integrityStatus").textContent =
        integrity.status === "running"
          ? "完整性巡检进行中"
          : check
            ? `最近巡检：${{ completed: "校验通过", findings: "发现异常", unavailable: "存储暂不可用", interrupted: "检查中断" }[check.status] || check.status} · ${pageContext.stamp(check.finished_at)} · ${check.issue_count} 项异常`
            : "等待首次完整性巡检";
      if (pageContext.historyMode === "workbenches") {
        pageContext.renderWorkbenches();
        return;
      }
      const events =
        pageContext.historyMode === "readouts"
          ? pageContext.historyData
          : pageContext.state.activity || [];
      pageContext.$("#historyScope").textContent =
        pageContext.historyMode === "readouts"
          ? `第 ${pageContext.historyStack.length} 页 · ${events.length} 条 · 按提交先后${pageContext.historyLoading ? " · 正在更新" : ""}`
          : `最近 ${events.length} 条操作`;
      pageContext.$("#historyPager").hidden =
        pageContext.historyMode !== "readouts";
      pageContext.$("#historyPrevious").disabled =
        pageContext.historyLoading || pageContext.historyStack.length === 1;
      pageContext.$("#historyNext").disabled =
        pageContext.historyLoading || !pageContext.historyNext;
      const labels = {
        photo: "照片 / 视频证据",
        scan: "扫码识别",
        binding_started: "仪器绑定",
        binding_ended: "结束绑定",
        binding_end_corrected: "绑定判定修正",
        binding_restored: "恢复绑定",
        scene_end_corrected: "场景判定修正",
        scene_restored: "恢复场景",
        scene_entered: "进入场景",
        scene_left: "结束场景",
        readout: "面板读数",
      };
      if (
        document.activeElement?.closest("#historyRows form[data-reprocess-job]")
      )
        return;
      const renderKey = JSON.stringify([
        events,
        pageContext.historyLoading && events.length === 0,
      ]);
      if (renderKey === pageContext.historyRenderedKey) return;
      pageContext.historyRenderedKey = renderKey;
      // Preserve expanded timing details while the background status refreshes.
      const opened = new Set(
        [...document.querySelectorAll("#historyRows tr[data-event]")]
          .filter((row) => row.querySelector("details[open]"))
          .map((row) => row.dataset.event),
      );
      pageContext.$("#historyRows").innerHTML = events.length
        ? `<div class="table-scroll"><table class="history-table"><thead><tr><th>时间 / 事件</th><th>对象与结果</th><th>实验员 / 相机</th><th>识别耗时</th><th>NAS 留存</th><th>凭证</th></tr></thead><tbody>${events.map((event) => `<tr data-event="${pageContext.esc(event.event_id)}"><td>${pageContext.esc(pageContext.stamp(event.occurred_at))}<small>${pageContext.esc(event.kind === "readout" && event.input_mode === "video" ? "视频面板读数" : labels[event.kind] || event.kind)}</small>${event.captured_at ? `<small>拍摄 ${pageContext.esc(pageContext.stamp(event.captured_at))}</small>` : ""}</td><td><strong>${pageContext.esc(event.target)}</strong><small>${pageContext.esc(event.status)}</small>${event.detail ? `<div class="history-reading">${pageContext.esc(event.detail)}</div>` : ""}${(event.quality_notes || []).map((note) => `<small class="history-error">${pageContext.esc(note)}</small>`).join("")}${event.fallback?.status === "failed" ? `<small class="history-error">${event.fallback.error === "account_arrearage" ? "账户欠费，进一步识别不可用" : "进一步识别失败，见任务回执"}</small>` : ""}</td><td>${pageContext.esc(pageContext.displayOperator(event.operator) || "当时未记录")}<small>${pageContext.esc(pageContext.displayCamera(event.camera_id))}</small></td><td>${pageContext.historyLatency(event)}</td><td>${pageContext.historyArchive(event)}</td><td>${event.image_url ? `<a class="history-photo" href="${pageContext.esc(event.image_url)}" target="_blank" rel="noopener"><img src="${pageContext.esc(event.image_url)}" loading="lazy" decoding="async" alt="本条记录的照片">原图 ↗</a>` : ""}${event.result_url ? `<a href="${pageContext.esc(event.result_url)}" target="_blank" rel="noopener">任务回执 ↗</a>` : ""}${event.job_id ? pageContext.reprocessControl({ job_id: event.job_id, status: event.raw_status, recognition_revision: event.recognition_revision }) : ""}${event.measurement_url ? `<a href="${pageContext.esc(event.measurement_url)}" target="_blank" rel="noopener">标准读数 ↗</a>` : ""}</td></tr>`).join("")}</tbody></table></div>`
        : pageContext.historyLoading
          ? "正在读取记录…"
          : "当前范围暂无相关记录。二维码识别、绑定和有明确面板归属的读数会显示在这里。";
      document
        .querySelectorAll("#historyRows tr[data-event]")
        .forEach((row) => {
          if (opened.has(row.dataset.event) && row.querySelector("details"))
            row.querySelector("details").open = true;
        });
    };

    pageContext.$("#export").onclick = pageContext.busy(
      pageContext.$("#export"),
      async () => {
        const data = await pageContext.api("/api/export");
        const url = URL.createObjectURL(
          new Blob([JSON.stringify(data, null, 2)], {
            type: "application/json",
          }),
        );
        const a = document.createElement("a");
        a.href = url;
        a.download = "FieldRecognitionSession.json";
        a.click();
        URL.revokeObjectURL(url);
      },
    );

    pageContext.$("#historyMode").onchange = () => {
      pageContext.historyMode = pageContext.$("#historyMode").value;
      pageContext.historyStack = [null];
      pageContext.historyNext = null;
      pageContext.historyData = [];
      pageContext.historyRequest++;
      pageContext.historyLoading = false;
      if (pageContext.historyMode === "readouts") pageContext.loadReadoutPage();
      else if (pageContext.historyMode === "workbenches")
        pageContext.loadWorkbenches();
      else pageContext.renderHistory();
    };

    pageContext.$("#historyNext").onclick = () => {
      if (!pageContext.historyLoading && pageContext.historyNext) {
        pageContext.historyStack.push(pageContext.historyNext);
        pageContext.historyData = [];
        pageContext.historyNext = null;
        pageContext.loadReadoutPage();
      }
    };

    pageContext.$("#historyPrevious").onclick = () => {
      if (!pageContext.historyLoading && pageContext.historyStack.length > 1) {
        pageContext.historyStack.pop();
        pageContext.historyData = [];
        pageContext.historyNext = null;
        pageContext.loadReadoutPage();
      }
    };
  }
  root.HistoryController = { install };
  if (typeof module !== "undefined") module.exports = { install };
})(globalThis);
