# NAS 文件夹与留存指南

程序在本机运行。只读相机采集目录，把结果写入 `/mnt/realityloop-nas/FieldRecognitionArchive/`；不更改 `VisionCortexExperimentArchive/`。目录采用“日期＋相机 → 事件 → 照片与结果”的结构，业务文件夹和文件名使用大驼峰格式，已有相机 ID、事件 ID 保持原值。

## 打开哪里

打开根目录 `Readme.html`，选择日期和相机进入 `DailyReport/DailyReport.html`。HTML 使用相对链接，直接用浏览器打开即可；也可访问 `http://127.0.0.1:8188/api/archive/files/Readme.html`。

```text
FieldRecognitionArchive/
├── Readme.html
├── 2026-09-14_lubancat-52d2ef0c_cam01/
│   ├── Bindings/
│   │   └── 13-26-19.000_<绑定或扫码ID>/
│   │       ├── Binding.json
│   │       └── Photos/Original<图片摘要>.png
│   ├── VideoReadings/
│   │   └── 13-27-02.120_<采集ID>/
│   │       ├── Result.json             # 单台仪器六字段
│   │       ├── Evidence.json           # 原始证据、状态和结果文件映射
│   │       ├── Photos/Frame<图片摘要>.png
│   │       └── Regions/Temperature<图片摘要>.png
│   ├── VoicePhotoReadings/
│   │   └── 13-28-01.000_<测量或采集ID>/
│   │       ├── Result.json             # 单台仪器六字段
│   │       ├── Evidence.json           # 原始证据、状态和结果文件映射
│   │       ├── Photos/Original<图片摘要>.jpg
│   │       └── Regions/Mass<图片摘要>.png
│   ├── PhotoReadings/                 # 手动上传照片，有数据时出现
│   └── DailyReport/
│       ├── DailyReport.html
│       └── DailyReport.json
└── .System/                           # 隐藏维护目录
    ├── Receipts/<相机>/<日期>/<实例>/<序号>-<SHA256>.json
    ├── Integrity/Latest.json
    ├── Integrity/Reports/<UTC日期>/<检查ID>-<SHA256>.json
    ├── MigrationMap.json
    ├── Index.json
    ├── Audit.html
    ├── SourceMaterials/              # 历史未分类材料，有数据时出现
    ├── UnassignedAssets/             # 历史无引用原件，有数据时出现
    └── PendingAssets/                # 发布过程中待归位图片，完成后移出
```

这是结构示例，不表示发生过示例时间的测量。没有数据的目录不会创建。没有可靠采集时间的记录使用 `UnknownTime_<ID>`，目录日期取首次归档队列日期，并在 JSON 的 `folder_time_basis` 明确说明，绝不将接收时间填成拍摄时间。目录首次确定后，结果修订、人员变更、绑定结束不会将其重命名。

## 各类事件里的文件

| 想查的内容 | 目录与文件 | 文件内容 |
|---|---|---|
| 谁在什么时间绑定 A、B 或实验台 | `Bindings/<事件>/Binding.json` | `binding` 保留原绑定、场景、扫码或交接文档，含人员、仪器 ID、起止时间和状态；`sources` 给出二维码来源，`receipt_versions` 给出不可覆盖历史。扫码命中与绑定成功分别保留其含义。 |
| 视频哪一刻读到哪台设备的数 | `VideoReadings/<采集>/Result.json` | 每台仪器一个六字段结果文件；`Evidence.json` 的 `observations` 保留 OCR 原文、区域、模型和时间，`sources.video_observation` 保留视频时间依据。 |
| 某张语音照片识别了什么 | `VoicePhotoReadings/<测量>/Result.json` | 同目录 `Evidence.json` 的 `sources.external_photo` 保留原 NAS 文件路径、源采集编号、拍摄和写入时间；读数与相应照片哈希、区域逐项关联。 |
| 上传照片的结果 | `PhotoReadings/<测量>/Result.json` | 同上；来源明确区别于语音拍照和视频。 |
| 一张实验台的总体记录 | `DailyReport/DailyReport.html` | 同日同相机各设备和场景事件的链接。具体实验台关系在 `Evidence.json` 的 `observations.workbench/workbenches`；不把 A、B 数值相加或复制成第三条测量。 |
| 原图和实际识别区域 | 事件中的 `Photos/`、`Regions/` | `Original` 为原始字节；`Photo/Frame` 为方向修正、解码后用于处理的整图；`Temperature/Speed/Mass` 为已定位字段的识别图；仅能定位整体面板时用 `Panel`。文件名摘要用于避免重传冲突。 |

`Evidence.json` 的 `photos` 给出图片相对归档根目录的路径、完整 SHA-256 和字节数，`sources` 将原件/整图关联到采集 ID。`observations.panel_regions` 保留面板 ID、定位框、数字区域和图像哈希，读数字段据此核对。相同图片字节只保存一份；如果扫码和读数共用照片，其中一个目录保存文件，另一个通过路径引用，不再次复制。原始 JPEG 和方向修正后的 PNG 字节不同，是两种证据表示，不是重传造成的副本。

## 连拍、修订和归属

- 显式 `burst_id` 才合并为一次测量；不按几秒内的时间接近关系猜测连拍。单张照片按采集 ID 归组，重新识别/网络重传共用目录。
- 一次连拍的多张照片都在同一测量中，`sources` 列出各自时间与原件；草稿、校正和确认共用原事件目录，`Evidence.json` 的 `decision` 保存字段、冲突、修订记录和确认依据，历史版本在回执中。
- 一图出现 A、B 时，分别生成 `Result.json`、`Result02.json`，每个文件只对应一台仪器。`Evidence.json.measurement_files` 列出文件路径、SHA-256、仪器 ID/名称和字段证据。首次分配后文件名与仪器的对应关系固定；新增连拍图片、重传或重识别不会交换两台仪器的文件。原图共用，不重复复制。
- 未绑定或归属不明确的原始材料可以保留；不补造 QR、设备编号、绑定关系或读数。显式人工校正建立的归属注明 `explicit_field_correction`，不会伪装为二维码绑定。
- 生产模式仍须草稿确认、写入并读回；当前运行模式单独由 `FIELD_RECORD_MODE` 决定。调整文件夹不会把历史测试记录提升为正式实验记录，也不会覆盖原始识别值。

## 固定读数结构

机器契约：[`schemas/InstrumentMeasurement.schema.json`](../schemas/InstrumentMeasurement.schema.json)。每个 `Result*.json` 直接是以下六字段对象，没有 `record`、`observations` 或其他外层包装。溯源信息放在同目录 `Evidence.json`，通过 `measurement_files` 引用结果。

```json
{
  "wearer_id": null,
  "device_model": null,
  "device_no": null,
  "qr_hash": null,
  "photo_time": {"timestamp_ms": null, "time": null},
  "values": [
    {"name": "温度", "value": null, "unit": null, "range": [null, null]},
    {"name": "转速", "value": null, "unit": null, "range": [null, null]}
  ]
}
```

天平的 `values` 固定为一项 `{"name":"质量","value":null,"unit":null,"range":[null,null]}`。

| 字段 | 规则 |
|---|---|
| `wearer_id` | 采集时佩戴人员稳定 ID，未知 null；不以显示姓名代替。 |
| `device_model` / `device_no` | 登记型号和资产编号；未知 null，编号为字符串，保留前导零。 |
| `qr_hash` | 实际解码原始字符串的 SHA-256，64 位小写十六进制；来自当时绑定或同图解码，不从标签文字猜测。 |
| `photo_time.timestamp_ms` / `time` | 同一瞬间的 Unix 毫秒和北京时间 `YYYY-MM-DD HH:mm:ss.SSS`，不额外加 8 小时。语音照片来自采集元数据；视频仅采用同步有效的 GWHP 全局帧时间，处理/接收时间另记。未知两者都 null。 |
| `values[].value` | 数字类型，保留正负号与小数；不可读、歧义、冲突为 null。搅拌器固定温度与转速两项，天平固定质量一项。 |
| `values[].unit` | 温度 °C、转速 rpm、质量按显示 g/mg/kg；必须有识别文本或该仪器登记依据。未知 null，冲突不猜。 |
| `values[].range` | 来自该仪器登记，单位必须一致；未知边界 null，不用本次读数的极值替代。 |

此格式不判断显示的是实际值还是设定值。原始识别文字、单帧分数、多帧一致性和规则版本在 `Evidence.json`，模型分数不等于实测准确率。多图冲突进入校正，正式值与原始候选分开留存。

## 隐藏维护区与兼容

`.System/Receipts` 是不可覆盖的原始版本；`.System/Integrity` 保存完整性检查，验证文件存在、字节数、SHA-256 和数据库快照一致。`.System/Index.json` 与 `Audit.html` 是全量版本索引。`.System/MigrationMap.json` 保存旧路径 → 新路径、照片去重位置和稳定事件目录，迁移前先记录映射，再原子移动文件；中断可继续。完整性检查、恢复工具和旧 HTTP 地址均通过该映射读取，历史回执中的旧路径/哈希保持原样。

旧 `Browse/Records/Objects/Receipts` 不再作为当前可见业务结构；已识别的生成视图被替换，未知的手写文件保留。日常仅打开根目录导航。备份应复制整个归档，包括 `.System`；只复制某个事件可能缺少共用照片或历史回执。

## 迁移和断线恢复

本机 SQLite 事务同时写业务记录与持久化归档队列。NAS 不可用时保留本地图片与队列，恢复后重试；容量不足暂停归档。原图和回执写完才能确认已归档；人类导航尚未重建时 `navigation_pending=true`，不冒充整理完成。

手动升级已有归档时只暂停归档 worker，API、采集和 OCR 可继续：

```bash
systemctl --user stop field-recognition-archive.service
.venv/bin/python scripts/migrate_archive.py --apply
systemctl --user start field-recognition-archive.service
```

脚本使用项目的 `.env`，取得归档进程独占锁，先校验、备份本机数据库和旧文件哈希清单，再迁移并逐一读回校验。备份位于本机 `Verification/ArchiveMigration<时间>/`。不删除源 NAS 采集材料；失败后修复存储问题，重复执行会沿映射继续，不应手工重建空队列。

识别新版本仍放在原测量目录：`Result*.json` 给出每台仪器当前规范结果；`Evidence.json.observations` 保留各次识别、规则快照与重识别原因，`receipt_versions` 可回溯旧回执。已确认的测量重新识别后，`decision` 表示修订草稿；再次确认前旧正式记录仍有效。照片沿用原来的单份哈希文件，重识别不会再创建测量文件夹。

源语音照片目录可由采集方提供 `PhotoReceipt.json` 明确连拍关系。这个回执属于输入协议，不能从相邻照片文件名推导。格式见项目 `AgentTools.md`；识别服务只读源照片及回执。

## 无有效结果与旧格式升级

未绑定且未执行 OCR 的照片只保留 `Evidence.json`、`Photos/` 及已有区域文件，不伪造空仪器测量。追溯状态为 `skipped`，`skip_reasons` 说明原因；`processing_status=completed` 只代表任务结束。已定位并归属到仪器、但部分字段看不清时仍输出该仪器的固定字段，未知值为 null。

旧版包含处理回执的 `Result.json` 是生成视图，升级后移至 `Evidence.json`，规范读数单独输出。原始照片、数据库任务和 `.System/Receipts` 不改写。未生成规范读数的旧 Result HTTP 地址映射到 Evidence，NAS 实体目录不会再出现冒充结果的空回执。
