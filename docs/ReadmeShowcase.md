# README 界面演示说明

README 的截图来自项目现有页面，使用独立的本机只读演示接口与合成素材。它们用于展示工作台、查询和候选复核方式，不能作为现场识别准确率或生产运行状态的证据。

## 页面与素材

| 展示 | 页面源码 | 演示素材 |
| :--- | :--- | :--- |
| 相机详情工作台 | [index.html](../static/index.html)、[app.js](../static/app.js) | [桌面截图](assets/readme-demo/workbench.png) · [窄屏截图](assets/readme-demo/workbench-mobile.png) |
| 按采集时间追溯 | [trace.html](../static/trace.html)、[trace.js](../static/trace.js) | [时间轴截图](assets/readme-demo/timeline.png) |
| 测量草稿复核 | [photo-measurements.html](../static/photo-measurements.html)、[photo-measurements.js](../static/photo-measurements.js) | [复核截图](assets/readme-demo/review.png) |
| 三张合成输入 | 人工绘制的演示面板，无现场照片 | [第 1 张](assets/readme-demo/input-1.png) · [第 2 张](assets/readme-demo/input-2.png) · [第 3 张](assets/readme-demo/input-3.png) |
| 身份示意与回执 | 合成二维码及预置接口数据 | [二维码](assets/readme-demo/demo-qr.png) · [完整演示回执](assets/readme-demo/receipt.json) |

截图直接加载原页面的 HTML、CSS 与 JavaScript。演示服务器只添加醒目的演示说明，以及选中页面、查询固定时间范围和展开最近结果的操作脚本；没有修改产品源码或重新绘制产品界面。

演示在独立工作区、临时端口和浏览器配置目录中完成。服务器不导入 `app.py`，不创建业务数据库，不读取运行数据、环境凭证或现场照片，不连接 Receiver、真实相机、GPU、云端模型或 NAS。演示写接口统一拒绝，页面中的确认按钮不用于提交正式记录。

## 完整示例的含义

1. **输入：** 三张合成面板分别显示 24.5 °C、1200 rpm 和明确的 `SYNTHETIC` 标记；三张图片均为人工生成。
2. **候选：** 相同数值通过演示接口预置，`candidate_source=preset_fixture`、`ocr_executed=false`。逐张候选的置信分数为空，处理耗时为空，没有执行模型推理。
3. **关联：** 图片中的二维码编码演示仪器 `demo-stirrer-01`。OpenCV `QRCodeDetector` 从二维码及完整合成图分别解码，核对内容一致；绑定快照和人员时段由演示接口预置，不是实际调用绑定事务所得。
4. **时间与批次：** 2026-10-10 10:30:00–10:30:02（北京时间）是合成声明时间。三连拍关系预置为同一个演示批次，不证明上游拍照或硬件时钟同步。
5. **回执：** 原图 SHA-256 来自提交的合成图片实际字节。草稿保持测试用途；归档状态保持 `pending`，未验证 NAS 发布，也未生成正式实验记录。

界面原有的“主码流在线”“实时视频”“识别原文”等标签来自演示接口状态；应与图片和截图中的演示说明一起阅读。它们不表示真实设备在线或模型实际执行。

## 可核对的证据

| 状态 | 本次说明 |
| :--- | :--- |
| **`PROVEN`** | 现有界面可用上述预置数据呈现；合成二维码可离线解码；回执中的图片哈希与提交素材一致。 |
| **`PARTIAL_EVIDENCE`** | 只覆盖只读演示数据下的页面呈现，未连接生产 API 或完整业务事务。 |
| **`NOT_PROVEN`** | 真实仪器准确率、相机采集与三连拍闭环、硬件校时、生产时延、NAS 归档和正式确认。 |

以下核对仅处理仓库中的合成图片与 JSON，使用本项目 Python 环境；不调用任何服务：

```bash
.venv/bin/python - <<'PY'
import hashlib, json
from pathlib import Path
import cv2

root = Path('docs/assets/readme-demo')
receipt = json.loads((root / 'receipt.json').read_text())
assert receipt['demo_only'] is True
assert receipt['candidate_source'] == 'preset_fixture'
assert receipt['ocr_executed'] is False
for index, source in enumerate(receipt['measurement']['sources'], 1):
    image = root / f'input-{index}.png'
    assert hashlib.sha256(image.read_bytes()).hexdigest() == source['image_sha256']
    payload, _, _ = cv2.QRCodeDetector().detectAndDecode(cv2.imread(str(image)))
    assert payload == receipt['qr_decode']['payload']
print('Synthetic image hashes and QR payloads verified; OCR was not executed.')
PY
```

README 内容与截图更新仅发布到 GitHub；运行中的服务源码与资源没有在本次迭代中替换。
