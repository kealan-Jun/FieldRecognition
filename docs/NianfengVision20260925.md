# 年丰视觉模型与照片优先识别

通过 `.env.example` 中的 `FIELD_VISION_PROVIDER=nianfeng`、`FIELD_VISION_ENABLED=1`、`FIELD_VISION_API_KEY`、`FIELD_VISION_MODEL` 明确选择年丰兼容接口。`FIELD_VISION_BASE_URL` 只接受 `https://api.nianfeng.tech/v1`；钱包页面不是推理接口。密钥放在未跟踪的环境文件，不写入回执。阿里云默认配置保持兼容。

`FIELD_VISION_PHOTO_MODE=primary` 使无绑定照片直接调用所配置视觉模型，避免整图 OCR 中的背景数字或错误整数阻止视觉调用。每张照片排队等待最多 120 秒，视觉请求仍串行；该模式不受每相机 30 秒兜底冷却影响。失败时保留云端失败回执并尝试本地 OCR；模型明确返回不可读时不生成读数。绑定照片仍先定位已授权面板，再对各面板优先调用视觉模型；视频原规则不变。此模式会增加外部调用次数及费用。

原图、选框哈希、原始模型回答、返回模型名和用量保留。无绑定视觉结果为 `needs_review` 候选，`binding_id` 为空，`human_verified=false`；不根据视觉文字补填仪器身份或生成正式测量确认。源照片实际二维码解码得到的身份可在任务中单独保留，不等于已确认这些数字属于该仪器，也不创建绑定。

## 阿里云 Flash：速度优先的照片配置

使用已有的 `DASHSCOPE_API_KEY`，在本地配置中明确选择以下参数：

```dotenv
FIELD_VISION_PROVIDER=aliyun
FIELD_ALIYUN_FALLBACK_ENABLED=1
FIELD_ALIYUN_MODEL=qwen3.8-flash
FIELD_ALIYUN_ENABLE_THINKING=0
FIELD_VISION_PHOTO_MODE=primary
```

`FIELD_ALIYUN_ENABLE_THINKING` 只控制阿里云请求，支持 `0/1`、`false/true`、`no/yes`，省略时保持原有的开启思考行为。关闭后明确发送 `enable_thinking=false`，不再附带思考预算；年丰请求不发送这两个参数。已有用户确认的显示规则继续按原条件使用，无绑定候选仍需复核，不自动补造身份、数字或正式确认。公开配置与新阿里云回执记录思考开关，历史回执保持原样。

模型和开关随应用工厂的配置快照传入识别流程；修改环境文件不会热更新正在运行的 worker。现有共享源码部署仍须遵守 [不中断升级约束](RuntimeReliability.md#升级入口)，先完成隔离验证与升级检查；未经明确授权不得重启线上服务或修改已加载的源码、配置。

阿里云官方说明：[Qwen3.8-Flash 模型能力](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)、[关闭深度思考](https://help.aliyun.com/zh/model-studio/deep-thinking)。速度与仪表读数效果须以实际请求及人工真值核对为准。

2026-10-14 隔离验证：完整回归由基线 1117 项增加到 1134 项，均通过（`PROVEN`，限于测试覆盖的协议与配置行为）。复用已有阿里云凭据，向上述模型发起共三次请求，未重试同一照片；第一份响应因本地回执保存脚本错误未保留，调用结果与费用为 `NOT_PROVEN`。其余两份完整回执耗时分别为 3.182 秒、3.749 秒（`PARTIAL_EVIDENCE`，仅本批同步调用，未含生产队列与归档）。第一份因将模糊字符输出为非数字而被 `invalid_answer` 校验拒绝；第二份通过协议校验，但助手目视发现其将疑似 `OFF` 显示转录为 `056`，不能当作读数正确。样本没有人工真值，与历史年丰回执的不同时间调用也不构成受控速度对比；仪器准确率和长期生产时延仍为 `NOT_PROVEN`。线上源码、配置、服务与历史结果均未切换或改写。

## 用户确认的类型显示模板

本次用户明确质量单位 g、固定四位小数，且人工查看样张确认电子天平显示。可通过 `FIELD_VISION_DISPLAY_RULES` 配置 `.env.example` 的显式模板。只有用户确认标志为 true 且模型明确输出 `label=质量`、`unit=g` 时才应用，不能从唯一登记仪器猜类型。

完整整数字符串 `144062` 可依确认的固定显示规则规范化为 `14.4062`，原始字符串、原始模型回答及规则依据保留；身份仍为空。已经识别出小数点的 `144.062` 不移动小数点，而是标记 `display_precision_mismatch`，`normalized_value.value=null`。类型/单位不明、冲突模板、缺位和模糊数字不补写。

这只证明流程和规则约束，不能证明模型读数正确。实际模型可用性、速度和真实仪器准确率必须以现场回执和人工核对为准；配置模型名称并不代表该模型已被调用。

绑定照片定位为空，或所有定位面板均未给出数字/明确显示状态时，primary 模式会再对原请求照片范围调用一次视觉识别。该恢复路径只产生未归属候选，不把整图数字挂到已有绑定；原定位、局部云端回答和裁剪记录保留于 `localized_attempt`，绑定快照保留。已有面板读数不触发全图恢复。恢复失败时保留原处理结果，并将新失败回执写入 `whole_photo_recovery`。
