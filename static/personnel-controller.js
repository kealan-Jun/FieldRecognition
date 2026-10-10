/* Personnel interactions; all mutable workbench state lives in the supplied page context. */
(function (root) {
  "use strict";
  function install(pageContext) {
    pageContext.waitingBindingText = function waitingBindingText() {
      if (pageContext.state?.automation?.enabled) {
        return pageContext.state.automation.status === "waiting_camera"
          ? pageContext.state.camera?.service_status?.online === false
            ? "等待采集服务恢复，恢复后重新扫码绑定，无需手动确认。"
            : "等待新鲜画面，清晰二维码入镜后自动绑定，无需手动确认。"
          : "等待仪器二维码。识别后自动绑定，无需确认。";
      }
      if (pageContext.historicalScan)
        return "当前未绑定。这里展示历史识别照片；开启自动运行后，使用新的相机画面绑定。";
      return "当前未绑定。登记实验员并开启自动运行后，仪器码入镜即可绑定。";
    };

    pageContext.currentCamera = function currentCamera() {
      return pageContext.previewLive
        ? pageContext.state?.camera.id
        : pageContext.scan?.camera_id || pageContext.state?.camera.id;
    };

    pageContext.selectBindings = function selectBindings(camera) {
      pageContext.activeBindings = pageContext.BindingView.current(
        pageContext.state,
        camera,
      ).instruments;
      pageContext.activeBinding =
        pageContext.activeBindings.length === 1
          ? pageContext.activeBindings[0]
          : null;
    };

    pageContext.renderBinding = function renderBinding() {
      pageContext.selectBindings(pageContext.currentCamera());
      const visits = pageContext.BindingView.current(
        pageContext.state,
        pageContext.currentCamera(),
      ).scenes;
      pageContext.$("#sceneInfo").innerHTML =
        pageContext.BindingView.scenesMarkup(visits);
      pageContext.$("#sceneInfo").hidden = false;
      const pending = pageContext.BindingView.pendingInstruments(
        pageContext.state,
        pageContext.currentCamera(),
      );
      pageContext.$("#bindingInfo").innerHTML =
        (pageContext.activeBindings.length
          ? pageContext.activeBindings
              .map(
                (b) =>
                  `<div class="bound-item"><b>仪器已绑定 · ${pageContext.esc(b.instrument.name)}</b><small>${pageContext.esc(b.instrument.device_category || "instrument")}${b.instrument.device_type ? ` · ${pageContext.esc(b.instrument.device_type)}` : ""} · ${pageContext.esc(b.operator)} · ${pageContext.esc(pageContext.stamp(b.started_at))}${b.valid_until ? " · 当天有效" : ""}</small><a href="${pageContext.esc(b.image_url)}" target="_blank" rel="noopener">绑定命中帧 ↗</a></div>`,
              )
              .join("")
          : pending
            ? ""
            : visits.length
              ? "场景绑定已完成；尚未绑定仪器，可继续扫描仪器码。"
              : pageContext.waitingBindingText()) + pending;
      pageContext.$("#scanResults").hidden =
        pageContext.historicalScan ||
        Boolean(pageContext.state?.automation?.enabled);
      pageContext.$("#scanBadge").textContent =
        `${pageContext.activeBindings.length} 台仪器 · ${visits.length} 个场景`;
      if (pageContext.scan) pageContext.renderQrEvidence();
      pageContext.updateButtons();
    };

    pageContext.renderAutomation = function renderAutomation() {
      const automatic = pageContext.state.automation;
      if (!automatic) return;
      const labels = pageContext.ViewUtils.labels.automation;
      pageContext.$("#automationState").textContent =
        labels[automatic.status] || automatic.status;
      pageContext.$("#automationDetail").textContent = automatic.message;
      pageContext.$("#operatorSummary").textContent = automatic.operator
        ? `实验员 · ${automatic.operator}`
        : "请先登记实验员";
      if (!automatic.operator) pageContext.$("#operatorSettings").open = true;
      pageContext.$("#operatorRegistration").textContent =
        automatic.registered_at
          ? `已登记：${automatic.operator} · ${pageContext.stamp(automatic.registered_at)}。更换人员后保存即可交接；原使用时段与历史读数保留。`
          : automatic.operator
            ? `当前相机已分配实验员：${automatic.operator}。设置仅对当前相机生效。`
            : "首次使用请登记姓名或编号，保存后本机持续运行，关闭网页也有效。";
      if (
        !pageContext.operatorDirty &&
        document.activeElement !== pageContext.$("#operator")
      )
        pageContext.$("#operator").value = automatic.operator || "";
      if (automatic.enabled) {
        const session = automatic.session;
        const detail = session?.message || automatic.message;
        pageContext.$("#automationDetail").textContent = detail;
        pageContext.$("#liveStatus").textContent =
          detail +
          (session?.frames_scanned
            ? ` · 已扫描 ${session.frames_scanned} 帧`
            : "");
      }
      pageContext.updateButtons();
    };

    pageContext.refreshPersonnel = async function refreshPersonnel() {
      pageContext.$("#cameraPersonnel").hidden =
        !pageContext.state.personnel_membership_enabled;
      pageContext.$("#operator").readOnly = Boolean(
        pageContext.state.personnel_membership_enabled,
      );
      if (!pageContext.state.personnel_membership_enabled) return;
      const camera =
        pageContext.selectedCameraId || pageContext.state.camera.id;
      pageContext.cameraMembers = await pageContext.api(
        `/api/cameras/${encodeURIComponent(camera)}/members`,
      );
      const selected = pageContext.operatorDirty
        ? pageContext.$("#cameraMember").value
        : pageContext.cameraMembers.active_user_id || "";
      pageContext.$("#cameraMember").innerHTML =
        '<option value="">请选择本次使用人员</option>' +
        pageContext.cameraMembers.members
          .map(
            (member) =>
              `<option value="${pageContext.esc(member.user_id)}">${pageContext.esc(member.display_name)} · ${pageContext.esc(member.user_id)}</option>`,
          )
          .join("");
      pageContext.$("#cameraMember").value = selected;
    };

    pageContext.fieldRulesForm = function fieldRulesForm(asset) {
      return `<details><summary>读数字段规则</summary><p class="caption">只填写铭牌、说明书或设备设置已确认的信息。留空表示未知；显示小数位数默认用于检查；仅确认设备为固定小数显示时，勾选按固定小数位换算。</p>${[
        "温度",
        "转速",
        "质量",
      ]
        .map((name) => {
          const r = asset.measurement_ranges?.[name] || {},
            units = { 温度: ["°C"], 转速: ["rpm"], 质量: ["g", "mg", "kg"] }[
              name
            ];
          return `<fieldset><legend>${name}</legend><div class="row"><label class="field">单位<select name="${name}.unit"><option value="">未知</option>${units.map((u) => `<option value="${u}" ${u === r.unit ? "selected" : ""}>${u}</option>`).join("")}</select></label><label class="field">最小值<input name="${name}.low" type="number" step="any" value="${pageContext.esc(r.range?.[0])}"></label><label class="field">最大值<input name="${name}.high" type="number" step="any" value="${pageContext.esc(r.range?.[1])}"></label><label class="field">显示小数位<input name="${name}.places" type="number" min="0" max="8" step="1" value="${pageContext.esc(r.decimal_places)}"></label><label class="field"><span>固定小数显示</span><input type="checkbox" name="${name}.fixed" ${r.fixed_decimal_display ? "checked" : ""}><small>仅对完整数字串按登记的小数位换算；原始识别文字保留</small></label></div></fieldset>`;
        })
        .join("")}</details>`;
    };

    pageContext.forms = function forms() {
      pageContext.$("#instrumentForms").innerHTML =
        pageContext.state.instruments
          .map(
            (a) =>
              `<form class="instrument-form" data-id="${pageContext.esc(a.id)}"><strong>${pageContext.esc(a.name)}</strong><div class="id">${pageContext.esc(a.id)}</div><div class="row"><label class="field">仪器名称<input name="name" value="${pageContext.esc(a.name)}" required maxlength="80"></label><label class="field">设备分类<input name="device_category" value="${pageContext.esc(a.device_category || "instrument")}" required maxlength="50"></label><label class="field">所属场景 / 实验区域<input name="scene" value="${pageContext.esc(a.scene)}" required maxlength="100"></label></div><label class="field">型号（可选）<input name="model" value="${pageContext.esc(a.model)}" maxlength="100"></label>${pageContext.fieldRulesForm(a)}<button class="secondary" type="submit">保存登记</button> <a href="/static/labels/Instrument${a.id === "e9434a0a-3319-414a-b988-4cc6884edce4" ? "A" : "B"}.png" target="_blank" rel="noopener">查看标签 ↗</a></form>`,
          )
          .join("");
      document.querySelectorAll(".instrument-form").forEach(
        (form) =>
          (form.onsubmit = async (e) => {
            e.preventDefault();
            const button = form.querySelector("button");
            if (pageContext.cameraSwitching) return;
            button.disabled = true;
            pageContext.pendingButtons.add(button);
            pageContext.updateButtons();
            try {
              const data = new FormData(form),
                values = Object.fromEntries(
                  ["name", "scene", "model", "device_category"].map((k) => [
                    k,
                    data.get(k),
                  ]),
                );
              values.measurement_ranges = {
                ...(pageContext.state.instruments.find(
                  (a) => a.id === form.dataset.id,
                )?.measurement_ranges || {}),
              };
              for (const name of ["温度", "转速", "质量"]) {
                const raw = (k) =>
                    String(data.get(`${name}.${k}`) || "").trim(),
                  number = (k) => (raw(k) === "" ? null : Number(raw(k)));
                if (
                  ["unit", "low", "high", "places", "fixed"].some(
                    (k) => raw(k) !== "",
                  )
                )
                  values.measurement_ranges[name] = {
                    unit: raw("unit") || null,
                    range: [number("low"), number("high")],
                    decimal_places: number("places"),
                    fixed_decimal_display: data.has(`${name}.fixed`),
                  };
                else delete values.measurement_ranges[name];
              }
              await pageContext.api(`/api/instruments/${form.dataset.id}`, {
                ...pageContext.json(values),
                method: "PUT",
              });
              await pageContext.refresh(false);
              if (pageContext.scan) {
                pageContext.scan.matches = pageContext.scan.matches.map(
                  (m) => ({
                    ...m,
                    ...pageContext.state.instruments.find((a) => a.id === m.id),
                  }),
                );
                pageContext.renderScan();
              }
              pageContext.message(
                "仪器登记和读数规则已保存，新任务使用登记规则快照。",
              );
            } catch (err) {
              pageContext.message(err.message, true);
            } finally {
              button.disabled = false;
              pageContext.pendingButtons.delete(button);
              pageContext.updateButtons();
            }
          }),
      );
    };

    pageContext.$("#bind").onclick = pageContext.busy(
      pageContext.$("#bind"),
      async () => {
        const selected =
          pageContext.scan?.matches?.length === 1
            ? { value: pageContext.scan.matches[0].id }
            : pageContext.$('input[name="instrument"]:checked');
        if (!selected) throw Error("请选择本次使用的仪器");
        const operator = pageContext.$("#operator").value.trim();
        if (!operator) throw Error("请填写实验员姓名或编号");
        pageContext.activeBinding = await pageContext.api(
          "/api/bindings",
          pageContext.json({
            scan_id: pageContext.scan.scan_id,
            instrument_id: selected.value,
            operator,
          }),
        );
        localStorage.setItem("fieldOperator", operator);
        await pageContext.refresh(false);
        pageContext.renderBinding();
        pageContext.message("绑定已保存。现在可拍摄面板并框选识别区域。");
      },
    );

    pageContext.$("#endBinding").onclick = pageContext.busy(
      pageContext.$("#endBinding"),
      async () => {
        await pageContext.api(
          "/api/camera/relations/end",
          pageContext.json({ camera_id: pageContext.currentCamera() }),
        );
        await pageContext.refresh(false);
        pageContext.message("本轮仪器与场景关联已结束；历史记录保留。");
      },
    );

    pageContext.$("#operator").oninput = () => {
      pageContext.operatorDirty = true;
    };

    pageContext.$("#cameraMember").onchange = () => {
      pageContext.operatorDirty = true;
      pageContext.personnelRequest = null;
      pageContext.$("#operator").value =
        pageContext.cameraMembers?.members.find(
          (member) => member.user_id === pageContext.$("#cameraMember").value,
        )?.display_name || "";
    };

    pageContext.$("#addCameraMember").onclick = pageContext.busy(
      pageContext.$("#addCameraMember"),
      async () => {
        const user_id = pageContext.$("#cameraMemberId").value.trim();
        if (!user_id) throw Error("请填写已登记的人员编号");
        await pageContext.api(
          `/api/admin/cameras/${encodeURIComponent(pageContext.selectedCameraId || pageContext.state.camera.id)}/members`,
          pageContext.json({ user_id }),
        );
        pageContext.$("#cameraMemberId").value = "";
        await pageContext.refreshPersonnel();
        pageContext.message("使用资格已登记，请明确选择本次实际使用人员。");
      },
    );

    pageContext.$("#removeCameraMember").onclick = pageContext.busy(
      pageContext.$("#removeCameraMember"),
      async () => {
        const id = pageContext.$("#cameraMember").value;
        if (!id) throw Error("请选择要取消资格的人员");
        await pageContext.api(
          `/api/admin/cameras/${encodeURIComponent(pageContext.selectedCameraId || pageContext.state.camera.id)}/members/${encodeURIComponent(id)}`,
          { method: "DELETE" },
        );
        pageContext.operatorDirty = false;
        await pageContext.refreshPersonnel();
        pageContext.message("使用资格已取消，历史记录保留。");
      },
    );

    pageContext.$("#autoEnable").onclick = pageContext.busy(
      pageContext.$("#autoEnable"),
      async () => {
        const operator = pageContext.$("#operator").value.trim();
        if (!operator) throw Error("请先登记实验员姓名或编号");
        const wearer_id = pageContext.state.personnel_membership_enabled
          ? pageContext.$("#cameraMember").value
          : null;
        if (pageContext.state.personnel_membership_enabled && !wearer_id)
          throw Error("请选择本次实际使用人员");
        const intent = {
          enabled: true,
          operator,
          wearer_id,
          expected_revision:
            pageContext.cameraMembers?.revision ??
            pageContext.state.automation?.revision ??
            null,
        };
        if (
          !pageContext.personnelRequest ||
          pageContext.personnelRequest.intent !== JSON.stringify(intent)
        )
          pageContext.personnelRequest = {
            intent: JSON.stringify(intent),
            body: { ...intent, request_id: crypto.randomUUID() },
          };
        try {
          await pageContext.api("/api/automation", {
            ...pageContext.json(pageContext.personnelRequest.body),
            method: "PUT",
          });
        } catch (error) {
          if (error.status === 409) {
            pageContext.personnelRequest = null;
            await pageContext.refreshPersonnel();
          }
          throw error;
        }
        pageContext.personnelRequest = null;
        pageContext.operatorDirty = false;
        await pageContext.refresh(false);
        pageContext.message(
          "实验员登记已保存，后台自动运行已开启；关闭网页仍会继续。",
        );
      },
    );

    pageContext.$("#autoPause").onclick = pageContext.busy(
      pageContext.$("#autoPause"),
      async () => {
        await pageContext.api("/api/automation", {
          ...pageContext.json({ enabled: false }),
          method: "PUT",
        });
        await pageContext.refresh(false);
        pageContext.message(
          "自动扫码已暂停。已有绑定和语音照片监控仍保留；结束使用请点击结束绑定。",
        );
      },
    );

    pageContext.$("#enterScene").onclick = pageContext.busy(
      pageContext.$("#enterScene"),
      async () => {
        const chosen = pageContext.$('input[name="scene"]:checked');
        if (!chosen) throw Error("请选择场景");
        await pageContext.api(
          "/api/scene/enter",
          pageContext.json({
            scan_id: pageContext.scan.scan_id,
            scene_id: chosen.value,
          }),
        );
        await pageContext.refresh(false);
        pageContext.selectBindings(pageContext.scan.camera_id);
        pageContext.renderBinding();
        pageContext.message("已进入场景，请拍摄仪器二维码并绑定使用。");
      },
    );
  }
  root.PersonnelController = { install };
  if (typeof module !== "undefined") module.exports = { install };
})(globalThis);
