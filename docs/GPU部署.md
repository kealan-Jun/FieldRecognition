# GPU OCR 部署

默认使用 CPU。选用 GPU 时安装匹配平台的 GPU 依赖，并显式设置 `FIELD_OCR_DEVICE=gpu:<编号>`；二维码仍由 ZXing / OpenCV 解码，H.264 仍由 PyAV 在 CPU 上解码。

历史验证环境（2026-09-14）为 NVIDIA RTX 3090 Ti（24 GB）、`gpu:0`，仅作为已验证实例，不是首次克隆的硬件要求。

## 依赖和启动

GPU 锁文件面向 Linux x86_64、Python 3.11；历史验证使用 NVIDIA 驱动 580.173.02。GPU 锁文件固定 PaddlePaddle GPU 3.2.0、CUDA 12.6 依赖、PaddleOCR 3.2.0 和 PaddleX 3.2.1。GPU wheel 来自 [Paddle 官方 CUDA 12.6 索引](https://www.paddlepaddle.org.cn/packages/stable/cu126/paddlepaddle-gpu/)。它用于本机平台，不是通用的 Windows / ARM 安装文件。

以下保留 2026-09-14 单进程部署的 CPU → GPU 切换步骤，仅适用于已经离线的环境或明确授权的停写窗口。现有托管部署按 [托管运行](ProductionRuntime.md) 与 [升级入口](RuntimeReliability.md#升级入口) 管理；正常迭代不能停止服务或在其加载的环境中安装依赖。停止相机采集会结束旧绑定、打断正在进行的采集，恢复后须重新扫码；已保存的照片与队列仍须保留。

满足上述前提后，在项目根目录执行：

```bash
systemctl --user stop field-recognition-demo.service
uv pip uninstall --python .venv/bin/python paddlepaddle
uv pip install --python .venv/bin/python -r Requirements.gpu.lock.txt
mkdir -p ~/.config/systemd/user/field-recognition-demo.service.d
cp deployment/gpu.conf ~/.config/systemd/user/field-recognition-demo.service.d/gpu.conf
systemctl --user daemon-reload
systemctl --user start field-recognition-demo.service
```

全新 Python 3.11 环境直接安装 `Requirements.gpu.lock.txt`。CPU 环境使用 `Requirements.lock.txt`；两种 PaddlePaddle 包共享导入模块，不能同时安装。若 `.env` 已配置 `FIELD_OCR_DEVICE`，应同步设成 `gpu:0`，因为 systemd 的 EnvironmentFile 会覆盖 Environment 设置。

手动运行：

```bash
FIELD_OCR_DEVICE=gpu:0 FLAGS_allocator_strategy=auto_growth \
OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  .venv/bin/python -m field_recognition --host 127.0.0.1 --port 8188
```

GPU 依赖带入所需 CUDA 用户态库，本轮不更换宿主机驱动或其他项目环境。`FLAGS_allocator_strategy=auto_growth` 让 Paddle 按需申请显存；它不是显存硬上限。原服务的 `MemoryMax` 限制系统内存，不限制 GPU 显存。

## 运行语义

`FIELD_OCR_DEVICE` 只接受 `cpu` 或单张卡编号（如 `gpu:0`），未设置时保留 CPU 作为便携默认值。设备在进程启动时确定；修改后在已离线或明确授权的停写窗口应用配置，正常迭代不得重启正在使用的服务。

GPU 模式加载前确认当前 Paddle 具备 CUDA 支持、指定 GPU 对该进程可见；不满足则明确记录 `OcrDeviceUnavailable`，不会悄悄切回 CPU。模型在成功绑定后加载并常驻，一个 `panel-ocr` 工作线程串行预测。状态 API 的 `ocr.device`、单次本地预测回执的 `device` 和网页 OCR 状态显示相同的设备配置。`not_loaded` 仅表示配置已选定，不表示已经执行 GPU 推理。

本机识别服务停止会释放模型；重新启动后有有效绑定则重新加载。设备采集会话重新上线的绑定规则、原图与历史记录、单张照片五秒无数字时的视觉兜底规则保持不变。

## 验证

自动测试使用模型替身，不启动 GPU：

```bash
env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL \
  OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 .venv/bin/python -m pytest -q tests
```

明确需要实际 GPU 验证时运行：

```bash
OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 PADDLE_PDX_MODEL_SOURCE=BOS \
  .venv/bin/python scripts/verify_gpu_ocr.py --output Verification/GpuOcr
```

该命令仅使用临时数据库、合成 `123.45` 面板，关闭相机、NAS 监控和云端调用。它验证真实模型连续预测两次、数值输出、模型只加载一次、当前进程的 GPU 显存记录，并保存 `GpuReceipt.json`。验证通过仅证明 GPU 调用与复用，不证明真实仪器准确率、相机到绑定或语音照片的完整现场链路。

2026-09-14 本机实测（`Verification/GpuOcr20260914/GpuReceipt.json`）：GPU 3.2.0 / CUDA 12.6，模型加载 7.590 秒；同一合成面板首次预测 0.385 秒、复用预测 0.012 秒；测试进程占用约 380 MiB GPU 显存。数值与时间只描述这次合成样张检查，不代表真实仪器准确率或长期吞吐。
