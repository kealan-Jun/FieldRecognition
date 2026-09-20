# 结果发布、候选合并与延迟复测（2026-09-20）

检查基线为本地 `main` / `ee0dbf31710a5505b0132bb2a1f564da9fd9c6b0`，工作区已有大量未提交实现；本次保留并增量修改。未访问现场 NAS、生产数据库或报告中的 Windows 路径，未运行真实 OCR 或部署服务。

## 现场现象与代码确认

| 现场现象 | 当前代码确认 | 本次处理 | 证据边界 |
| --- | --- | --- | --- |
| 三十任务/十组 Evidence 可读耗时 10.22–27.46 秒 | 已有源写入、发现、稳定、读取、排队、识别、归档墙钟时间；未证明这些数值等于模型推理时间 | `readout_timing.py` 优先保留服务内单调时钟，逐字段注明时钟依据；`panel_readout.py` 实测处理、结果生成、视觉请求；检测/OCR 区域时长由 `panel_regions.py` 提供 | 现场报告数值未重新测量。NAS mtime 与服务时间差仍可能含时钟偏移，不能当模型性能 |
| 无目标面板时缺 Result | 当前已允许只发布 Evidence，且没有伪造 Result；HTTP 旧 Result 路径可能别名到 Evidence | `archive_events.py` 新增独立的 `processing_terminal`、`processing_outcome`、`task_outcomes`，跳过/失败均可结束等待；草稿待人工确认不等于识别仍在运行 | 检测异常归失败；服务重试/等待及连拍未收齐仍非终态 |
| clear / medium 导致相同值被置空 | 当前 `photo_measurements.candidates` 和 `archive_events.standard_records` 比较数字/单位，不比较 clarity，已具备同值保护 | 保留实现，新增同值不同清晰度、原始 `0.0010` 文本、真实数字冲突回归 | 合成文档仅证明合并规则，不能证明现场图像读出了正确数字 |
| 非目标仪器可能被人工修正带入正式文件 | `standard_records` 曾允许为“原先未绑定”的人工指派构造 Result | 移除这一输出旁路；人工决定和历史回执仍保留，Result 只从任务绑定与区域证据建立的仪器生成 | 已绑定但看不清的字段仍可按既有审核流程修正；不能借修正制造绑定 |
| 连拍期间发生交接可能合并不同责任人 | 归档原先只按 instrument_id 归并，采用首条 wearer | 增加 `binding_session_conflict`：同仪器存在多个使用会话/佩戴人时不生成合并 Result，`aggregation_issues` 保留逐任务证据；其他仪器不受影响 | 不把真实时段变化投票成唯一读数，需要按会话复核 |
| 读取正在发布的 JSON 可能失败 | 当前 `replace_view`/`immutable_write` 已使用同目录临时文件、flush、fsync、rename | 保留单文件原子实现并补并发读取、rename 失败恢复测试；`archive_paths.py` 强制 Result 先于 Evidence 发布；Evidence 增加 publication 协议与 generation | 单文件替换依赖实际挂载后端的 rename 语义；没有声称 NAS 多文件能一起原子发布 |

`Evidence.json` 的 `publication.protocol=evidence_manifest_sha256/1` 表示：读者先解析 Evidence，再逐个解析 `measurement_files` 指向的物理 Result，核对每个 SHA-256。不匹配或暂缺时重试；旧 Evidence 与新 Result 同时可见仍不构成一致快照。`processing_terminal=true` 后清单为空是明确的无结果终态，不制造成功 Result。

`timing` 通过 `task_id` 和 `group_id` 关联，`duration_clock_basis` 区分 `process_monotonic` 与墙钟差。队列跨进程、NAS 写入时钟等无法共享单调时钟的阶段仍保留带时区时间并声明来源，不补造值。`ocr_ms` 是区域识别流程/预处理/模型等待时长，仍不等同于纯模型内核推理。`archive_store.snapshot()` 的 `last_receipt_publication` 给出回执/任务/组对应的实测发布耗时，`last_view_rebuild` 是全档案派生视图重建时长，不能摊成某一任务的 OCR 耗时。Evidence 的候选合并时长在路径账本中保留该 generation 第一次重建的实测值，重复索引保持文档幂等。

## 隔离现场复测

脚本 `scripts/measure_publication_latency.py` 不启动服务、不改数据库、不调用相机/OCR；只在显式初始化的空目录下写三图副本及 PhotoReceipt，读取该目录下的 archive。它拒绝未经初始化的目录和指向根外的子目录符号链接。原图只读，复制到测试环境后以注入时间作为**测试重放时间**，不是原始物理拍摄时间。

1. 在本项目目录中初始化新的隔离根：

   ```bash
   .venv/bin/python scripts/measure_publication_latency.py init --root tmp/latency-site-check
   ```

2. 按 `docs/ProductionRuntime.md` 的服务配置机制启动独立测试实例，使用独立数据根/数据库、独立端口、`FIELD_RECORD_MODE=test`、独立相机标识，照片根和归档根分别指向上述根中的 `voice_photos`、`archive`；清除生产 Receiver/相机快照地址，不复用生产 DB 或服务。目录自动发现必须启用 `FIELD_PHOTO_RECEIPT_REQUIRED=1`。在这个实例中登记测试人员与仪器，再用实际解码的二维码建立绑定；先核对任务的绑定快照与 `allowed_instrument_ids`。如果是在 NAS 上复测，初始化位置必须是授权的独立测试目录，原始现场目录只作只读来源。

3. 在写入和观察的同一客户端进程中运行，例如（替换三个真实可读路径和独立测试相机 ID）：

   ```bash
   .venv/bin/python scripts/measure_publication_latency.py run \
     --root tmp/latency-site-check --camera-id TestCamera \
     --images /read-only/photo1.jpg /read-only/photo2.jpg /read-only/photo3.jpg \
     --groups 10 --interval 60 --timeout 180 --poll 0.2
   ```

   没有默认性能门槛；产品确定后才传 `--evidence-target-ms` / `--result-target-ms`。interval/timeout/poll 是实验参数，不修改服务的文件稳定等待或推理阈值。脚本每轮写出 `reports/latency-*.json`，保留运行中间状态，结束后打印摘要。

4. 检查每组 `t0`：三张副本全部写完并完成本客户端 fsync/rename 后记录；分别核对 `evidence_first_parse_ms`、`result_first_parse_ms`、`evidence_terminal_parse_ms`、`manifest_verified_ms`。Result 的首次解析可早于 Evidence，脚本预先观测 Result 并在得到清单后核对归属和哈希。多仪器各 Result 另有逐文件首次解析时间；`result_first_parse_ms` 指首个结果文件，`manifest_verified_ms` 才代表终态清单全部一致可读。未识别的 Result HTTP 别名不计作物理读数文件。

5. 分别核对 `group_count`、`expected_task_count`、`observed_task_count` 与组/任务成功、跳过、失败、超时数量。每个统计字段都有实际样本数；例如十组中一组合法跳过，Evidence 可能 n=10，Result 可能 n=9，不能把分母都写成三十个任务。脚本不输出缺乏样本支撑的分位数，不把十组均值当长期稳定性。

脚本的延迟用同进程单调时钟，UTC 仅为日志标签，因此不混用 NAS 服务端 mtime。fsync/rename 完成仅表示客户端调用完成；网络文件系统可见性、缓存、服务处理、轮询间隔都进入这条端到端观测，不等于存储服务器的物理落盘时刻。换到另一台机器观察时必须另行记录时钟同步条件，不能直接相减两个 monotonic 值。

## 自动验证与兼容性

已实际运行聚焦测试：

```bash
env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  .venv/bin/python -m pytest -q tests/test_publication_repair.py tests/test_readout_timing.py \
  tests/test_archive_events.py tests/test_archive_store.py tests/test_panel_readout.py
```

该命令首次为 44 通过、1 失败：暴露每次索引更新候选合并耗时破坏 Evidence 幂等。改为每 generation 保留首次实测值，并新增连拍终态、无绑定人工修正、跨交接连拍回归后，实际结果 **50 passed**（另有既有 Starlette/AnyIO 弃用警告）。上述修改文件已通过 `.venv/bin/python -m compileall -q`，`git diff --check` 无错误。

独立 CLI 已在 `tmp/publication_script_validation/run` 实际执行一组三图复制与 0.1 秒超时观测（未启动服务）：结果为 **1 组 timeout、期望 3 任务、观测 0 任务、Evidence/Result 样本数均 0**。这证明无服务时脚本诚实报告超时，不能算识别或现场发布成功。

Result 六字段格式、旧 status/skip_reason、旧 HTTP 兼容别名保留。Evidence 增加可选元数据，旧消费者仍可使用原字段；新消费者使用终态和哈希清单。无需业务数据库迁移；归档路径账本仅增加 `event_stage_timings` 可选成员，旧版本读取器忽略扩展字段，原始 immutable 回执和源照片不改写。发布前要核对 NAS 的同目录原子 rename 与缓存行为，保留归档目录/路径账本备份；本轮未自动执行生产升级或归档重建。

`PROVEN`：以上本地临时目录测试所覆盖的事务外发布、JSON 一致性、合并与统计规则。`PARTIAL_EVIDENCE`：已有实现与合成回归支撑协议，但实际 NAS 后端未测试。`NOT_PROVEN`：现场图像准确率、搅拌器 63 °C/1140 rpm、真实新版本 Evidence/Result 端到端延迟及长期稳定性。
