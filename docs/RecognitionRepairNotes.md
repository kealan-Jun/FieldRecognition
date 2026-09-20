# 识别与现场复测补充说明（2026-09-20）

基线分支 `main`，提交 `ee0dbf31710a5505b0132bb2a1f564da9fd9c6b0`；开始时工作区已有大量未提交修改，本次在其实际实现上增量修复。未读取现场 NAS、生产库、报告所列 F:/Z: 文件或原图，未调用真实检测/OCR 服务。

## 已确认的当前实现与修复

| 现场报告 | 当前代码确认 | 本次修改及证据 | 尚需现场验证 |
|---|---|---|---|
| 电脑/墙上设备进入候选或待复核 | 启用面板检测时，当前代码已在 OCR 前检查 `allowed_instrument_ids`；通用类型即使只有一个绑定也保持 `instance_evidence_required`。这不能证明现场内部 reject 候选污染了正式结果。 | 保留该过滤，补路由回归。`measurement_records.binding_for_region` 在 reading、标准结果、草稿再次校验任务绑定快照、相机、采集时段与归属待确认状态。检测关闭时全图数字也不能借唯一绑定或同图二维码自动成为某台仪器的字段。raw lines/原始回执保留。 | 使用原图核对原始候选、排除原因、OCR区域、正式读数分别在哪层出现；模型将电脑误判为某资产专用类别的可能性仍未测量。 |
| 同值 clear/medium 导致空值 | 当前跨照片聚合签名已不比较 clarity，不能确认现场所述具体原因。另可本地复现：同一照片同一面板重复 OCR 候选因 `len(matches) != 1` 一律变空。 | `select_field_reading` 仅在同一帧、同一面板、同字段、数值相同且单位相容时选择质量较好的候选。数字冲突或不同面板仍为空并保留全部候选。`display_value` 保存 `0.0010`，数值契约仍为 number。 | 取得现场原 Evidence/Result 与模型回执后检查是否属于此重复候选路径；不能将此次合成回归声称为现场原因证明。 |
| 搅拌器 boxes=0、只有天平在允许名单 | 当前允许名单从任务绑定快照传入 `predict_readout`，类型/资产匹配先于 OCR；复原过滤也可能输出空列表。旧日志只有最终 boxes 无法区分。 | `panel_recovery` 保留逐检测视图返回数、质量排除原因；`panel_detector` 保留模型版本/权重/类映射与大小/数量过滤；`panel_regions` 保留 raw/quality/binding/OCR 数量和具体失败码。仅天平绑定的搅拌器候选不调用 OCR；正确搅拌器绑定允许双窗进入 OCR。 | 正确绑定的 63 °C / 1140 rpm 原图检测召回、裁剪、红字预处理与 OCR 准确率未验证。本次未换模型、调阈值或写死原图坐标/值。 |
| 温度有值、转速空、OFF 未写 | 当前已支持双字段及单字段 OFF，六字段格式已含可选 `display_state`。草稿此前把只有 OFF 的面板作为 unreadable，阻止正确状态确认。 | 保留既有双字段逻辑；OFF 草稿保留原文及状态来源，OFF 与数值/不可读候选混合仍冲突。每仪器输出 `field_statuses`/`field_completeness`；草稿 `instrument_completeness` 明确缺失指标，前端显示不完整。允许人工确认部分指标，但不将此解释为完整面板识别。 | 真实 OFF 是否被正确看清、角色是否正确分到温度或转速需要现场核对。单窗角色不明时继续不猜测。 |
| 单位正确 0/10 | 无原始统计材料，无法确定分母。 | 单位仍只来自 OCR 文本或登记字段配置，unknown/conflict 明确记录；`unit_basis` 在显示字段可见。复测分别记录组数、仪器数、字段数。 | 十组不等于十个字段；需要核对每个温度、转速、质量字段的正确单位与缺失单位。 |

`identity_evidence` 的 `detector_manifest_asset_mapping` 表示具体类由模型清单映射到资产、使用该帧检测区域和任务绑定；保留类、权重哈希、模型版本、区域及照片证据。它不是本次照片二维码空间解码证明，也不是已测得的身份识别准确率。仅 `type_id` 的通用模型仍要求实例证据，不按唯一绑定、画面中心、大小或清晰度猜资产。专用类别的真实误检率未验证。

## 新增诊断字段和兼容性

既有 `boxes`、`skipped_unbound_panels`、`skip_reason` 和六字段 `record` 契约保留。新增：

- `panel_detection.diagnostics.raw_candidate_count`、`raw_count_basis`、`passes`、`quality_filtered_count`、`binding_matched_count`、`ocr_input_region_count`、`excluded_candidates[].reason`。
- 原始数量指检测模型按其阈值/NMS 返回的候选，不是神经网络未筛选 proposal。未运行/异常时未知数量为 null；复原各视图可重复命中同一位置，raw 总数不能当作物理面板数。
- `failure_reason` 区分 `no_active_instrument_binding`、`target_not_allowed`、`instrument_identity_unconfirmed`、`panel_not_detected`、`panel_candidates_filtered`、`processing_exception`。兼容旧的 `no_visible_bound_panel`；检测异常终态为 failed。
- `panel_detection.duration_ms`、`ocr_elapsed_ms` 和每面板视觉调用 `duration_ms` 都由服务进程单调时钟测量，分别对应检测、区域 OCR/预处理及实际视觉调用，不混作跨机器端到端时长。
- `display_fields[].display_value`、`unit_basis`、原始 OFF source；标准结果外层证据 `field_statuses`/`field_completeness`。Result.json 中 number 仍不承载显示精度，精度在 reading/raw text/显示字段/证据中保留。

人工校正不提供历史占用授权。草稿可保存校正，但确认必须有各来源照片对应的有效仪器绑定；人工指定一个未绑定目标不能绕过正式输出过滤。同一明确 burst 跨绑定/操作人/佩戴人时，草稿值不合票、确认阻断；归档保留逐任务证据并拒绝合成一个负责人的结果。

## 隔离复测

本地运行（全部使用临时库、合成检测/OCR返回值）：

本次已实际运行识别/归档相关组合测试，结果 121 passed；新增只读核查脚本后的识别/发布聚焦集 34 passed，最后的字段展示过滤调整后相关集 48 passed。这些集合有重叠，不能相加当作总测试数。已运行 Python compileall、`node --check static/photo-measurements.js` 和 `git diff --check`，均通过；全仓最终结果以总修复报告为准。

```bash
env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  .venv/bin/python -m pytest -q tests/test_recognition_repairs.py tests/test_panel_recovery.py \
  tests/test_measurement_records.py tests/test_panel_regions.py tests/test_photo_measurements.py \
  tests/test_archive_events.py tests/test_readout_improvements.py tests/test_publication_repair.py \
  tests/test_multi_readout.py tests/test_panel_readout.py tests/test_unbound_readout.py
```

现场请在隔离 `FIELD_DEMO_DATA`（并设置 `FIELD_DATABASE_PATH` 指向其中的测试库）、独立服务端口和测试归档根下部署待验代码，关闭自动相机/照片目录监控；不要指向生产 Data 或写入原 NAS。复制获得授权的原图及回执到测试源目录，保留 SHA-256 和原始时间。使用真实解码二维码创建测试绑定：一组仅绑定天平，一组同时正确绑定搅拌器；每组以照片自身可信采集时间对应测试绑定窗口。若历史原图时间不落在测试绑定窗口，不修改原件或伪造原始采集时间，应通过隔离数据库中的明确测试历史会话 fixture 重放，标明 test_only。

检查每个 job 的 `all_binding_snapshots` / `allowed_instrument_ids`、模型版本与权重、诊断阶段数量、区域图和 raw lines，再核对仪器正式字段。控制一须无搅拌器正式结果且排除原因清楚；控制二必须进入搅拌器区域 OCR，按实际原图逐字段核对 63 °C 与 1140 rpm；看不清留空，OFF 不当零，缺一字段仍显示 incomplete。出现 raw=0 与 raw>0/quality=0 应分开排查。

可执行只读收据检查，不跟随 NAS/图片链接、不访问数据库：

```bash
.venv/bin/python scripts/audit_recognition_evidence.py /path/to/local-copied/Evidence.json
```

其 `allowed_matches_snapshot` 只检验回执内部一致性，历史回执缺少字段时为 null；输出恒为 `PARTIAL_EVIDENCE`，真实图片准确率为 `NOT_PROVEN`。该脚本不代替人核对原图。用相同原图三张/十组复测也只能建立复现基线，不证明长期准确率。
