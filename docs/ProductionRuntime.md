# 本机托管运行与 NAS 归档

本机运行 API、相机监控、GPU OCR、NAS 归档四个独立服务。NAS 只提供文件共享；原采集目录只读，不安装服务。`FIELD_PRODUCTION_ENABLED=1` 启用服务拆分、账号认证和持久化任务队列；`FIELD_RECORD_MODE=production` 才启用正式业务草稿确认。两个开关含义不同，测试结果不会自动成为正式记录。

## 服务与入口

| 单元 | 职责 |
|---|---|
| `field-recognition-api.service` | HTTP、网页、Agent 工具、账号与访问权限；不加载模型或连接相机 |
| `field-recognition-cameras.service` | 从登记表读取已分配账号的相机；每台相机一个监控进程 |
| `field-recognition-ocr.service` | 统一 GPU 模型所有权；照片任务优先、按相机公平取任务；最多四个照片流程等待，模型串行 |
| `field-recognition-archive.service` | 消费本地归档队列、同步 NAS、生成索引与完整性巡检 |
| `field-recognition.target` | 一起启动、停止上述服务 |

进程通过本机权限为 `0600` 的 Unix socket 通信，不开放额外网络端口。API 默认只监听 `127.0.0.1:8188`。跨机器接入须配置 HTTPS 反向代理；不直接暴露明文登录端口。

迁移前使用 SQLite backup API 生成一致性备份。迁移只增加结构或保留原表副本，不删除照片、绑定、历史记录。先停止旧服务，避免旧进程与新任务消费者并行写入。

```bash
systemctl --user stop field-recognition-demo.service
.venv/bin/python prepare_runtime.py --data Data --username kealan --display-name 徐荣炜 --camera lubancat-52d2ef0c_cam01
.venv/bin/python scripts/install_runtime.py
systemctl --user status field-recognition.target
```

首次创建管理员时，密码只写入权限为 `0600` 的 `Data/Access/InitialAccess.json`，不写入日志、Git 或文档。管理员在 `/login` 登录，后续创建操作员、审核员并分配相机。相机凭证不能进行人员交接或确认正式测量。禁用账号会立即撤销会话。

普通用户仅能访问分配相机的数据；审核员可校正、确认测量；管理员管理登记与队列重放。网页使用 HttpOnly 会话和 CSRF 校验；API/Agent 使用 `Authorization: Bearer <session>`。`/api/v1/` 与现有 `/api/` 执行相同端点和权限检查。`GET /api/cameras` 列出可见相机，后续请求使用 `X-Camera-Id` 选择相机。Agent 工具目录见 `GET /api/tools`，测量与交接工具直接调用同一业务事务。

## 确认、交接与恢复

生产 OCR 结果进入测量草稿。一次连拍由 `burst_id` 合并，同一来源的网络重传复用照片、任务和测量。校正保留原字段证据和修订版本；确认校验目标、冲突、版本后写入正式记录，并读回验证。`test` 模式仍仅生成测试产出。

已占用仪器不能由另一相机静默接管。接收人需实际扫码申请，原使用人明确交出后，接收人确认；请求 24 小时未完成失效。相机账号重新分配前必须结束现有绑定。晚到照片按拍摄时间匹配历史绑定，无法可靠归属时保持待处理，不改写旧归属。

任务队列保存在本机 SQLite，包含租约、尝试次数、重试时间和失败历史。60 秒租约、15 秒续约；进程退出后过期任务可重新领取，旧租约不能覆盖新结果。暂时故障采用指数退避和抖动，最多初次加三次重试；耗尽进入失败队列，管理员带理由重放，正式确认记录不允许重放覆盖。相机与全局队列有容量上限。

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

- `/health/live` 检查 API 存活；`/health/ready` 检查数据库及必要工作进程心跳。相机画面在线与真实识别是否成功需查看状态及业务证据。
- `/api/admin/status` 查看进程、OCR、队列和归档；`/metrics` 输出队列与待归档数量；均要求管理员身份。
- 升级后的本机服务重启不会主动解除仪器绑定。设备采集会话确实结束、显式交接或手动结束才改变归属。
- 原有模型 A/B 的明确资产映射继续使用；通用设备类型检测只能说明类型，不能仅凭“当前只有一台同类绑定”推断物理资产。
- 自动测试使用临时数据库，覆盖身份隔离、实际 API 到工作进程链路、事务、重传、租约、重试及归档去重；这些证据不代表现场设备识别准确率或整套生产上线验收。

回退前停止 `field-recognition.target` 并保存新产生的数据库和队列。不得直接用升级前备份覆盖已经产生新记录的数据库。需要旧程序应在隔离目录验证兼容后恢复运行，证据目录保持原位。
