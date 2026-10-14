# MetaData 原始照片与识别结果

采集端创建日期、相机、MetaData 和拍摄批次目录；识别端等待文件写入稳定后读取，只创建自身的处理结果。无需识别端预建上传目录，也不要求采集端等待识别完成。

```text
FieldRecognitionArchive/
└── YYYY-MM-DD_相机ID/
    ├── MetaData/                     # 采集端所有
    │   └── HH-MM-SS/
    │       ├── YYYYMMDD_HHMMSS.jpg
    │       ├── YYYYMMDD_HHMMSS_001.jpg
    │       └── YYYYMMDD_HHMMSS_002.jpg
    ├── VoicePhotoReadings/           # 识别端所有
    │   └── 时间_事件ID/
    │       ├── Readme.html
    │       ├── Readings.json
    │       ├── Evidence.json
    │       └── Regions/              # 实际面板/数字裁剪
    └── DailyReport/
        └── DailyReport.html
```

相机 ID 必须与登记值完全一致，例如 `lubancat-14d632c0_cam02`。文件名与目录中的日期、时分秒一致，默认按北京时间解释；文件时间不视为硬件校时证明。鲁班猫相机是用户确认的第一人称设备，这一视角信息不代替仪器二维码或人员绑定。

采集端只写 MetaData。上传过程中可保留批次目录内的 `.uploading`，完成后删除；识别端不会读取带该标记的目录。没有标记时仍检查文件稳定性、大小与哈希。明确的 `PhotoReceipt.json` 按原协议验证；已启用 `directory_triplet` 的部署仍须满足完整三张照片的目录边界。目录只有一两张时会等待，不能把未收齐解释成模型识别失败。

## 监控与过渡

主输入根路径是 `FieldRecognitionArchive`，不含末尾的日期、相机或 MetaData。监控发现各已登记相机后来创建的日期与批次目录。新硬件仍需登记相机与实际使用人员；上传目录名称不授权占用仪器。

配置示例使用占位挂载路径，实际部署必须核对 NAS 挂载与卷身份：

```dotenv
FIELD_SAVED_PHOTO_ROOT=/mnt/field-data/FieldRecognitionArchive
FIELD_SAVED_PHOTO_ADDITIONAL_ROOTS=["/mnt/field-data/voice_photos","/mnt/field-data/FieldRecognitionArchive"]
FIELD_ARCHIVE_LAYOUT=metadata
FIELD_SAVED_PHOTO_WATCH_ENABLED=1
```

额外根目录显式包含新旧两处，是为了兼容相机注册表中尚未替换的单相机根路径。没有硬编码站点默认值，也不会在未挂载目录下创建原图。

旧输入 `voice_photos/相机ID/日期/时刻/照片` 和过渡期的 `FieldRecognitionArchive/相机ID/日期/时刻/照片` 都可读取。MetaData 与旧路径统一使用 `相机ID/日期/时刻/文件名` 作为来源身份；相同身份和字节沿用已有任务、连拍编号和首次来源回执，避免重复识别。同身份但不同内容会报冲突。

## 看哪个文件

- 日常查看 `DailyReport/DailyReport.html`，或批次处理目录的 `Readme.html`。
- `Readings.json` 保存逐张/逐次数字观测，不要求不同照片数值相等，也不取平均或多数值。
- `Result.json` 保持既有单台仪器六字段契约，是单值复核视图；没有可确定的单值时可为 null。它不代表逐张读数丢失。
- `Evidence.json` 保存原文、质量问题、时间与身份依据、识别版本、历史回执和草稿状态。`UnboundReadings.json` 保留未绑定候选的兼容入口。归档 JSON 的追溯字段由程序组织，不等于云端返回了同样长的文本。

未绑定的照片也保留观测，不推断仪器或人员。天平已确认的 g、固定四位小数规则继续生效；数字缺位、小数点冲突和单位冲突仍保留质量问题。

三连拍数值变化单独展示为逐张观测，不自动认定识别错误。视频可读变化也可先保存观测，稳定性作为附加证据；继续复用该帧 OCR、每个面板至少两秒的保存间隔与照片任务优先规则。它不表示每帧存储、每次观测都正确或自动人工确认。

## 原图与历史

MetaData 原图经过实际字节和哈希核验后直接引用。识别端不改名、不删除采集端文件，不再额外归档一份完整原图或可从原图重建的整幅 PNG。实际区域裁剪和不可变回执继续保留，当前工作图仍在本地。

旧归档已留存的原图可按哈希移入相应 MetaData；原事件 ID、结果路径和不可变回执不变，历史图片引用通过路径映射解析。仅处理端拥有且字节完全一致的副本可以回收。过渡期上传端仍拥有的旧路径原图保持原位，不能在同事切换前移动或删除。

缺失的历史归档对象仅在本地证据仍存在、源文件稳定且 SHA-256 完全一致时恢复，并追加恢复记录；无匹配字节时保持失败，不生成替代图片或重写旧回执。

此改动需要按升级守卫检查修订并发布。隔离测试不等于线上已经切换；服务运行期间不能直接替换加载的源码、配置或网页资源。
