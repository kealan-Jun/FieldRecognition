# 本机托管运行与 NAS 归档

本机运行 API、相机监控、GPU OCR、NAS 归档四个独立服务。NAS 只提供文件共享；原采集目录只读，不安装服务。`FIELD_PRODUCTION_ENABLED=1` 启用服务拆分和持久化任务队列；`FIELD_RECORD_MODE=production` 才启用正式业务草稿确认。两个开关含义不同，测试结果不会自动成为正式记录。

## 服务与入口

| 单元 | 职责 |
|---|---|
| `field-recognition-api.service` | HTTP、网页、Agent 工具；默认免登录；不加载模型或连接相机 |
| `field-recognition-cameras.service` | 从登记表读取已登记使用人的相机；每台相机一个监控进程 |
| `field-recognition-ocr.service` | 统一 GPU 模型所有权；照片任务优先、按相机公平取任务；最多四个照片流程等待，模型串行 |
| `field-recognition-archive.service` | 消费本地归档队列、同步 NAS、生成索引与完整性巡检 |
| `field-recognition.target` | 一起启动、停止上述服务 |

进程通过本机权限为 `0600` 的 Unix socket 通信，不开放额外网络端口。API 默认只监听 `127.0.0.1:8188`。打开 `http://127.0.0.1:8188/` 即可使用；旧 `/login` 地址会返回工作台。

迁移前使用 SQLite backup API 生成一致性备份。迁移只增加结构或保留原表副本，不删除照片、绑定、历史记录。先停止旧服务，避免旧进程与新任务消费者并行写入。

```bash
systemctl --user stop field-recognition-demo.service
.venv/bin/python prepare_runtime.py --data Data --username kealan --display-name 徐荣炜 --camera lubancat-52d2ef0c_cam01
.venv/bin/python scripts/install_runtime.py
systemctl --user status field-recognition.target
```

默认 `FIELD_AUTH_ENABLED=0`，网页、API 和 Agent 均无需登录。准备脚本登记使用人唯一 ID、姓名与相机关系，不生成密码；已有账号、绑定身份、原图和历史回执保留。实验员登记仍用于数据归属，免登录下的操作人是本机登记或请求声明的身份，不代表通过账号认证。

一台相机可以登记多名人员。`camera_users` 以 `(camera_id,user_id)` 为联合主键保留成员关系，`camera_active_users` 单独保存当前使用人，后台只为每台相机启动一个采集进程。重复运行准备脚本会追加成员并切换当前使用人；此前成员、绑定身份和二维码证据保留。切换时同步更新有效关系与后台实验员设置，并记录人员变更历史。

`PUT /api/admin/cameras/{camera_id}/owner` 提交 `{"user_id":"已登记人员ID"}`，追加成员并切换当前使用人；`GET /api/admin/cameras/{camera_id}/members` 查询成员；`DELETE /api/admin/cameras/{camera_id}/members/{user_id}` 移除非当前成员。默认本机免登录可调用，开启登录时要求管理员。相机成员都可在登录后访问该相机；设备凭证及后台采集使用当前使用人。成员姓名重复时，绑定或自动运行接口需提供明确的 `wearer_id` 才能归属到人员 ID。

二维码身份按活跃绑定独占：同一个二维码不能同时绑定到多个设备或相机。只有明确结束绑定（含交接交出）、当天凭证失效，或采集服务确认设备启动标识变化而结束旧绑定后，才允许重新分配给替换设备；二维码登记表会更新当前归属，旧绑定与原始扫码证据不会删除。

登录是独立的可选配置，托管模式或 `FIELD_RECORD_MODE=production` 均不会自动开启。新建需要账号认证的部署时，使用 `prepare_runtime.py --enable-login` 并设置 `FIELD_AUTH_ENABLED=1`；初始密码只写入权限为 `0600` 的 `Data/Access/InitialAccess.json`。仅在这种配置下启用会话、CSRF、相机账号隔离及管理员账号接口。

`/api/v1/` 与现有 `/api/` 执行相同业务规则。`GET /api/cameras` 列出已登记相机，后续请求使用 `X-Camera-Id` 选择相机。Agent 工具目录见 `GET /api/tools`，测量与交接工具直接调用同一业务事务。

## 确认、交接与恢复

生产 OCR 结果进入测量草稿。一次连拍由 `burst_id` 合并，同一来源的网络重传复用照片、任务和测量。校正保留原字段证据和修订版本；确认校验目标、冲突、版本后写入正式记录，并读回验证。`test` 模式仍仅生成测试产出。

同一相机当天内可直接更换操作人，无需先结束绑定。向 `POST /api/bindings` 提交同一仪器的有效扫码记录和新操作人时，原地更新有效绑定；保留 `binding_id`、开始时间、有效期、`qr_hash` 与原扫码证据，追加 `operator_history`，旧归档版本保留。全相机交接可通过当前使用人接口或自动运行设置更新全部有效关系。单次绑定仍记录一名明确操作人；相机的多人登记用于共享设备与人员切换。

已占用仪器不能由另一相机静默接管。接收人需实际扫码申请，原使用人明确交出后，接收人确认；请求跨过北京时间零点即失效，必须使用当天新的二维码证据。晚到照片按拍摄时间匹配历史绑定及人员变更历史，无法可靠归属时保持待处理，不改写旧归属。当天换人不延长北京时间次日零点的到期时间。

任务队列保存在本机 SQLite，包含租约、尝试次数、重试时间和失败历史。60 秒租约、15 秒续约；进程退出后过期任务可重新领取，旧租约不能覆盖新结果。暂时故障采用指数退避和抖动，最多初次加三次重试；耗尽进入失败队列，填写操作人 `actor` 和理由 `reason` 后重放（启用登录时使用管理员账号身份），正式确认记录不允许重放覆盖。相机与全局队列有容量上限。

NAS 断开或容量不足时暂停归档写入，本地事务队列保留；恢复后重试。归档写入校验源文件和目标哈希，先原件后回执，成功后才确认本地队列。NAS 错误不阻塞 API/GPU；不自动清理历史材料腾空间。

## NAS 目录

打开 `FieldRecognitionArchive/Readme.html` 查看中文入口。目录名称采用 PascalCase，记录 ID、日期和哈希沿用原始标识。

```text
FieldRecognitionArchive/
  Readme.html                   中文业务导航
  Browse/                       分类索引，只包含链接与摘要
    BindingEvents/              谁在何时绑定设备或场景
    DeviceHandoffs/              交接申请、交出与接收
    PanelReadings/               何时在哪台设备读到哪些指标
    VoicePhotoReadings/          原语音照片来源、时间与读数
    MeasurementDrafts/          连拍测量草稿及校正
    ExperimentRecords/          确认后的正式测量
    WorkbenchReadings/          实验台汇总，逐项保留仪器归属
    SourceMaterials/           照片及扫码凭证
  Records/<Entity>/<Id>/        每个业务实体的一份当前记录
    Record.json                指标、值、单位、图像和原回执引用
    Readme.html                同一记录的阅读页面
  Objects/<HashPrefix>/        内容寻址原件；分类不复制图片
  Receipts/                    不可变历史版本回执
  Integrity/                   完整性巡检报告
  Audit.html                   历史版本索引
  Index.json                   机器索引
  LegacyLinks.json             旧索引 URL 到新目录的映射
```

同一个任务同时出现在设备、语音照片、实验台分类中时，链接指向同一 `Records` 文件；同一哈希原件只存一份。`Records` 是方便阅读的当前视图，`Receipts` 是不可变版本证据，两者不能混为重复记录删除。旧版分类下的自动生成副本只在新视图已存在、旧文件具有已知生成标识时清理；未知文件、人工备注及原回执保留。旧 URL 在应用中继续映射至新位置。

## 运维与验证边界

更新代码统一使用 [运行可靠性说明](RuntimeReliability.md) 中的 `scripts/upgrade_runtime.py`。
先在独立 worktree 测试提交，部署时停止全部写入进程后才切换代码、演练迁移并备份。
安装新的用户单元后，四个服务在启动时都等待 `field-recognition-prepare.service` 成功。

- `/health/live` 检查 API 存活；`/health/ready` 检查数据库及必要工作进程心跳。相机画面在线与真实识别是否成功需查看状态及业务证据。
- `/api/admin/status` 查看进程、OCR、队列和归档；`/metrics` 输出队列与待归档数量；默认可在本机直接查看；启用登录时要求管理员身份。
- 升级后的本机服务重启不会主动解除仪器绑定。北京时间跨日、设备采集会话变化、显式交接或手动结束会结束当前关联；晚到照片保留拍摄时的历史归属。
- 原有模型 A/B 的明确资产映射继续使用；通用设备类型检测只能说明类型，不能仅凭“当前只有一台同类绑定”推断物理资产。
- 自动测试使用临时数据库，覆盖身份隔离、实际 API 到工作进程链路、事务、重传、租约、重试及归档去重；这些证据不代表现场设备识别准确率或整套生产上线验收。

回退前停止 `field-recognition.target` 并保存新产生的数据库和队列。不得直接用升级前备份覆盖已经产生新记录的数据库。需要旧程序应在隔离目录验证兼容后恢复运行，证据目录保持原位。
