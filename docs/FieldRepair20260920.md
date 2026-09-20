# 设备人员、交接、识别与发布修复（2026-09-20）

## 检查基线与边界

实际仓库为 `/home/x1/Projects/FieldRecognition`，分支 `main`，提交
`ee0dbf31710a5505b0132bb2a1f564da9fd9c6b0`。配置了 `origin`（kealan-Jun/FieldRecognition）和
`realityloop`（RealityLoopAI/FieldRecognition）两个 GitHub remote；本次没有推送、提交、切分支或部署。
工作区开始时已有大量修改及未跟踪实现，本次基于这些实际文件增量修改，未回退或覆盖无关改动。
本地 `tmp/repair_baseline` 保存初始 diff/源文件副本和本轮测试日志，不加入 Git。

已检查 README、AgentTools、SQLite migrations、绑定/场景/权限接口、后台扫码、照片入口、任务队列、
面板定位、OCR 与候选合并、标准读数、归档发布及前端。未访问生产数据库、现场 NAS、线上服务、报告 F:/Z: 路径或原图；
现场报告属于提供的观察材料，不是本次重测结果。旧 `8217193d` 文件行号未作为当前实现依据。

## 真实数据关系与变更

| 对象/字段 | 当前确认的语义与本次规则 |
| --- | --- |
| `users` | 稳定人员记录；取消相机资格不删除人员或其历史。 |
| `camera_users` | 相机可使用人员名单。当前已有 v12 `(camera_id,user_id)` 联合主键；本次保留并补登记/选择接口与验证。 |
| `camera_active_users` | 明确选择的当前相机使用人。取消首位名单成员自动变成当前人的触发器。v12 迁移只继承原唯一人员，不在多人中选 MIN。已存在明确选择保留。 |
| `operator` / `wearer_id` | 沿用当前产品语义：实际人员的显示名 / 稳定人员 ID，均为单值。托管模式必须为该相机登记人员，同名要求 ID；不把整个人员名单当读数责任人。 |
| `bindings` | 一台相机可以同时关联多台仪器；同一仪器/仪器码活动占用仍独占。每条绑定现在是不可改名的使用时段，交接关闭旧行并创建后继行。 |
| `scene_visits` | 每台相机独立的场景访问；一个场景码允许任意多台获授权相机同时关联。退出一台不结束其他相机。场景码与仪器占用规则分开。 |
| `binding_policy_version` | `daily-qr-binding/1` 仍表达北京时间每日二维码授权，到次日零点失效，不表示当天禁止换人。 |
| `session_policy_version` | 新增 `binding-session/2` 表达人员使用时段；新 `session_revision`、前后继 ID 保留链路。时段为 `[started_at,ended_at)`。 |
| `binding_action` | 检查开始时本地字段见于 `live_scan` 的设备服务变更回执，不能据此推定线上绑定字段语义。本次绑定返回新增 `created/handover/updated/refresh`，记录创建本时段的动作；重复调用返回原时段/请求回执。 |
| `binding_session_audit` | 事务内追加旧/新时段、相机、人员和原因；禁止 UPDATE/DELETE。旧绑定人员保持原值，只补结束时间和后继引用。 |
| `binding_change_requests` | 保存请求内容指纹及原响应；同请求重复不再交接，内容/身份改变返回冲突。 |
| `camera_session_state` | 持久人员会话 revision；事务内检查 `expected_revision`。绑定接口另支持 `expected_binding_id`。 |
| 任务快照及允许名单 | `readout_context.at_capture` 按来源采集时间解析历史绑定，`app.enqueue_ocr` 固化人员、相机、仪器、时段及规则。`predict_readout` 从该快照取得 allowed IDs，不读取处理完成时的当前人员。 |

人员交接作用于相机的所有活动仪器和场景时段，保留多仪器能力；增加/刷新某台仪器仍只更新其内容。
`action=refresh` 明确建立新时段并要求 request_id；普通同人、同仪器、同内容重扫幂等。
仪器配置或场景凭证变化也比较实际内容，不能仅按 instrument_id 提前返回。
跨相机转交一台已占用仪器仍使用既有双方确认流程，不改变该授权边界。

请求/会话操作采用 SQLite `BEGIN IMMEDIATE`，关闭旧行、创建新行和审计一起提交；失败整体回滚。
request_id 重试可在后续再次交接之后返回原回执且不切回旧人。无 request_id 的旧绑定调用对同一 scan、
配置、人员的交接也保存确定性重试回执；有意再次使用相同扫码信息切回旧人应传新的 request_id 或重新扫码。
旧回执不是实时授权，当前状态应重新查询。未提供版本条件的不同请求按数据库提交顺序执行；要拒绝陈旧意图需传版本条件。
后台扫码携带实际 wearer_id，人员改变后刷新会话；事务内拒绝陈旧自动扫码恢复旧人员，包括当前没有仪器绑定时。

可信来源时间限于带时区的采集回执、已有照片目录时间契约或明确同步的视频帧时间；来源真实性沿用现有设备/本地访问边界，
没有新增硬件签名认证。仅接收时间的上传标记 `attribution_status=needs_review/capture_time_unverified`，人员不静默取当前值，
正式字段不借其当前绑定生成。缺历史绑定、历史归属重叠分别标记原因；已有人员登记历史可独立提供当时人员信息，仍不制造仪器绑定。
旧 `operator_history` 继续按采集时间回溯读取，不重写旧任务/原始回执。跨日失效依据 Asia/Shanghai，晚到任务保留自己的旧快照。

## 现场现象、已确认原因、修复和剩余门槛

| 项目 | 现场观察 / 当前代码确认 | 修改文件与关键逻辑 | 本地证据及现场门槛 |
| --- | --- | --- | --- |
| A 多人登记 | 报告旧表单人；当前已有 v12 多人结构，但新增成员触发默认当前人，名单/选择接口不完整。 | `database.py` v14、`security.py` 独立登记与选择、`static/app.js`/`index.html` 明确人员选择；`migrate_db.py` 预检备份恢复。 | 临时旧库/权限/重复登记/删除资格保历史测试；生产副本迁移尚未演练。 |
| B 当天交接 | 当前已有换人能力，但改写原绑定行，依赖 operator_history；缺持久请求幂等及独立人员时段。 | `binding_operator.py` 事务内后继时段和审计；`app.py` 内容比较、版本检查、重试；`automation.py`/`live_scan.py` 人员 ID 与会话更新；`readout_context.py`/`saved_photo.py` 历史来源。 | 覆盖同日甲→乙、多仪器、并发竞争、失败回滚、迟到照片/排队后换人、旧请求回放、跨日；真实设备仍需验。 |
| 共享场景 | 用户观察 A 进入后 B 失败；当前 `scene_visits` 已按 camera 独立，未发现全局场景独占。 | 保留共享机制；`scene_binding.py` 同步人员时段/ID；`agent_tools.py` 更正旧描述；新增共享场景回归。 | 实际 QR 样例解码、A/B 并发、重复幂等、A退出B保留、混合场景成功/仪器冲突独立，5 项回归通过。现场现象未复现，需要实际 HTTP detail、相机授权、扫码类型及混合码错误。 |
| C 多屏/同值候选 | 当前已有 OCR 前绑定过滤和跨图同值保护。确认另一问题：同帧同区域重复同值因候选数不是 1 被清空；全图 OCR/人工修订存在归属旁路。 | `measurement_records.py` 同值去重保精度/冲突证据；`reading_results.py`、`photo_measurements.py`、`archive_events.py` 校验区域与任务绑定，不借唯一名单归属；跨时段不合票。 | 合成电脑/目标、同值 clear/medium、真实数字冲突、未授权修订/跨时段回归。不能断言现场旧空值由此路径造成。 |
| D 搅拌器 | 报告允许名单只有天平，不能直接称 OCR 失败。当前已有双字段/OFF及绑定前过滤；过滤后零框缺完整分层诊断，OFF 草稿丢失状态。 | `panel_detector.py`/`panel_recovery.py`/`panel_regions.py` 分层数量和原因；标准结果/草稿保留 OFF 原文、单位依据和字段缺失；`static/photo-measurements.js` 显示不完整。 | 正确/错误绑定控制、63/1140合成输入契约、OFF/缺字段/单位未知测试。原图检测、红色数字裁剪/预处理准确率 NOT_PROVEN；未改模型或硬编码答案。 |
| E 可见延迟/发布 | 原报告仅测 Evidence，可见耗时不是 OCR 推理耗时。当前已有单文件临时更名；增加组终态及多文件一致性协议。 | `readout_timing.py`、`panel_readout.py` 单调计时；归档分阶段、Result先发布、Evidence哈希清单；`scripts/measure_publication_latency.py` 分组/任务及两种文件独立计时。 | 临时并发读JSON/失败发布/终态与统计测试。CLI无服务试跑诚实超时。尚未测现场NAS rename/缓存/新的端到端基线，未设产品性能门槛。 |

详细识别与单位统计边界见 [RecognitionRepairNotes.md](RecognitionRepairNotes.md)；
发布协议、计时定义与复测脚本见 [PublicationRepairNotes.md](PublicationRepairNotes.md)。
保留 raw text、候选、来源帧/区域和原始数值字符串；Result 的 number 不能表达尾零，显示精度由 Evidence/reading/display_value 承载。
未将 needs_review 全部改成功。确认部分指标不等于整个搅拌器面板完整。

## 接口兼容与部署前提

HTTP 原路径及 Agent 工具保留，输入新增字段可选；输出新增追溯/诊断字段。兼容语义变化必须告知调用方：

- 换人返回新的 binding_id，旧 ID 是历史时段；调用方应使用返回值或查询当前状态，不能假设同日 ID 不变。
- `/members` 只登记资格；旧 `/owner` 仍是明确的登记并切换操作。托管模式不接受无登记或有歧义的姓名；免登录本地模式未新增认证保证。
- 新任务缺可信采集时间不会再作为已确认人员/仪器结果，原始 OCR 保留；补时间的人工操作不制造缺失的区域/绑定证据。
- Result 六字段保留（OFF 可选 display_state 为当前已有扩展）；Evidence 新增诊断、终态与哈希清单。旧消费者忽略扩展，新消费者核对清单并停止等待合法跳过/失败。
- `migrate_db.py --status/--dry-run/--check` 现在输出 JSON；修改数据库必须显式 `--db` 且先备份。部署需要 v14，停止全部写入者并在隔离副本演练；脚本不替用户停止服务。
- 单文件原子更名依赖存储后端支持；多个文件不会同时原子发布。读者依据 Evidence 清单逐文件解析与 SHA-256 校验。

人员 API 参数和流程见 [AgentTools.md](../AgentTools.md#2026-09-20多人登记使用时段与共享场景)。
迁移预检、备份恢复及多人表不能无损降级的说明见 [BindingMigration.md](BindingMigration.md)。
本次未修改生产数据库，也未运行生产归档重建、部署或推送。

## 回归覆盖与实际执行

所有测试使用本项目 `.venv`（`sys.prefix=/home/x1/Projects/FieldRecognition/.venv`），临时 SQLite/目录和模型替身；
未针对生产 Data 测试。使用样例二维码的解码测试证明二维码流程，合成面板/模型替身不能证明现场仪器准确率。

| 验收场景 | 回归文件 |
| --- | --- |
| 1 多人/重复登记；2 旧库历史保留 | `test_camera_membership_repair.py`、`test_migrate_db.py`、`test_binding_operator.py` |
| 3 重扫幂等；4 同日交接；5 重试/并发/事务失败 | `test_binding_session_repair.py`、`test_automation.py` |
| 6 交接前拍/交接后处理；7 跨日历史；8 多仪器 | `test_binding_session_repair.py`、`test_binding_policy.py`、`test_device_binding.py` |
| 9 非目标屏幕；10 同值清晰度/真实冲突 | `test_recognition_repairs.py`、`test_publication_repair.py`、`test_multi_readout.py` |
| 11 搅拌器两组控制；12 双字段/单位/OFF/缺失 | `test_recognition_repairs.py`、`test_measurement_records.py`、`test_panel_regions.py` |
| 13 原子JSON/失败/跳过；14 Evidence/Result与组/任务分母 | `test_publication_repair.py`、`test_archive_store.py`、`test_readout_timing.py` |
| 多台相机共享场景，权限与仪器独占仍保持 | `test_shared_scene_repair.py` |

```bash
env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  .venv/bin/python -m pytest -q tests
node --check static/app.js
node --check static/photo-measurements.js
git diff --check
```

全量中间运行实测 451 passed / 2 个 Starlette/AnyIO 依赖弃用警告。最终运行记录在本文件结尾；聚焦测试集合有重叠，不能相加计数。
`compileall` 对本轮改动 Python 逐文件执行；不导入应用或触碰生产数据库。

## 现场复测入口与判定

1. 按迁移文档在**隔离副本**执行 `--dry-run/--check`、备份、迁移、恢复演练，核对旧人员/绑定/读数和外键。
2. 在独立测试实例登记甲乙到相机 A，仅添加名单时确认 active 不变。明确选甲，解码仪器码并同时关联两台仪器；保存一次带可信采集时间的照片任务。
3. 使用新的 request_id 和当前 expected_revision 一次切乙；重复原请求、提交两个并发旧版本请求，核对一个冲突或串行独立时段，旧任务仍甲、新任务乙。实际时段不跨北京时间零点续权。
4. 相机 A/B 分别实际解码**场景**码，同时进入；退出 A 后查看 B 保留。若同帧有已占**仪器**码，分别查看 scene_visit 和 binding_errors。不要混淆“场景登记”与“仪器独占”。
5. 用授权只读原图副本做天平限定/搅拌器正确绑定控制，先核对快照名单再检查诊断各阶段、crop、raw text 与正式字段。运行只读核对：

   ```bash
   .venv/bin/python scripts/audit_recognition_evidence.py /path/to/local-copied/Evidence.json
   ```

6. 使用 [发布复测步骤](PublicationRepairNotes.md#隔离现场复测) 的隔离根脚本，逐组三图全部写完为 t0；分别记录 Evidence/Result 首次解析、组终态和任务结果。未指定性能目标时只报告实测基线，不宣称达标。

`PROVEN` 限于文档列出的本地事务、解析、过滤、归档和统计行为；
`PARTIAL_EVIDENCE` 为合成输入支持链路和结果契约；
`NOT_PROVEN` 包括真实 63 °C/1140 rpm 图片、现场多屏误检率、线上多人/场景现象、生产数据兼容性、NAS可见延迟和长期稳定性。
缺失门槛是获授权的真实原图/原回执、线上版本与请求错误、隔离生产副本，以及实际存储后端复测；本次没有用模拟通过替代这些验收。
