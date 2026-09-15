# 现场识别 Agent 工具 v1

2026-09-15：托管部署默认免登录，业务工具可直接调用；多相机请求用 `X-Camera-Id` 选择已登记相机。仅显式设置 `FIELD_AUTH_ENABLED=1` 时要求 `Authorization: Bearer <session>` 并按账号限制相机。`FieldTools(..., session_token=..., camera_id=...)` 支持这两个参数。增加测量读取、修订、确认、拒绝及交接申请、决策六个工具，共 15 个，精确参数以 `/api/tools` 为准。它们与网页共用证据事务；免登录时使用登记的实验员与请求声明的操作人，不标记为已认证账号。队列支持持久化重试及租约恢复；参阅 [托管运行说明](docs/ProductionRuntime.md)。

2026-09-14 更新：原有九个工具保持兼容，新增多关联、实时视频 OCR 和实验台分类 HTTP 接口，见 [多二维码与实时视频 OCR](docs/多二维码与实时视频OCR.md)。以下单绑定字段是兼容字段；多候选使用 `binding_ids` / `instrument_candidates` / `readings`，不可取第一条作为默认归属。

本机后台自动模式及实验员登记见 [本机自动运行](docs/本机自动运行.md)。`get_field_state` 新增 `automation` 状态；后台自动扫码独立于网页，下面手动连续扫码接口的 20 秒租约只适用于 `owner=browser` 会话。新模式未增加 Agent 工具数量。

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
| read_panel | capture_id | 异步 job_id；binding_id、crop=[x,y,width,height] 可选 |
| read_saved_panel | image_path（或 photo） | binding_id 可选；复用 NAS 已拍照片，异步 job_id |
| get_panel_result | job_id | 任务状态及完成后的原文、分数、坐标、来源 |
| end_instrument_binding | binding_id | 结束本次使用绑定 |

## Agent 调用顺序

1. get_field_state 查看仪器登记和相机状态。仪器所属场景先在网页登记，Agent 不猜测场景或操作人。
2. capture_and_scan：对准仪器码取图，即可继续绑定。场景码是可选的独立证据；实际扫到时从 scene_matches 调用 enter_scene。matches 为空时不能建立新绑定；多个码逐项关联，各仪器使用自己的解码 ID。
3. bind_instrument：使用实际扫描所得 instrument_id、scan_id 和用户提供的 operator。同相机、仪器和实验员的重复请求返回原 binding_id；同一实验员可新增其他仪器关联，已有关系保留。设备采集服务当前会话内沿用原绑定，无需每次重复扫码。成功绑定后异步预加载 PaddleOCR，绑定接口不等待模型加载完成。
4. 用户对现有 Agent 说“拍照”，沿用该 Agent 的拍照与 NAS 保存流程。本服务监控配置相机的新语音照片，未绑定也执行 OCR 和留存，稳定后自动识别；不用再调用 capture_and_scan。
5. get_field_state 查看该相机的 jobs；需要直接传递现有拍照回执时，调用 read_saved_panel，传 binding_id、确切 image_path 和可选 crop。已导入的本地 capture_id 继续用 read_panel。重复提交同一源照片与选框沿用原任务。
6. 每隔约 1 秒 get_panel_result；只有 completed 才展示 lines。调用方设置自己的总等待期限，超时保留 job_id 稍后查询，不重新提交 OCR。
7. 使用结束时 end_instrument_binding。

不传 binding_id 时，按未绑定照片识别，仪器/场景为空；自动目录导入可关联拍摄时已存在的有效绑定。显式传入 binding_id 后仍检查相机、时间、场景和二维码冲突，校验失败拒绝关联请求。crop 不传表示整图识别，不代表已自动定位面板。上传照片的 camera_id 为调用方声明，不等于硬件身份认证。

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

当前服务监听本机 127.0.0.1:8188，Agent 适配器可以运行在同一电脑。若 Agent 在挂脖设备本机，localhost 指的是挂脖设备而非此电脑，尚需受控网关或隧道接入；本轮未将无认证服务 暴露到局域网。

图片采集来自挂脖设备，二维码解码与 PaddleOCR 在服务端执行。当前为未签名二维码及本地 OCR；此接口封装不改变其身份认证和准确率边界。

## 场景进入工具

工具总数现为 9。调用 capture_and_scan 或 scan_photo 后，从 scene_matches 取得已登记的场景；调用 enter_scene，参数 {"scan_id":"扫码记录 UUID","scene_id":"场景 UUID"}，确认该照片相机进入场景。仪器码也可直接调用 bind_instrument；实际已进入场景时，系统校验同相机的当前场景是否冲突。场景可独立追加关联，不覆盖已有场景或仪器记录；同场景重复进入不会结束绑定。录入实验员仍由用户指定。get_field_state 返回 scenes 与 scene_visits；所有场景与仪器码仍未签名。


## 连续视频扫码 HTTP 接口

原有八个 Agent 工具保持兼容，新增 read_saved_panel；网页使用新增 HTTP 接口，无需逐张调用 capture_and_scan：

- `GET /api/camera/preview.mjpg`：MJPEG 实时预览，无绑定副作用。
- `POST /api/camera/scan-sessions`：参数 `{"operator":"用户指定实验员"}`。调用即选择自动进入唯一场景、自动绑定唯一已登记仪器（已有场景扫码关系时校验匹配）。
- `GET /api/camera/scan-sessions/{session_id}`：约每 0.5 秒查询。`scanning` / `waiting_camera` 继续等待；`bound` 读取原始命中帧 `scan` 和 `binding`；`needs_selection` 暂停供用户选择；`stopped` / `failed` 结束。20 秒未查询自动停止。
- `DELETE /api/camera/scan-sessions/{session_id}`：停止自动扫码，保留已完成的绑定。

已有有效绑定时 start 保留关系并继续 scanning，只为新二维码建立关联，不重复写入。设备服务离线或 receiver 的 RGB ingress 会话变化后，该相机的旧绑定及场景失效，重新在线后必须使用新照片。HTTP 连接失败本身不代表设备重启。ingress 会话也可能因设备重连/receiver 重启改变，尚不等于设备进程启动标识；详见 README 的证据边界。绑定后自动加载并常驻 OCR 模型；启用视频识别时持续抽取新鲜视频帧执行 OCR，并继续处理语音照片；普通视频帧留在内存中。


## OCR 模型生命周期

get_field_state 的 `ocr.status` 为 queued/loading 时表示后台预加载正在进行，ready 且 resident=true 表示模型已常驻。`loaded_at`、`load_count`、`load_seconds` 可供核对。后续 read_panel 复用此实例；加载期间提交的任务在同一个 OCR 工作队列中等待。绑定结束、设备采集服务离线均不卸载模型；本地识别服务重启后，启用了照片监控或仍有有效绑定时会自动重新加载。

模型加载失败不撤销成功绑定，状态显示 error，后续 bind_instrument 或 read_panel 可重试。模型常驻不等于连续执行 OCR，也不等于物理仪器准确率已经验证。


## 已有语音照片与自动识别

当前服务监控 `/mnt/realityloop-nas/voice_photos/<配置相机>/日期/时间/照片.jpg`，不要求先绑定。启动照片监控时预加载 OCR；现有 Agent 写完照片后才执行预测。轮询间隔 0.5 秒，文件稳定至少 0.5 秒；不重拍、不修改源照片、不逐帧做 OCR。照片去重跨重启、重新绑定有效。设备离线后，已导入语音照片仍完成读数；新 NAS 照片仍可识别，无法确认的仪器关联保持 null。

直接传递拍照结果的标准库调用：

```python
# 没有有效绑定时 binding_id=None；image_path 必须来自现有拍照工具返回结果。
job = tools.read_saved_panel(binding_id, image_path)
result = tools.call('get_panel_result', {'job_id': job['job_id']})
```

`read_saved_panel` / `POST /api/ocr/photo-result` 接受以下两种输入之一：

- `image_path`、可选 `binding_id` 和 `crop=[x,y,w,h]`。image_path 是已配置 voice_photos 内的具体照片绝对路径或相对路径。文件名日期/时间和相机目录均校验；无目录自动搜索、无“最新图”猜测。
- `photo`、可选 `binding_id` 和 crop。photo 包含源 `capture_id`、`camera_id`、带时区 `captured_at`、`source_ref`、纯 `image_base64`，可选原字节 `sha256`。source_ref 只作回执保留，不解释为任意下载 URL。照片相机、绑定时间、原图哈希不符则拒绝。

同一照片和选框已有任务时返回原 job_id；不要通过重发来等待结果。识别先用常驻 OCR，从任务真正运行开始计时，5 秒没有完整数字才用视觉兜底。每次最多一个云端请求，同一绑定（未绑定时同一相机）两次尝试至少隔 30 秒。收到 `completed` 后统一展示 `lines`；没有数字时如实回答看不清。`confidence`、`polygon` 允许 null，不要生成分数、坐标或额外读数。界面无需单列云端来源，后端 `local_ocr`、`fallback`、`external_photo` 保存过程和原图回执。

`get_field_state.photo_watch` 提供监控状态、待处理文件数和最近 job_id；自动识别结果位于同一状态的 jobs。此项目没有更改现有 Agent 的聊天或拍照服务，Agent 若要在对话中主动报出读数，需通过 get_field_state/get_panel_result 获取结果。网页会自动刷新显示。


## OCR 推理设备

本机通过 `FIELD_OCR_DEVICE=gpu:0` 使用 GPU；`get_field_state.ocr.device` 和本地预测的 `device` 按当前配置返回，历史结果保留原设备。`not_loaded` 不代表已调用 GPU。配置的 GPU 不可用时加载明确失败；模型常驻、照片触发、五秒兜底规则保持不变。部署和验证见 [GPU部署.md](docs/GPU部署.md)。

直接仪器绑定时，`scene_visit_id=null`、`scene_qr_verified=false`、`scene_basis=instrument_registration`；`scene.name` 只表达登记位置。实际识别并进入场景后绑定则保留真实 visit ID，并标记 `decoded_scene_qr`。不得把前者说成已经扫描场景码。`enter_scene` 可传使用者明确提供的 `operator`，自动扫码会传入已登记实验员，缺失时不补造。

`get_field_state.activity` 返回配置相机最近 50 条操作记录，包括扫码、场景进入／结束、仪器绑定／结束与面板读数。每条含 `event_id`、`kind`、`occurred_at`、`camera_id`、`operator`、`target`、`status`、`detail`、`image_url`。扫码成功但没有绑定也会出现；人员缺失保持 null，不能用当前登记者代填历史操作人。

`get_field_state.archive` 返回 NAS 留存状态：`enabled`、`status`（disabled/ready/retrying）、`pending_receipts`、`archived_receipts`、`last_error`、`last_archived_at`、`root` 和 `source_instance`。服务仍在本机运行，后台向独立 NAS 目录归档，Agent 不需要新增调用。`ready` 表示归档目标可用；确认当前全部记录归档还需 `pending_receipts=0`。不把存储成功说成读数正确或物理操作完成。目录结构与回执见 [本机运行与 NAS 留存](docs/本机运行与NAS留存.md)。

任务的 `timing` 保存源写入、首次发现、稳定、读取、导入、排队、OCR、视觉模型、最终结果时间和毫秒差值；`get_panel_result.archive` 保存最新回执的归档确认与耗时。缺失时间为 null，NAS mtime 未独立校时，补处理标记为 `is_backfill=true`。完整字段、近实时目标与边界见 [识别时延与未绑定照片](docs/识别时延与未绑定照片.md)。

`GET /api/readouts?limit=20&before=<游标>` 提供当前配置相机的读数分页，返回 `items`、`next_cursor` 和 `order=submitted_desc`。摘要包含 `job_id`、`result_url`、照片、拍摄时间、阶段时延和最新 `archive` 状态，不返回图片字节；`next_cursor=null` 表示末页。此为新增只读 HTTP 接口，九个 Agent 工具保持兼容。

调度最多同时推进四张照片，GPU 推理仍串行；每张五秒等待可重叠。视觉请求同时只允许一个，忙时记录 `fallback.status=skipped, reason=busy`，不重试、不消费该任务的冷却名额。已完成的本地数字结果可以在其他照片等待视觉请求时先交付。


NAS 全量索引：`GET /api/archive/files/Index.json` 返回全部已归档版本摘要；`GET /api/archive/files/Readme.html` 提供可筛选页面。`get_field_state.archive.integrity` 返回每日巡检状态、最近报告摘要、下次计划、最多 50 项异常及报告相对路径；完整报告位于 `/api/archive/files/<report_path>`。检查未完成与已完成但发现异常分别记录，不以 `pending_receipts=0` 替代完整性校验结论。九个 Agent 工具保持不变。
