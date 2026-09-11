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
| get_panel_result | job_id | 任务状态及完成后的原文、分数、坐标、来源 |
| end_instrument_binding | binding_id | 结束本次使用绑定 |

## Agent 调用顺序

1. get_field_state 查看仪器登记和相机状态。仪器所属场景先在网页登记，Agent 不猜测场景或操作人。
2. capture_and_scan：对准二维码取图。matches 为空时不能建立新绑定；多个码时由用户明确选择仪器。
3. bind_instrument：使用实际扫描所得 instrument_id、scan_id 和用户提供的 operator。
4. 对准面板再次 capture_and_scan，即使没有二维码也会返回新 capture_id。
5. read_panel：传当前绑定和新照片，可附面板像素选框。服务立即返回 job_id，不等待模型执行。
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

工具总数现为 8。调用 capture_and_scan 或 scan_photo 后，从 scene_matches 取得已登记的场景；调用 enter_scene，参数 {"scan_id":"扫码记录 UUID","scene_id":"场景 UUID"}，确认该照片相机进入场景。之后扫描仪器码并调用 bind_instrument，系统校验同相机的当前场景。切换场景会结束旧仪器绑定；同场景重复进入不会结束绑定。录入实验员仍由用户指定。get_field_state 返回 scenes 与 scene_visits；所有场景与仪器码仍未签名。
