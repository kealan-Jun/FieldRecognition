# 可恢复的跨项目事件消费

`GET /api/events/feed?camera_id=<登记相机>&after_seq=0&limit=100` 使用已有
`archive_outbox` 的不可变业务快照。该 outbox 由同一数据库事务中的触发器写入；
无需增加另一条队列，不读取原图、不运行 OCR，也不确认 NAS 归档。

网页原有 `GET /api/events` 保留 SSE 变更通知，无需 `camera_id` 查询参数。
跨项目 JSON 消费者使用专用 `/api/events/feed`，与网页订阅分别维护契约。

返回 `field-event-feed/1`，包含 `source_instance`、`camera_id`、`after_seq`、
`next_cursor`、`high_watermark`、`has_more` 和 `items`。游标是全局 outbox 序号，
消费者分别保存每个相机的进度；跳过其他相机的序号不表示其他相机已经消费。
每页最多 100 条；以 `next_cursor` 继续并传回 `source_instance`。实例变化、
游标高于当前账本或组织命名空间变化返回冲突，不能自动清空消费者旧账本。
组织未配置时 `tenant=null`；可通过 `FIELD_EVENT_TENANT` 明确配置。首次安装
事件接口时该命名空间保存在本地 `archive_meta.event_tenant`，以后不能把既有
实例的历史投递重新贴到另一个组织。

每条事件同时保留业务 `event_id`、投递 `delivery_id` 和修订 `revision`。
相同字节的照片仍有各自的 `capture_id`。发生时间与接收、处理、记录时间分开；
未知发生时间保持空值。图像哈希、原始 OCR、规范化候选、模型版本、绑定快照和
outbox 引用分别保留。候选读数未经复核，不证明实际物理动作。人员显示名不能
替代稳定的 `operator_id`；`wearer_id` 与操作人分开。

消费者应在一个本地事务中提交原始投递、当前修订与游标，并检查重复投递的
内容一致性。拒绝/取消状态以撤回修订保存，不能丢掉旧证据。绑定使用
`[valid_from,valid_to)`；不得用当前绑定覆盖晚到照片的原归属。

VisionCortex 的 `device_day.field_recognition` 显式配置端点、相机和组织后，
发布重放服务会消费本接口并更新本地人员日 JSON。设备日原始归档保持原位置。
生产消费者未配置、FieldRecognition 未部署本接口或人员关联有冲突时，人员级
全天交付仍是 `NOT_PROVEN`。本地真实事件回放不等于生产服务已经加载此版本。

该变更需要部署新 API 代码，但不增加数据库 schema_migrations 版本或改写业务
记录。升级仍遵守 `AGENTS.md` 的不中断运行要求和 `scripts/upgrade_runtime.py`
检查。现有共享源码部署没有已验证的无中断替换方式；不要在运行 checkout
上直接切分支或重启服务。回滚到旧代码保留 outbox、event_tenant 元数据及
消费者 inbox，不回退或删除业务数据库。
