# 快速开始

先按 [README 的环境准备](../README.md#quick-start) 建立本项目 Python 环境。CPU 与 GPU 依赖分别由两个锁文件维护；本机正在使用的环境不因文档或代码整理而重新安装。

## 隔离开发与验证

在仓库根目录运行完整测试。测试使用临时数据库、模型替身和合成图，不连接真实 Receiver 或 NAS：

```bash
env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL \
  OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  .venv/bin/python -m pytest -q tests
```

本地调试使用独立工作区、临时 `FIELD_DEMO_DATA` 与独立端口。完整 CPU 隔离命令见 [README](../README.md#quick-start)，它清除继承的生产来源配置，不调用 systemd。标准启动入口支持：

```bash
.venv/bin/python -m field_recognition --help
```

监听地址默认 `127.0.0.1:8188`，可用 `FIELD_HOST`、`FIELD_PORT` 或 `--host`、`--port` 设置。需要读取配置文件时显式传 `--env-file .env`；已有环境变量优先，不执行 shell 或变量展开。旧 `uvicorn app:app` 和 `uvicorn field_recognition.api.application:create_app --factory` 继续兼容，但不会自动读取 `.env`。导入 canonical 模块不会创建运行目录、数据库或模型；工厂调用和根 `app` 导入才会创建运行实例。

## CPU 照片识别

先启动 README 中的 CPU 隔离进程，再在另一个终端的仓库根目录执行。这里使用已标明来源的合成演示图；也可将文件路径换成自己的清晰 JPEG/PNG。提交识别会真正运行本地 CPU 模型，首次加载可能下载模型权重；无完整缓存且无法联网时会失败。此隔离配置不启用相机、NAS 或外部视觉调用。

```bash
.venv/bin/python - <<'PY'
import base64
import time
from pathlib import Path
from field_recognition.api.agent_client import FieldTools

tools = FieldTools('http://127.0.0.1:18188', camera_id='SampleUploadCamera')
scan = tools.call('scan_photo', {
    'camera_id': 'SampleUploadCamera',
    'image_base64': base64.b64encode(Path('docs/assets/readme-demo/input-1.png').read_bytes()).decode(),
})
job = tools.call('read_panel', {'capture_id': scan['capture_id']})
print('job_id:', job['job_id'])
for _ in range(60):
    result = tools.call('get_panel_result', {'job_id': job['job_id']})
    if result['status'] not in {'queued', 'running'}:
        print(result)
        break
    time.sleep(1)
else:
    print('仍未完成；稍后用原 job_id 查询，不重新提交。')
PY
```

检查最终 `status`、`outcome` 与 `lines`；任务完成也可能没有可用读数。未建立有效绑定时不推断仪器或人员归属，实际解码出的二维码身份单独保留。合成图识别结果不证明真实仪器准确率。完整工具契约见 [AgentTools.md](../AgentTools.md)。

## 托管部署

HTTP、相机监控、OCR 和归档已经集成，分别由托管进程承担职责。`FIELD_PRODUCTION_ENABLED=1` 选择进程拆分与持久化队列；`FIELD_RECORD_MODE=production` 选择正式测量草稿流程。登录由 `FIELD_AUTH_ENABLED` 单独控制，默认免登录。测试结果不能自动晋升为正式记录。

新的离线部署、人员登记和可选登录按 [托管运行说明](ProductionRuntime.md) 配置。`prepare_runtime.py` 负责人员/相机准备；`runtime_prepare.py` 负责数据库迁移预检与演练，两者用途不同。

正常使用期间的迭代只能在独立工作区开发和验证，推送代码不等于部署。检查测试提交使用：

```bash
.venv/bin/python scripts/upgrade_runtime.py --revision '<测试通过的提交 SHA>'
```

当前没有经过验证的运行代码不中断替换路径；守卫返回需要离线部署时，保持候选待部署。不要为满足该条件停服，已有绑定、原图、队列和历史回执须保留。详见 [升级入口](RuntimeReliability.md#升级入口) 与 [Agent 指南](../AGENTS.md)。

## 接入与维护

- [Agent 工具](../AgentTools.md)：发现与调用 15 个工具，沿用同一证据事务。
- [代码结构](CodeStructure.md)：业务目录、共享配置、兼容入口和测试夹具。
- [文档索引](README.md)：使用说明、证据契约和历史验证记录。
