# 快速开始

先按 [README 的环境准备](../README.md#quick-start) 建立本项目 Python 环境。CPU 与 GPU 依赖分别由两个锁文件维护；本机正在使用的环境不因文档或代码整理而重新安装。

## 隔离开发与验证

在仓库根目录运行完整测试。测试使用临时数据库、模型替身和合成图，不连接真实 Receiver 或 NAS：

```bash
env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL \
  OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  .venv/bin/python -m pytest -q tests
```

本地调试使用独立工作区、临时 `FIELD_DEMO_DATA` 与独立端口。显式关闭归档、照片监控、自动采集和视觉调用，避免继承本机生产来源配置。启动入口支持：

```bash
.venv/bin/python -m uvicorn field_recognition.api.application:create_app \
  --factory --host 127.0.0.1 --port 18188
```

旧 `uvicorn app:app` 入口继续兼容。导入 canonical 模块不会创建运行目录、数据库或模型；工厂调用和根 `app` 导入才会创建运行实例。开发完整配置示例见 [README](../README.md#quick-start)。

## 托管部署

HTTP、相机监控、OCR 和归档已经集成，分别由托管进程承担职责。`FIELD_PRODUCTION_ENABLED=1` 选择进程拆分与持久化队列；`FIELD_RECORD_MODE=production` 选择正式测量草稿流程。登录由 `FIELD_AUTH_ENABLED` 单独控制，默认免登录。测试结果不能自动晋升为正式记录。

新的离线部署、人员登记和可选登录按 [托管运行说明](ProductionRuntime.md) 配置。`prepare_runtime.py` 负责人员/相机准备；`runtime_prepare.py` 负责数据库迁移预检与演练，两者用途不同。

正常使用期间的迭代只能在独立工作区开发和验证，推送代码不等于部署。检查测试提交使用：

```bash
.venv/bin/python scripts/upgrade_runtime.py --revision <测试通过的提交 SHA>
```

当前没有经过验证的运行代码不中断替换路径；守卫返回需要离线部署时，保持候选待部署。不要为满足该条件停服，已有绑定、原图、队列和历史回执须保留。详见 [升级入口](RuntimeReliability.md#升级入口) 与 [Agent 指南](../AGENTS.md)。

## 接入与维护

- [Agent 工具](../AgentTools.md)：发现与调用 15 个工具，沿用同一证据事务。
- [代码结构](CodeStructure.md)：业务目录、共享配置、兼容入口和测试夹具。
- [文档索引](README.md)：使用说明、证据契约和历史验证记录。
