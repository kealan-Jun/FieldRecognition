# 本机运行与 NAS 留存

识别程序在当前电脑后台运行，NAS 只提供已有照片与归档存储。无需在 NAS 安装程序、打开 SSH 或取得管理员账号；挂载共享目录所用账号需要有语音照片的读取权限，以及独立归档目录的写入权限。

## 当前部署

| 部分 | 位置与职责 |
|---|---|
| 挂脖设备 | `lubancat-52d2ef0c_cam01`，当前登记实验员为徐荣炜 |
| Receiver | `http://192.168.1.196:8080`，沿用现有采集服务 |
| 本机识别服务 | `/home/x1/Projects/FieldRecognition`，systemd 用户服务常驻，GPU OCR |
| 原始语音照片 | `/mnt/realityloop-nas/voice_photos/<camera_id>/日期/时间/照片.jpg`，本项目只读 |
| 本机业务状态 | 项目 `Data/` 下的 SQLite、照片副本和待归档队列 |
| NAS 留存 | `/mnt/realityloop-nas/FieldRecognitionArchive/`，本项目独立目录 |
| 只读结构参考 | `VisionCortexExperimentArchive/`，不向此目录写入 |

网页用于查看、登记和处理异常，关闭后后台仍工作。电脑需要保持开机运行；电脑关机或休眠时不会继续扫码和识别。重新运行后沿用本机照片去重状态与待同步队列，绑定仍须核验设备采集服务会话。

## 实际数据流程

1. 本机接收实时视频，在内存中解码二维码。只有二维码命中帧或明确的拍照、上传请求进入照片存储，普通视频帧不保存。
2. 唯一有效仪器码命中后自动绑定，记录相机、实验员、仪器、场景依据、扫码证据和开始时间。结束绑定另存结束时间与原因。
3. 绑定后 OCR 模型在本机 GPU 常驻。Agent 语音拍照仍由原有流程完成；本机监控此相机的新文件，稳定后读取并识别。
4. 保存原始照片字节、方向统一后的 PNG、选框图、读数、OCR 原始结果及兜底回执。原始字节和规范化图片分别计算 SHA-256。
5. 业务记录的新增、变化与归档队列在同一个 SQLite 事务中提交。独立后台线程向 NAS 写入照片对象和版本回执；成功后才确认该队列项已归档。

## NAS 目录与回执

```text
FieldRecognitionArchive/
├── Readme.html                    # 可直接打开的中文留存索引
├── Index.json                     # 同一索引的结构化数据
├── Objects/
│   └── <哈希前两位>/<SHA-256>.<扩展名>
└── Receipts/
    └── <camera_id>/<北京时间日期>/<source_instance>/
        └── <序号>-<回执SHA-256>.json
```

`Readme.html` 和 `Index.json` 展示本数据库最近 200 次归档记录变化，同一读数从排队到完成会有多个版本；每个版本均保留在 `Receipts/`。文件中的相对路径指向同一归档根目录，便于整体复制与校验。索引可更新，照片对象和版本回执不覆盖不同内容。

回执结构为 `field-recognition-receipt/1`：

| 字段 | 含义 |
|---|---|
| `source_instance`、`sequence` | 本机数据库的稳定标识与事务内递增归档序号，用于重试去重 |
| `entity`、`entity_id` | 扫码/照片、绑定、场景关系、读数任务或实验员设置的类型和业务 ID |
| `recorded_at` | 该版本进入本机归档队列的 UTC 时间；不替代实际拍摄、绑定或识别时间 |
| `document` | 完整业务快照，保留原始时间、相机、人员、仪器/场景、照片/任务 ID、解码位置及识别回执 |
| `artifacts.image` | 方向统一后的证据图片，包含相对路径、SHA-256 和字节数 |
| `artifacts.original` | 原始照片字节及其哈希；新照片保留 JPEG/PNG 等实际格式 |
| `artifacts.panel` | OCR 选框图及其哈希，产生选框图后才存在 |
| `physical_action_confirmed` | 固定为 false；扫码或识别文本并不能证明实际称量等物理操作 |

原始时间字段保留时区，索引按 `Asia/Shanghai` 显示。`scene_qr_verified=false` 表示场景仅来自仪器登记，不代表扫过场景码。照片文件名推导的拍摄时间与设备提供的帧时间分开保留。

首次启用会补归档数据库中**现存**的历史记录及照片。旧版没有保存的原始压缩字节会明确标为 `original_status=not_retained_by_earlier_version`，不以规范化图片冒充。旧记录缺失的人员或事件也不补造；历史记录只有当前保存下来的快照，无法重建此前未记录的中间版本。

## 配置与状态检查

在本机未跟踪的 `.env` 中配置以下项目，保留现有 Receiver、相机、OCR 和阿里云设置：

```dotenv
FIELD_SAVED_PHOTO_ROOT=/mnt/realityloop-nas/voice_photos
FIELD_SAVED_PHOTO_WATCH_ENABLED=1
FIELD_ARCHIVE_ENABLED=1
FIELD_ARCHIVE_ROOT=/mnt/realityloop-nas/FieldRecognitionArchive
FIELD_ARCHIVE_MOUNT=/mnt/realityloop-nas
```

`FIELD_ARCHIVE_ROOT` 最后一层必须是 `FieldRecognitionArchive`，且不得放在 `VisionCortexExperimentArchive` 内。`FIELD_ARCHIVE_MOUNT` 要求目录是已挂载目标，避免 NAS 卸载后误写本地 `/mnt`。SQLite WAL 与待归档队列必须留在本机。

修改配置后，在没有进行中的读数任务时重启本项目服务：

```bash
systemctl --user restart field-recognition-demo.service
.venv/bin/python scripts/control.py status
```

网页“操作记录”显示 NAS 连接状态、已归档和待同步数量。`GET /api/state` 和 Agent `get_field_state` 的 `archive` 字段提供相同状态，以及 `last_error`、`last_archived_at`、`source_instance` 与归档路径；错误只暴露类别，不输出凭证。

## 断线、重复与保留边界

- 每 3 秒处理一批归档，失败项至少等待 15 秒重试；单张文件损坏不会阻塞其他记录归档。照片源文件缺失或哈希错误时保留失败项，不确认成功。
- NAS 不可用时，本机扫码和已取得照片的 OCR 可以继续；新的 NAS 照片需要等共享目录恢复后才能读取。待归档记录跨本机服务重启保留。
- 文件先写到同目录临时文件，完成写入并同步后更名，再确认本机队列。重试遇到同路径同内容时复用；遇到不同内容时拒绝覆盖。
- 不自动清理本机或 NAS 历史数据。需要保持两处存储空间充足。此归档是业务凭证与图片留存，不是可直接恢复全部运行状态的 SQLite 灾难恢复备份，也不提供 NAS 权限级防篡改保证。
- 当前按一台配置挂脖设备运行；目录包含相机 ID，给后续扩展保留区分，尚未实现多主机并发写同一归档根目录。

自动验证使用临时文件系统与模型替身，覆盖事务回滚、原件校验、绑定起止与 OCR 关联、NAS 失联重试、重复写入、坏文件隔离和索引恢复。实际 NAS 写入验证与真实仪器识别验收分别记录；存储成功不能替代物理仪器准确率验收。
