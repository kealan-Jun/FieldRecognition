# 三连拍可用候选汇总修复

## 行为

同一实验、仪器及有效绑定时段内，三连拍用于提高获得清晰照片的机会。按候选的数字、单位、量程与显示精度校验结果筛选可用候选：至少一个可用候选，且所有可用候选一致，可以形成测量草稿。不可读或已被判为异常的 OCR 候选保留在 original_candidates，作为 excluded_candidates 和 recognition_warnings 展示来源，不再独立否决整组。

仍阻塞的情况：没有可用候选；可用候选的数值、单位或 OFF 状态有冲突；二维码、面板身份、绑定或人员时段冲突；缺少实验引用或采集时间；照片未收齐/识别未结束；其他既有确认检查不通过。筛选不会修正原始数字、推定身份、自动确认或自动写表。

规则版本：photo-measurement/4。既有原始 OCR、图片哈希、版本和人员归属保持原样。

## 旧草稿重校验

新增 `POST /api/photo-measurements/{measurement_id}/revalidate`，请求与既有 Decision 一致：`revision` 和 `actor`。仅 admin/reviewer 可执行，设备凭证不可执行，继续使用原相机访问检查。

在单个 BEGIN IMMEDIATE 事务中检查版本和 draft 状态，基于草稿已经保存的 original_candidates 重算汇总，不读取新图片、不重新调用模型。已有人工或有审计的字段修订保持原样。validation_history 记录原字段、原阻塞、规则版本、调用人和基准版本。过期版本和已确认记录返回 409。不会创建 experiment_records。

## 当前现场样本

2026-09-25 17:43:11，lubancat-0282f88a_cam02，measurement `2aa98963-2eb9-47bf-b8e5-63022e108b36`：三张可用候选均为 0.7710 g；视觉模型另外两个候选分别为 9.1710、97.10，已被上游标记 digit_sequence_conflict。

基于原草稿审计快照离线验证：新规则保留全部五个候选，三个有效图片支持 0.7710 g，两个异常候选留存，blockers 为空，仍是未确认草稿。这不代表现场已经切换规则。

核对时现场另一个 Codex 已于 17:52:58 提交有原因记录的草稿修订（revision 13），当前该组 blockers 已为空；本修复不会覆盖该修订，也不将其说成人工确认。

## 部署状态与限制

基于目标机 main `30c75c9`，在独立分支 `fix/burst-valid-candidates-20260925`、目录 `/srv/sentinel-data/fieldrecognition-burst-fix-20260925` 开发测试。现场主目录中的实验关联补丁保持原样，未停止服务，未替换现场业务代码。

目标项目 AGENTS.md 明确要求："Do not stop or restart the live API, camera/scanner, OCR or archive services to deploy an iteration." 当前没有验证过的不间断替换机制。此次运行代码修复保持暂存；上线前需要明确的维护时段，识别暂停以及可能需要重新扫码绑定的影响必须说明。不得通过修改现场加载代码或直接重启绕过升级限制。

上线时保留当日实验关联的 source_io.py、photo_experiment_context.py 与 Runtime 配置，不能直接用干净基线覆盖主目录。不要覆盖其他并行人员的新改动。旧草稿通过新 revalidate 接口逐条带版本号重校验，不能通过 SQL 清空 blockers 或将读数伪装为人工确认。

这次修复只处理候选校验与旧草稿重校验，不完成 AgentDesk 原生草稿到飞书写入的独立桥接。

## 测试结果

在隔离工作区与临时数据库运行全套 584 项测试：579 项首轮通过，5 项因 SSH 会话 PATH 找不到 Node 失败；补上目标机现有 Node 路径后，仅复测这 5 项，全部通过。没有为测试重启生产服务或调用真实 GPU 推理。新增 14 个场景覆盖一张/两张坏图、同图多路候选、全部坏图、有效值冲突、身份冲突、重校验审计/版本竞争、已确认不可改、保留已修订字段与权限拒绝。Python 编译和 diff whitespace 检查通过。

测试命令：`env -u FIELD_DATABASE_PATH -u FIELD_DEMO_DATA -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL PATH=/srv/sentinel-data/agentdesk-runtime/node-v26.8.1-linux-x64/bin:/usr/bin:/bin OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 /home/x1/Projects/FieldRecognition/.venv/bin/python -m pytest -q tests`。
