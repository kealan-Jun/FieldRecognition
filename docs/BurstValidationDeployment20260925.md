# 上线记录：三连拍候选校验

- 用户明确回复“可以，现在暂停并更新”后实施。
- 目标：192.168.1.144，`/home/x1/Projects/FieldRecognition`。
- 实验关联补丁先单独保存为 `d8db1d2`，运行代码内容未改变；修复变基后版本为 `3c6b4d1feeb32090e067404294acab0fa8543a91`。
- 使用项目 `scripts/upgrade_runtime.py --revision ...` 审查，仅一个业务运行文件 photo_measurements.py 需要离线更新。
- 显式停止获准的 target、prepare、API、cameras、OCR、archive；升级工具确认离线后执行快进、准备与重启。没有停止 VisionCortex 或独立推理微服务。
- API 健康返回 ready/database ready，采集队列 0；四个业务服务均 active。实验关联配置字节不变。
- 全套测试 584 项已通过（579 项首轮通过，5 项补齐现有 Node 路径后复测通过）；合入先前实验关联补丁后，另外验证 9 项目录三连拍/上下文测试通过。

## 数据核验

已通过新 revalidate API 重校验当次 Demo 的三组草稿：

| 拍摄时间 | measurement | 当前修订 | 读数 | blockers |
| --- | --- | --- | --- | --- |
| 17:20 | 76da0367-4dcb-4fdd-8c88-9dfa8ab5a555 | 14 | 0.7742 g | 空 |
| 17:41 | ab04c628-8bec-4bc2-b598-36c8c57495e0 | 13 | 0.7717 g | 空 |
| 17:43 | 2aa98963-2eb9-47bf-b8e5-63022e108b36 | 14 | 0.771 g | 空 |

17:43 的已有 Codex 审计修订原样保留（其显示文本为 0.771）；重校验没有改变数值或冒充人工确认。三组仍为 draft，sources、original_candidates、corrections 不变；新增 validation_history 记录 photo-measurement/4 与原字段版本。AgentDesk 采集检查点已同步三个新修订及空 blockers。

升级前后整表业务文档哈希一致：bindings 51、jobs 810、scans 902、scene_visits 25、experiment_records 0。此次升级没有改写原图、原始任务或绑定历史，没有产生已确认记录/飞书行。

现场相机当前没有有效仪器绑定，需要重新扫码后继续拍照；尚待上线后新拍一次验证。不能把测试、旧数据重校验或 ready 声明为新硬件拍照到飞书填表的整条验收。

备份与审计：`/srv/sentinel-data/agentdesk-platform/data/local-run/burst-validation-20260925/`，包括一致性数据库备份、配置、启动前后状态、升级日志和三组重校验前后快照。原始推理模型版本不改写为新汇总规则。
