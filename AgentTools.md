# 现场识别 Agent 工具 v1

这是一组框架无关的 HTTP JSON 小工具，复用网页已有取流、二维码、设备绑定和 PaddleOCR 实现。无需改动挂脖设备 Agent。本版不是 MCP 服务；Agent 适配层将工具名称及参数转发到以下接口即可。

- 工具发现：`GET http://127.0.0.1:8188/api/tools`，返回 name、description、input_schema。
- 工具调用：`POST /api/tools/{name}`，请求体直接为参数 JSON。
- 返回：`{"tool":"工具名","result":{...}}`。
- 图片引用 `image_url` 相对于此服务地址；原图、时间戳、二维码证据、绑定及 OCR 结果沿用同一数据库。

| 工具 | 必填参数 | 结果 |
|---|---|---|
| get_field_state | 无，传 `{}` | 仪器登记、相机、绑定和 OCR 状态 |
| capture_and_scan | 无，传 `{}` | 最新主码流照片、capture_id、scan_id、二维码 matches、帧时间戳 |
| scan_photo | camera_id、image_base64 | 对 Agent 已有照片进行同样的扫码和存证 |
| bind_instrument | scan_id、instrument_id、operator | binding_id 和仪器/场景/相机绑定快照 |
| read_panel | binding_id、capture_id | 异步 job_id；可选 crop=[x,y,width,height] |
| read_saved_panel | binding_id、image_path（或 photo） | 复用 NAS 已拍照片，异步 job_id |
| get_panel_result | job_id | 任务状态及完成后的原文、分数、坐标、来源 |
| end_instrument_binding | binding_id | 结束本次使用绑定 |

## Agent 调用顺序

1. get_field_state 查看仪器登记和相机状态。仪器所属场景先在网页登记，Agent 不猜测场景或操作人。
2. capture_and_scan：先对准场景码取图，从 scene_matches 调用 enter_scene；再对准仪器码取图。matches 为空时不能建立新绑定；多个码时由用户明确选择仪器。
3. bind_instrument：使用实际扫描所得 instrument_id、scan_id 和用户提供的 operator。同相机、仪器和实验员的重复请求返回原 binding_id；已有其他有效绑定时拒绝覆盖。设备采集服务当前会话内沿用原绑定，无需每次重复扫码。成功绑定后异步预加载 CPU PaddleOCR，绑定接口不等待模型加载完成。
4. 用户对现有 Agent 说“拍照”，沿用该 Agent 的拍照与 NAS 保存流程。本服务监控当前绑定相机的新语音照片，稳定后自动识别；不用再调用 capture_and_scan。
5. get_field_state 查看该 binding_id 的 jobs；需要直接传递现有拍照回执时，调用 read_saved_panel，传 binding_id、确切 image_path 和可选 crop。已导入的本地 capture_id 继续用 read_panel。重复提交同一源照片与选框沿用原任务。
6. 每隔约 1 秒 get_panel_result；只有 completed 才展示 lines。调用方设置自己的总等待期限，超时保留 job_id 稍后查询，不重新提交 OCR。
7. 使用结束时 end_instrument_binding。

没有绑定、相机不符、照片过旧或仪器码冲突会拒绝 OCR。crop 不传表示整图识别，不代表已自动定位面板。上传照片的 camera_id 为调用方声明，不等于硬件身份认证。

## Python 接入

`agent_client.py` 仅使用 Python 标准库；可直接交给 Agent 开发方。

```python
from agent_client import FieldTools

tools = FieldTools('http://127.0.0.1:8188')
definitions = tools.definitions()  # 将 name/description/input_schema 注册到 Agent 框架

# Agent 选定工具后，统一分发，不需要复制任何识别算法：
def dispatch_tool(name, arguments):
    return tools.call(name, arguments)

photo = dispatch_tool('capture_and_scan', {})
print(photo['capture_id'], photo['matches'])
# 后续绑定必须使用实际 matches 中的 id 和真实操作人，再提交 read_panel。
```

请求示例：

```bash
curl -sS http://127.0.0.1:8188/api/tools/capture_and_scan \
  -H 'Content-Type: application/json' -d '{}'
```

## 状态与错误

- 工具调用 HTTP 200 只表示调用成功；read_panel 的 result.status=queued 不是 OCR 已完成。
- OCR：queued → running → completed / failed；服务重启时未完成任务标记 interrupted。
- 404：工具、扫描记录或任务不存在；422：参数或图片无效；409：相机暂不可用或绑定冲突；429：OCR 队列已满。
- HTTP 错误返回 detail，Python 客户端抛出 FieldToolsError(status, detail)。网络异常由标准库上抛。客户端不自动重试写操作，避免超时后重复绑定或重复提交 OCR。
- OCR 输出为模型原文和候选数字，不能将缺失文字补写为真实读数。

## 部署边界

当前服务监听本机 127.0.0.1:8188，Agent 适配器可以运行在同一电脑。若 Agent 在挂脖设备本机，localhost 指的是挂脖设备而非此电脑，尚需受控网关或隧道接入；本轮未将无认证 Demo 暴露到局域网。

图片采集来自挂脖设备，二维码解码与 PaddleOCR 在服务端执行。当前为未签名演示二维码及 CPU OCR；此接口封装不改变其身份认证和准确率边界。

## 场景进入工具

工具总数现为 9。调用 capture_and_scan 或 scan_photo 后，从 scene_matches 取得已登记的场景；调用 enter_scene，参数 {"scan_id":"扫码记录 UUID","scene_id":"场景 UUID"}，确认该照片相机进入场景。之后扫描仪器码并调用 bind_instrument，系统校验同相机的当前场景。有有效仪器绑定时拒绝切换场景；同场景重复进入不会结束绑定。录入实验员仍由用户指定。get_field_state 返回 scenes 与 scene_visits；所有场景与仪器码仍未签名。


## 连续视频扫码 HTTP 接口

原有八个 Agent 工具保持兼容，新增 read_saved_panel；网页使用新增 HTTP 接口，无需逐张调用 capture_and_scan：

- `GET /api/camera/preview.mjpg`：MJPEG 实时预览，无绑定副作用。
- `POST /api/camera/scan-sessions`：参数 `{"operator":"用户指定实验员"}`。调用即选择自动进入唯一场景、自动绑定唯一且场景匹配的仪器。
- `GET /api/camera/scan-sessions/{session_id}`：约每 0.5 秒查询。`scanning` / `waiting_camera` 继续等待；`bound` 读取原始命中帧 `scan` 和 `binding`；`needs_selection` 暂停供用户选择；`stopped` / `failed` 结束。20 秒未查询自动停止。
- `DELETE /api/camera/scan-sessions/{session_id}`：停止自动扫码，保留已完成的绑定。

已有有效绑定时 start 直接返回 bound 和原绑定，不重复写入。设备服务离线或 receiver 的 RGB ingress 会话变化后，该相机的旧绑定及场景失效，重新在线后必须使用新照片。HTTP 连接失败本身不代表设备重启。ingress 会话也可能因设备重连/receiver 重启改变，尚不等于设备进程启动标识；详见 README 的证据边界。绑定后自动加载并常驻 CPU OCR 模型；仅新语音照片或明确的照片识别请求才执行面板预测，不逐帧运行视频 OCR。


## OCR 模型生命周期

get_field_state 的 `ocr.status` 为 queued/loading 时表示绑定后的后台加载正在进行，ready 且 resident=true 表示模型已常驻。`loaded_at`、`load_count`、`load_seconds` 可供核对。后续 read_panel 复用此实例；加载期间提交的任务在同一个 CPU 工作队列中等待。绑定结束、设备采集服务离线均不卸载模型；本地识别服务重启后，如仍有有效绑定，会自动重新加载。

模型加载失败不撤销成功绑定，状态显示 error，后续 bind_instrument 或 read_panel 可重试。模型常驻不等于连续执行 OCR，也不等于物理仪器准确率已经验证。


## 已有语音照片与自动识别

当前服务监控 `/mnt/realityloop-nas/voice_photos/<绑定相机>/日期/时间/照片.jpg`。绑定后只加载 OCR 并等待文件；现有 Agent 执行拍照、写完照片后，本服务才执行识别。轮询 2 秒，稳定至少 1 秒；不重拍、不修改 NAS、不因相机视频不断变化而触发读数。目录和导入回执的持久化去重跨本地服务重启有效。设备采集服务停启导致绑定失效后，重新扫码才能处理之后的新照片。

直接传递拍照结果的标准库调用：

```python
# binding_id 来自当前有效绑定；image_path 必须来自现有拍照工具返回结果。
job = tools.read_saved_panel(binding_id, image_path)
result = tools.call('get_panel_result', {'job_id': job['job_id']})
```

`read_saved_panel` / `POST /api/ocr/photo-result` 接受以下两种输入之一：

- `binding_id`、`image_path`、可选 `crop=[x,y,w,h]`。image_path 是已配置 voice_photos 内的具体照片绝对路径或相对路径。文件名日期/时间和相机目录均校验；无目录自动搜索、无“最新图”猜测。
- `binding_id`、`photo`、可选 crop。photo 包含源 `capture_id`、`camera_id`、带时区 `captured_at`、`source_ref`、纯 `image_base64`，可选原字节 `sha256`。source_ref 只作回执保留，不解释为任意下载 URL。照片相机、绑定时间、原图哈希不符则拒绝。

同一照片和选框已有任务时返回原 job_id；不要通过重发来等待结果。识别先用常驻 CPU OCR，从任务真正运行开始计时，5 秒没有完整数字才用视觉兜底。每次最多一个云端请求，同一绑定两次尝试至少隔 30 秒。收到 `completed` 后统一展示 `lines`；没有数字时如实回答看不清。`confidence`、`polygon` 允许 null，不要生成分数、坐标或额外读数。界面无需单列云端来源，后端 `local_ocr`、`fallback`、`external_photo` 保存过程和原图回执。

`get_field_state.photo_watch` 提供监控状态、待处理文件数和最近 job_id；自动识别结果位于同一状态的 jobs。此项目没有更改现有 Agent 的聊天或拍照服务，Agent 若要在对话中主动报出读数，需通过 get_field_state/get_panel_result 获取结果。网页会自动刷新显示。
