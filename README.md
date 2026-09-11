# FieldRecognitionDemo · 现场识别

独立的二维码识别、场景登记、设备绑定与真实 PaddleOCR 面板识别演示。不依赖 Agent，不访问 NAS，不修改 VisionCortex 采集或预处理服务。

## 独立项目

本机项目根目录为 `/home/x1/Projects/FieldRecognitionDemo`，使用独立 Git 仓库和 Python 虚拟环境。旧目录 `/home/x1/FieldRecognitionDemo` 保留为指向此目录的兼容链接，供历史证据中的绝对路径继续访问。Python 3.11.15 由用户级 uv 运行时提供，不依赖 VisionCortex 的解释器、代码或配置。

| 内容 | 路径 |
|---|---|
| FastAPI 服务、异步 OCR 与存证 | `app.py` |
| 挂脖设备 GWHP 主码流接收 | `receiver.py` |
| 二维码解码、场景绑定 | `qr_decode.py`、`scene_binding.py` |
| Agent HTTP 工具与客户端 | `agent_tools.py`、`agent_client.py`、`AgentTools.md` |
| 网页和二维码样张 | `static/` |
| 仪器、场景初始登记 | `InstrumentRegistry.json`、`SceneRegistry.json` |
| 依赖、测试、服务模板 | `Requirements.lock.txt`、`tests/`、`deployment/` |
| 本地照片、数据库 | `Data/` |
| 历史验证、标签交付、临时材料 | `Verification/`、`VerificationData/`、`output/`、`tmp/` |

迁移保留全部已有本地材料；运行数据、历史验证原件、模型、虚拟环境和日志均不纳入 Git。下文的 `Verification/` 引用只在本机历史材料中存在，新克隆不会附带这些实拍数据或回执。模型缓存仍使用 PaddleOCR/PaddleX 的用户级默认位置，不复制进代码仓库。

### 新环境安装

以下命令用于新克隆或新环境；本机已迁移环境无需重建。需要已安装 uv，并将终端切换到项目根目录：

```bash
uv python install 3.11.15
uv venv --python 3.11.15 .venv
uv pip install --python .venv/bin/python -r Requirements.lock.txt
```

`Requirements.lock.txt` 是已有环境的完整版本清单，包含运行和测试依赖。模型权重不在仓库内；首次实际 OCR 调用会加载缓存，缺少缓存时可能下载权重。

### 本地启动与测试

不配置 receiver 时，可使用照片上传和二维码样张。启动命令：

```bash
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8188
```

若本机用户服务已运行，请直接使用现有页面；同一端口不能再启动第二个实例。运行已有测试：

```bash
env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 .venv/bin/python -m pytest -q tests
```

测试使用临时数据库和样张，不启动 PaddleOCR 模型，不连接真实 receiver。

### 用户服务安装

服务模板为 [deployment/field-recognition-demo.service](deployment/field-recognition-demo.service)，默认项目位置为 `~/Projects/FieldRecognitionDemo`。其他安装位置须调整模板中的工作目录、启动命令和环境文件路径。新安装时：

```bash
cp .env.example .env
# 在 .env 中填写本机 receiver 与 camera 设置；不要提交 .env。
mkdir -p ~/.config/systemd/user
cp deployment/field-recognition-demo.service ~/.config/systemd/user/field-recognition-demo.service
systemctl --user daemon-reload
systemctl --user enable --now field-recognition-demo.service
```

本机已有服务的配置在迁移时保留，只更新项目路径。不要用新安装模板覆盖已有相机配置；修改实际服务的环境配置后执行 `systemctl --user daemon-reload` 和 `systemctl --user restart field-recognition-demo.service`。手动启动的进程不会自动读取 `.env`，需在启动前设置环境变量。

## 使用

打开 http://127.0.0.1:8188/ 。

1. 在“仪器登记”中填写两台仪器的实际名称、型号和所属场景。
2. 点击“挂脖设备拍照”，从接收端取最新主码流画面。若接收端不可用，可上传照片或点击明确标注的测试样张。
3. 确认二维码读出的仪器，填写实验员，点击绑定。场景来自仪器登记；本版不包含场景分类模型。
4. 再拍摄面板，或使用同一照片。在图上拖动选择面板范围，点击识别。
5. 核对 OCR 原文、模型分数与图片。模型分数不是准确率；数字不会自动推断成转速、温度或实验动作。
6. 结束绑定或导出 JSON。原图、哈希、选框、模型结果、仪器/场景快照及时间分别留存。

摄像头码尚未用于领用登记。本版实验员由页面填写，设备标识来自取流协议或上传声明，不具备用户认证/硬件认证。两张仪器码沿用已有唯一 ID，未签名，不具有防复制能力。

## 接收端协议

参考用户提供的《下游_全路RGB主码流_Windows_Python开发与接口协议.md》（2026-08-11 / f806ba9）。

- 设备发现：`GET /api/status`，以 `sender_id` 和 `camera_id` 为身份，选择 `online && media_live` 的目标相机。
- 主码流：`GET /api/preview/rgb-h264-frames?sender_id=...&camera_id=...&quality=main&metadata=global`。
- 校验 `X-GWV3-Rgb-Stream: main`；解析 GWHP v1/v2、扩展头及长度边界；不把 HTTP chunk 当视频帧。
- 单目标相机、两包上限队列、丢包后等待 KEY|CONFIG 并重建解码器；只保留最新解码图，超过一秒拒绝当作新照片。
- 保存帧 PTS、sequence、sender 时间、global 时间与 clock_sync_valid；不额外加 offset。
- 本 Demo 使用 PyAV CPU 单路解码，**不是协议中要求的生产级全路 NVDEC 验收**。软件解码和 PaddleOCR 都不占用 NAS YOLO 的 GPU。
- 不直连 sender 内部媒体端口，不连接 Depth，不修改接收端连接容量。

当前 receiver：`http://192.168.1.196:8080`，目标：`lubancat-52d2ef0c_cam01`。2026-09-11 已实测主码流连接及真实取图，分辨率 1280×800。拍摄证据与时间戳见 `Verification/ActualNeckCapture.json`。当前画面未出现仪器二维码或可读面板，尚不能证明真实仪器绑定及面板识别准确率。

## 运行

独立 Python 环境：`.venv`，已安装 CPU `paddlepaddle==3.2.0`、`paddleocr==3.2.0`、`av==16.1.0`。完整依赖见 Requirements.lock.txt。

用户服务：`field-recognition-demo.service`。当前只监听本机 8188，CPU 上限 2 核、Nice=10，不对公网开放。

```bash
systemctl --user start field-recognition-demo.service
systemctl --user stop field-recognition-demo.service
```

在该服务环境配置中更新 `FIELD_RECEIVER_URL` 后，仅重启此 Demo 服务。不要重启 `visioncortex-analysis.service`。

主要环境变量：

- `FIELD_RECEIVER_URL`：receiver 的 HTTP 地址。
- `FIELD_CAMERA_ID`：目标 camera key，从 `/api/status` 精确匹配后取得稳定身份。
- `FIELD_DEMO_DATA`：默认项目下 Data；测试使用独立目录。
- `FIELD_CAMERA_SNAPSHOT_URL`：无 GWHP receiver 配置时可选的 JPEG/PNG 接口，不与 GWHP 混用。

## 独立接口

- `GET /api/state`：设备登记、绑定、OCR 和取流状态。
- `PUT /api/instruments/{id}`：登记仪器和所属场景。
- `POST /api/scans`：上传照片，解码二维码并保存证据。
- `POST /api/camera/capture`：读取所选挂脖设备最新主码流帧并扫码。
- `POST /api/bindings`：确认使用关系；同相机旧绑定自动结束。
- `POST /api/bindings/{id}/end`：结束使用。
- `POST /api/ocr`：提交 binding_id、capture_id 和可选 crop=[x,y,w,h]，异步返回 job_id。
- `GET /api/jobs/{id}`：读取状态和 OCR 原文、坐标、模型分数。
- `GET /api/export`：导出登记、绑定和 OCR 结果；原图片以引用提供。

相机身份不同、照片中的仪器码与当前绑定冲突、绑定已结束或面板选框越界时拒绝识别请求。面板照片若没有二维码，其仪器关联依据为操作人选择的当前绑定，而非模型已证明同一仪器。

## 验证边界

- tests：真实二维码解码、绑定隔离、历史快照、错误输入、异步派发、协议拆包、真实 H.264 CPU 解码及过期帧拒绝。
- Verification/ActualOcr.json：真实 PaddleOCR 模型调用；输入为带明显标记的合成测试面板，不是实际设备拍摄，不能作为仪器准确率结论。
- Verification/BrowserCheck.json：独立测试数据下的浏览器扫码、登记、绑定、框选和 OCR 流程；与正式 Demo 数据隔离。
- 挂脖设备主码流连接与真实取图：PROVEN，见 Verification/ActualNeckCapture.json。
- 真实仪器扫码绑定及面板准确率、生产级多路 GPU 吞吐：NOT_PROVEN。

PaddleOCR 安装与调用依据：https://www.paddleocr.ai/main/en/quick_start.html 。

## Agent 小工具

已提供八个共用现有实现的 HTTP JSON 工具，工具发现入口为 `/api/tools`。接入说明见 [AgentTools.md](AgentTools.md)，标准库 Python 适配器见 [agent_client.py](agent_client.py)。`Verification/AgentTools.json` 记录工具接口真实取图验证和测试边界。

## 场景扫码接入（2026-09-11）

场景 01 已登记为湿实验实验台，称量仪器 A/B 均属于该场景。先用挂脖设备拍摄场景二维码，点击“确认进入场景”；再用同一设备拍摄仪器码，填写实验员并绑定，之后拍面板提交 OCR。同场景重复确认幂等，切换场景结束旧仪器绑定。其他相机的场景记录不可借用，仪器场景不一致拒绝绑定。每次 OCR 无需重复扫码，但必须保留有效的仪器绑定和场景记录。

页面和 Agent 共用 `/api/scene/enter` 与 `enter_scene` 工具；参数为 scan_id、scene_id。状态包含 scenes、scene_visits；扫码包含 scene_matches；绑定及 OCR 回执保留 scene_visit_id、场景快照和原图引用。原二维码不变，仍是未签名 Demo 码。场景所属关系由登记提供，扫码不能证明真实地理位置或硬件身份。

23 项测试通过，包括真实二维码解码、相机隔离、场景切换、重复进入、仪器绑定及 OCR 前置验证。浏览器使用 DemoSampleCamera 验证场景样张到仪器绑定，验证绑定已结束。真实挂脖设备在现场拍摄贴纸的完整验证尚待用户操作。

## 2026-09-11 实物扫码排查

扫码增加 ZXing-C++ 2.3.0 补充解码、灰度放大与局部对比度重试，保留原图、真实解码内容与原图坐标；扫描记录新增 qr_diagnostics。只接受校验通过的码内容，不根据标签文字或已登记设备猜测身份。依据：https://github.com/zxing-cpp/zxing-cpp/tree/master/wrappers/python 。

25项测试通过，但本次12张挂脖实拍仍均未解码成功，见 Verification/QrDecoderActualImages.json；不能称实物问题已修复。近处码块模糊、部分照片存在反光。请先固定平整标签，调整距离至黑白格边缘清楚，再重拍；用手机拍清晰照片上传可区分打印问题与挂脖镜头成像问题。
