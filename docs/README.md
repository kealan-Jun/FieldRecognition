# 文档索引

照片本地初读与云端同时处理、同条读数更新及验证边界见 [并行照片识别](ParallelPhotoReview20261015.md)；最新像素保护、覆盖率取舍和本机上线记录见 [云端复核误报保护](CloudReviewRepair20261015.md)。

日常使用从 [项目介绍](../README.md) 与 [Agent 接口](../AgentTools.md) 开始。

| 主题 | 文档 |
| --- | --- |
| 安装与配置 | [快速开始](QuickStart.md)、[技术方案](Architecture.md)、[GPU 部署](GPU部署.md)、[托管运行](ProductionRuntime.md) |
| 代码维护 | [代码结构与约束](CodeStructure.md)、[Agent 指南](../AGENTS.md)、[贡献与支持](../CONTRIBUTING.md)、[安全说明](../SECURITY.md)、[运行可靠性](RuntimeReliability.md) |
| 采集与身份 | [本机自动运行](本机自动运行.md)、[自动解绑](AutomaticBindingRelease20260921.md)、[短断流恢复](BindingReconnect20261014.md)、[两路采集](TwoStreamEvidence20260923.md) |
| 读数与证据 | [MetaData 上传与逐次读数](MetaDataUpload.md)、[视觉配置](NianfengVision20260925.md)、[云端多区域与 OFF 协议](VisionReadoutRepair20261014.md)、[NAS 留存指南](NAS文件夹与留存指南.md)、[六字段契约](../schemas/InstrumentMeasurement.schema.json) |
| 验证记录 | [时延验收](PublicationLatency20261008.md)、[运行与验证记录](运行与验证记录.md)、[项目进展](ProjectHistory.md)、[界面演示](ReadmeShowcase.md) |

`history/`、日期化修复/部署报告及运行验证记录保存当时的代码、部署与证据事实，可能包含原部署环境标识，不是首次克隆的配置模板。当前接口和部署方式以本索引的使用文档为准；历史记录不能代替新版本验证。
