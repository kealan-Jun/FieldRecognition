# 启动恢复、升级与失败任务（2026-09-20）

本轮处理两种实际故障：开机时 `RealityLoop.local` 暂不可解析，NAS 挂载依赖失败，
独立 OCR API 未启动；以及业务代码要求 v14、运行数据库仍为 v13，工作进程反复退出。
临时恢复已完成。以下机制用于减少相同故障再次需要人工处理的情况。

## NAS 恢复后的 OCR 接口补启动

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

服务直接使用部署目录中的 Python 文件。不要在运行中的部署目录边改代码边运行；
先在独立 worktree 修改、运行测试、提交，再使用本入口停写后快进到已验证提交。

```bash
# 当前版本的只读检查（存在待迁移时返回非零）
.venv/bin/python scripts/upgrade_runtime.py

# 完整升级；目标必须是当前提交的后继，工作区必须干净
.venv/bin/python scripts/upgrade_runtime.py --apply --revision <已测试的本地提交SHA>
```

升级依次执行停止本项目服务、核对进程退出、快进代码、数据库演练/迁移、安装用户单元、启动和健康检查。
失败会给出非零退出码；迁移失败时不继续启动服务，也不将旧库覆盖到新记录上。
部署回执保存在 `Data/Runtime/Upgrade-*.json`（自定义数据路径时使用该路径）。
首次引入该脚本可从已测试 worktree 调用，传 `--project-root` 指向实际部署根目录。
这不是对任何直接 `git pull`、人工改库或磁盘故障的无条件保护。

## 失败任务和正式记录

`POST /api/jobs/{job_id}/reprocess` 现在也接受 failed 任务，创建新的识别版本。
原任务、原错误、原照片哈希及历史归属不改写；归属待确认状态随新版本保留。
相同 request_id 重试复用新版本，不重复创建。网页失败任务提供同一入口。
历史失败数可能仍包含原版本，应结合 `/versions` 的当前版本判断是否已恢复。

设置 `FIELD_RECORD_MODE=production` 并受控重启后，新照片生成待确认草稿，
人工确认后才产生正式实验记录。旧 test 记录和对旧任务的重新识别保持原模式。
没有绑定、缺少可信拍摄时间、单位冲突或无法看清的字段继续待复核，不能为清空失败数编造读数。

## 验证范围

`tests/test_runtime_reliability.py` 使用临时数据库和替代 systemctl，覆盖依赖失败后的重复尝试、
维护暂停、禁用服务、迁移演练失败阻断、活跃写入者阻断、历史哈希保留及升级顺序。
真实运行验证回执保存在本机 `Verification/ReliabilityRepair20260920/`，不进入 Git。

自动恢复有前提：NAS 最终恢复可达、账户权限有效、磁盘可写、配置和模型有效。
不能保证设备永不故障，也不能将接口恢复或历史样本识别成功等同于所有真实仪器准确率。
整机断电重启、实际断网恢复和长期识别准确率分别需要对应现场证据。
