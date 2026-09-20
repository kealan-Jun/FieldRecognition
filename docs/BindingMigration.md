# 人员登记与交接数据库迁移

本说明对应 SQLite 迁移脚本 [migrate_db.py](../migrate_db.py) 和
[database.py](../database.py)。本次仅在临时数据库中演练；没有读取或修改生产数据库、NAS 原件。
迁移的运行前提是停写窗口，运行数据库仍须位于本地受支持的 SQLite 文件系统。

## 数据关系与兼容性

- `camera_users(camera_id,user_id)` 是相机的可使用人员名单，v12 使用联合主键防止重复登记。
  相机与被识别的仪器是不同对象；这张表不限制相机可同时关联的仪器数量。
- `camera_active_users(camera_id,user_id)` 表达明确选择的当前使用人，不能用名单第一人代替。
  旧单人数据可保留原人员选择；多人数据中没有可靠选择依据时应要求明确选择。
- v14 取消新增名单成员时自动选择当前使用人的触发器，并为交接增加请求幂等与会话审计表。
  历史绑定与任务中的实际人员快照不能由名单变动覆写。
- 撤销名单资格不是删除用户或历史实验记录。名单迁移不重写原始绑定、任务、照片、二维码、时间或已归档回执。
- v12 之前的单人主键结构不能保存多人名单。迁移只提供向前升级；脚本没有破坏性的 `down` 操作。

脚本的只读命令保留原默认路径，但建议始终显式传 `--db`。写入必须显式指定 `--db`，且目标数据库必须已经存在。
该脚本用于迁移既有数据库；新实例初始化沿用项目部署入口。`--status`、`--dry-run`、`--check` 输出 JSON，
检查结果和待迁移版本均可保存为回执；依赖旧终端文字的运维解析器需要更新。

## 迁移前检查及备份

先在隔离副本上演练。正式操作须由部署人员安排 API、相机监控、OCR、归档工作进程及其他 SQLite 写入者全部停写；
本脚本不会自动停服务或操作生产环境。SQLite backup API 能获取包含已提交 WAL 数据的一致快照，
但备份与迁移不是覆盖其他进程的一个事务，不能替代停写窗口。

以下变量必须由部署人员填入明确的本地数据库和备份路径，目录需事先存在：

```bash
FIELD_MIGRATION_DB=/absolute/path/to/isolated/State.sqlite3
FIELD_MIGRATION_BACKUP=/absolute/path/to/backups/State-before-binding.sqlite3

.venv/bin/python migrate_db.py --db "$FIELD_MIGRATION_DB" --dry-run
.venv/bin/python migrate_db.py --db "$FIELD_MIGRATION_DB" --check
.venv/bin/python migrate_db.py --db "$FIELD_MIGRATION_DB" --backup "$FIELD_MIGRATION_BACKUP"
.venv/bin/python migrate_db.py --db "$FIELD_MIGRATION_DB" --check
```

`--dry-run` 和 `--check` 用 SQLite `mode=ro`、`query_only` 连接，不创建缺失数据库或父目录，不执行迁移。
检查包括 `PRAGMA integrity_check`、`PRAGMA foreign_key_check`、未知迁移版本、关键表行数以及多人相机数量。
失败时返回非零退出码；不得通过删除异常历史或关闭外键来绕过检查。
`--status` 对不存在的路径仍可返回待迁移版本，但明确标记 `database_missing`；其他操作对此返回失败。

实际迁移在写入前：

1. 验证源数据库完整性及外键，拒绝当前代码不认识的迁移版本。
2. 使用 SQLite backup API 创建备份，再验证备份完整性及外键；权限设为 `0600`。
3. 在标准错误输出确认备份绝对路径，再调用项目现有事务迁移机制。
4. 迁移成功后再次检查完整性及外键，并输出迁移前后版本、行数和备份位置。

`--backup` 指向现有文件或符号链接时拒绝覆盖。省略它时在数据库旁生成包含 UTC 时间和唯一后缀的备份。
如果没有待执行迁移，不进行写入，也不额外创建备份。备份空间不足、目录不可写或检查失败时不得继续迁移。
保留备份及检查回执在受控的运行资料目录，避免加入 Git；备份包含人员与认证数据库数据，应按数据库本身的权限保护。

## 恢复与旧版本回退

迁移失败时先保持停写，保留失败库及日志；脚本报告备份位置，不自动覆盖恢复。
恢复只能写入**不存在的新文件**，验证后再由部署人员选择配置切换：

```bash
FIELD_MIGRATION_RESTORED=/absolute/path/to/isolated/State-restored.sqlite3
.venv/bin/python migrate_db.py --restore-from "$FIELD_MIGRATION_BACKUP" --db "$FIELD_MIGRATION_RESTORED"
.venv/bin/python migrate_db.py --db "$FIELD_MIGRATION_RESTORED" --check
```

恢复同样使用 SQLite backup API，并检查源备份和恢复库。已存在的目标文件会被拒绝，避免覆盖正在使用的库。
恢复备份得到的是备份时刻的状态，无法包含备份后发生的新登记、交接、任务或实验记录。
若升级后已产生新数据，先停写并完整备份升级库；不能直接用旧备份替换并宣布无损回退。
数据库副本不包含图片和 NAS 归档文件；这些原始材料应原样保留，并在恢复后核对引用。

尤其不能把多人 `camera_users` 改回 `camera_id PRIMARY KEY` 后任取第一人、`MIN(user_id)`、`LIMIT 1` 或删除其余关系。
单人旧结构没有能力表达全部关系，新会话/幂等/审计记录也可能无法被旧程序理解。
如必须恢复旧程序，应保留完整升级库、完整多人关系及新审计数据的受控副本，先在隔离环境定义明确的数据保留和转换方案，
再决定哪些业务能力暂时不可用。当前脚本不提供有损降级，不通过删表让旧程序启动。

## 可执行的隔离回归演练

以下命令仅使用 pytest 创建的临时目录及合成 SQLite 数据，不连接相机、生产数据库或 NAS：

```bash
env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL \
  OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  .venv/bin/python -m pytest -q tests/test_migrate_db.py
.venv/bin/python -m compileall -q migrate_db.py tests/test_migrate_db.py
```

演练覆盖：旧单人表迁移、两人登记和重复登记、历史绑定与读数字节保留、SQLite WAL 备份、恢复到新路径、
只读检查不创建文件、外键异常、未知版本、损坏数据库、已有备份防覆盖，以及迁移中注入异常后的整体事务回滚。
生产副本的检查还需要核对当前使用人是否有明确选择依据、历史人员快照、绑定边界与交接请求审计。
本地数据库测试为 `PROVEN` 的只是这些事务与数据保留行为；生产实际数据兼容性、真实交接流程和图片识别仍需现场验证。
