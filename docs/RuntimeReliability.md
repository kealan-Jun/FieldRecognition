# 启动恢复、升级与失败任务（2026-09-20）

本轮处理两种实际故障：开机时 `RealityLoop.local` 暂不可解析，NAS 挂载依赖失败，
独立 OCR API 未启动；以及业务代码要求 v14、运行数据库仍为 v13，工作进程反复退出。
临时恢复已完成。以下机制用于减少相同故障再次需要人工处理的情况。

## NAS 恢复后的 OCR 接口补启动

这是上述 2026-09-20 部署中已有独立 OCR 服务的可选恢复机制，不是首次克隆或 CPU 上传的安装前提。服务名与原部署有关；其他环境不能照抄启用，先核对实际服务归属并取得修改该服务的授权。

`deployment/system/field-recognition-ocr-recovery.timer` 开机 45 秒开始检查，此后每 30 秒运行一次。
它仅在现有 `realityloop-ocr-api.service` 已启用且处于 inactive/failed 时提交启动请求。
挂载的等待和错误仍由原 API 单元管理；挂载失败后下次定时检查再次尝试。
不修改 NAS 地址、共享权限、GPU 调度、模型或其他推理服务。正在运行或启动中的 API 不会被反复重启。

安装时在仓库根目录执行以下命令。系统服务执行的是 root 拥有的独立脚本副本，
使用隔离 Python，不导入用户工作区代码，也不读取 OCR 密钥。

```bash
sudo install -d -o root -g root -m 0755 /etc/field-recognition
sudo install -D -o root -g root -m 0644 scripts/recover_ocr_api.py /usr/local/lib/field-recognition/recover_ocr_api.py
sudo install -o root -g root -m 0644 deployment/system/field-recognition-ocr-recovery.service deployment/system/field-recognition-ocr-recovery.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now field-recognition-ocr-recovery.timer
```

维护时先 `sudo touch /etc/field-recognition/ocr-recovery.paused`，再停止 OCR API，
避免被补启动。维护结束删除这个暂停标记即可。禁用原 API 单元也会使恢复检查跳过。
可用 `journalctl -u field-recognition-ocr-recovery.service` 查看 start_requested、active、paused 等结果。
该定时器恢复 API 入口；GPU 非服务时段仍按原调度排队，不被视为需要强行启动 GPU。

## 启动前数据库检查与迁移

四个业务服务均 Requires/After `field-recognition-prepare.service`。
该准备单元先检查读数模块能否导入及数据库完整性，存在待迁移版本时：

1. 确认四个业务服务没有运行进程；其他手动启动的数据库写入程序也应先停止。
2. 用 SQLite backup API 创建隔离副本，执行迁移与备份恢复演练。
3. 校验历史绑定、任务、扫码、场景和正式记录的文档哈希保持一致。
4. 为正式库创建独立的 `0600` 备份，事务迁移并再次核对历史数据。
5. 将迁移回执保存在数据库旁的 `Runtime/Migration-*.json`，成功后才允许业务服务启动。

任何预检、导入、演练或迁移失败都会阻止写入服务启动，备份保留，不自动覆盖回退正式库。
未知版本、外键异常等必须修复原因；不能通过删记录或反复启动绕过。
准备单元使用 RemainAfterExit；代码更新后应走下面的升级入口，不能只重启某一个工作进程。

## 升级入口

**2026-09-21 用户要求：日常迭代不得中断正常使用。** 旧入口会先停掉 API、相机、OCR、归档服务，
既会留下扫码空窗，也会按采集会话规则结束绑定。现已移除升级入口自动停止服务的行为。

服务直接使用部署目录中的 Python 文件。开发、回归和提交都在独立 worktree 完成，
测试使用临时数据。提交完成不等于允许立即上线；不能直接覆盖正在运行的源文件，
也不能用 `systemctl restart`、安装脚本或直接 Git 更新绕过升级检查。

```bash
# 当前版本的只读检查（存在待迁移时返回非零）
.venv/bin/python scripts/upgrade_runtime.py

# 检查待部署提交的文件范围；不更新代码、不调用服务启停
.venv/bin/python scripts/upgrade_runtime.py --revision <已测试的本地提交SHA>

# 按检查结果应用；运行中涉及运行代码的变更会直接拒绝，不停服务
.venv/bin/python scripts/upgrade_runtime.py --apply --revision <已测试的本地提交SHA>
```

入口区分以下情况：

- 没有文件变化：不更改代码，不重启服务。
- 仅文档、测试、独立升级工具变化：快进到已验证提交，不执行迁移、安装单元或服务启停。允许范围为 `AGENTS.md`、`README.md`、`AgentTools.md`、`docs/` 下 Markdown、`tests/` 和 `scripts/upgrade_runtime.py`。
- 其他变化：包括 Python 业务代码、网页资源、依赖、配置和 systemd 单元。若本项目目标或服务仍在运行、启动、停止中，或无法确认状态，直接拒绝应用。即使暂时没有有效绑定或待处理任务，也不能据此停止服务，因为设备可能随时扫码。
- 本来就离线的部署：确认目标、准备服务、旧单进程服务及四个业务服务已停止后，才快进代码、演练/迁移、安装单元和启动。不得为了日常迭代主动停服务来满足这个前置条件。

失败会给出非零退出码；运行中被拒绝的升级不触碰代码和服务。离线迁移失败时不继续启动服务，也不将旧库覆盖到新记录上。
部署回执保存在 `Data/Runtime/Upgrade-*.json`（自定义数据路径时使用该路径）。
首次引入本防护可从已测试 worktree 调用，传 `--project-root` 指向实际部署根目录；
本次变更本身仅涉及上述允许范围，业务进程无需重启。部署前后记录各服务的 PID、InvocationID 和就绪状态，核实没有由更新引起的服务重启。

**当前边界：这是避免迭代中断服务的强制防护，并未实现运行代码的无缝替换。**
服务共用源目录和数据库，采集进程退出还会结束绑定，不能直接双开写入进程充当滚动升级。
后续运行代码若要在线发布，需要先实现并验证独立版本运行、单一采集/写入归属、持久化接收与任务续接、
兼容数据库变更和失败切回；以升级期间真实扫码持续生成绑定、识别信息和既有 NAS 回执作为验收门槛。
未完成这些验证前，运行代码留在开发工作区，不执行会打断使用的发布。

## 失败任务和正式记录

`POST /api/jobs/{job_id}/reprocess` 现在也接受 failed 任务，创建新的识别版本。
原任务、原错误、原照片哈希及历史归属不改写；归属待确认状态随新版本保留。
相同 request_id 重试复用新版本，不重复创建。网页失败任务提供同一入口。
历史失败数可能仍包含原版本，应结合 `/versions` 的当前版本判断是否已恢复。
启动前加载实际读数模块；运行中若仍遇到 ImportError/ModuleNotFoundError，任务持久化等待，
不消耗照片重试次数。工作进程捕获的异常仅保存类型和代码位置，不保存可能包含凭证的异常消息。
依赖修复后继续领取原任务；这不会将输入错误或读不清的图片自动标记为正确读数。

设置 `FIELD_RECORD_MODE=production` 并受控重启后，新照片生成待确认草稿，
人工确认后才产生正式实验记录。旧 test 记录和对旧任务的重新识别保持原模式。
没有绑定、缺少可信拍摄时间、单位冲突或无法看清的字段继续待复核，不能为清空失败数编造读数。

## 验证范围

`tests/runtime/test_runtime_reliability.py` 使用临时数据库和替代 systemctl，覆盖依赖失败后的重复尝试、
维护暂停、禁用服务、迁移演练失败阻断、活跃写入者阻断、历史哈希保留、运行中升级拒绝、无变化不重启、
文档更新不调用服务管理，以及离线升级顺序。
真实运行验证回执保存在本机 `Verification/ReliabilityRepair20260920/`，不进入 Git。

自动恢复有前提：NAS 最终恢复可达、账户权限有效、磁盘可写、配置和模型有效。
不能保证设备永不故障，也不能将接口恢复或历史样本识别成功等同于所有真实仪器准确率。
整机断电重启、实际断网恢复和长期识别准确率分别需要对应现场证据。
