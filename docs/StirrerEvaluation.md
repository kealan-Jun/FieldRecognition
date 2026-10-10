# 搅拌器离线评测

[`scripts/evaluate_stirrer.py`](../scripts/evaluate_stirrer.py) 读取两份显式 JSON，将温度、转速输出与参考逐字段比较。CLI 不调用 OCR、不加载模型、不读取图片或数据库，也不连接相机、NAS 或服务。真实模型回放和原件核验由独立采集／回放流程完成。

主指标叫 `reference_agreement`，表示**本批输出与所提供参考的一致率**。它不能自动解释为仪器识别准确率。`assistant_visual_review` 是助手看图形成的参考，不是人工真值；`human_ground_truth` 必须显式声明 `human_verified: true`，并提供非空 `reviewer`。脚本校验声明格式，不验证审核者身份、标注正确性或测试集独立性；报告保持 `PARTIAL_EVIDENCE` 和 `independent_test_set: NOT_PROVEN`。

## 输入约定

参考顶层使用 `schema: stirrer-reference/1`、`reference_kind` 和 `samples`。每项必须有按输入顺序排列的 `sample_id: 01, 02, ...`、小写 64 位 `image_sha256`、非空 `target_instrument_id`、`scope` 和 `fields`。同一参考文件的目标仪器 ID 必须一致。

预测顶层使用 `schema: stirrer-predictions/1`、非空 `run_id` 和 `samples`。每项用 `image_sha256` 联结，可附带 `job_id`、`pipeline_status`、`target_instrument_id`；若声明目标 ID，必须与参考一致。一个文件对应一个固定运行版本，不将多个版本的最好结果合并。

两类输入的 `fields` 都必须恰好包含 `temperature`、`speed`：

| 对象 | 状态 | 规则 |
| --- | --- | --- |
| 参考字段 | `numeric` | `value` 是有限数字或数字字符串；可提供 `unit` |
| 参考字段 | `off` | 明确可见 OFF；`value` 缺省或为 null，不能填 0 |
| 参考字段 | `unreadable` | 能看到该字段，但参考无法可靠判读；不猜数字 |
| 参考字段 | `not_visible` | 该字段不在画面内；与无法判读分开记录 |
| 预测字段 | `numeric`、`off` | 与上面相同的数值／状态契约；可附带 `unit`、`unit_basis` |
| 预测字段 | `unrecognized` | 无有效字段输出；`value` 缺省或为 null |

`scope: target` 表示本次目标样本；`scope: context_only` 只记录背景／关联上下文，两字段都不进入识别一致率分母。参考不可判读、不可见时仅排除相应字段，另一可判读字段仍可计分。

已有完整 job 可通过 `normalize_job(job, instrument_id)` 转换。适配器严格筛选 `display_fields` 的目标 `instrument_id`，明确映射 `温度 → temperature`、`转速 → speed`，也接受这两个英文名。缺字段、同字段多个输出、失败任务或冲突值转为 `unrecognized`；不从绑定上下文、允许名单、其他仪器或 raw OCR 候选补造目标读数。手工生成预测时也应遵守这一规则。

## 哈希与原件

每份输入中，同一 `image_sha256` 只能出现一次，完全相同的重复项也会被拒绝。预测出现参考之外的哈希会被拒绝；参考缺预测时按未识别处理，保留其可评字段的分母。图片名、任务数或重放次数不能代替唯一图片数。

联结键的字节含义由采集清单声明。历史回放可能需要同时记录历史规范化图、原始照片和本轮转换图，清单／参考顶层可附加如下角色说明：

```json
{
  "hash_roles": {
    "image_sha256": "historical_normalized_png_sha256",
    "original_sha256": "original_bytes_sha256",
    "replay_image_sha256": "current_original_decode_then_png_sha256"
  }
}
```

这里的 `hash_roles` 是额外的来源说明，不是 CLI 新选项。评分器只校验联结键的格式、唯一性和对应关系，不校验或保留这些额外元数据，不读取文件来验证哈希。回放流程必须另行核验原始字节及转换关系；不能将历史关联哈希声称为本轮推理输入的字节哈希，也不能把三种角色当成三张独立样本。

## 合成示例

以下是**纯离线格式测试**：数值、仪器 ID、预测和 `a…a` 哈希均为人为构造，不对应真实图片或模型调用。它仅验证评分方法。已建好项目 `.venv` 后，从仓库根目录执行：

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path

root = Path("tmp/StirrerEvaluationExample")
root.mkdir(parents=True, exist_ok=True)
fields = {
    "temperature": {"state": "numeric", "value": "25.0", "unit": "°C"},
    "speed": {"state": "off", "value": None, "unit": "rpm"},
}
references = {
    "schema": "stirrer-reference/1",
    "synthetic_fixture": True,
    "reference_kind": "assistant_visual_review",
    "samples": [{
        "sample_id": "01",
        "image_sha256": "a" * 64,
        "target_instrument_id": "synthetic-stirrer",
        "scope": "target",
        "fields": fields,
    }],
}
predictions = {
    "schema": "stirrer-predictions/1",
    "synthetic_fixture": True,
    "run_id": "synthetic-format-test",
    "samples": [{
        "image_sha256": "a" * 64,
        "job_id": "synthetic-job",
        "pipeline_status": "completed",
        "target_instrument_id": "synthetic-stirrer",
        "fields": {
            "temperature": {
                "state": "numeric", "value": "25", "unit": "°C",
                "unit_basis": "instrument_registry",
            },
            "speed": {"state": "off", "value": None, "unit": "rpm"},
        },
    }],
}
for name, data in (("references", references), ("predictions", predictions)):
    (root / f"{name}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
PY
.venv/bin/python scripts/evaluate_stirrer.py \
  tmp/StirrerEvaluationExample/references.json \
  tmp/StirrerEvaluationExample/predictions.json \
  > tmp/StirrerEvaluationExample/report.json
```

这个例子的温度数字一致数为 1/1、转速 OFF 一致数为 1/1、双字段完整一致数为 1/1，均只是合成格式结果。数字使用 Decimal 比较，`25` 与 `25.0` 数值相等，不统计字符显示精度；没有近似容差，OFF 与零永远分开。

## 读报告

每字段的 `numeric`、`off` 分别记录 `eligible`、`correct`、`misread`、`unrecognized` 和 `reference_agreement`。错误数字或错误状态属于 `misread`；无有效输出属于 `unrecognized`。缺预测同样属于未识别，不从分母删除。没有可评字段时比率为 null，不能称为全对。

`coverage` 统计有数字或 OFF 输出的可评字段比例，包含误读；它不表示正确率。`both_eligible_complete` 只统计两字段参考都可评的图片，区分两个字段都有输出与两个字段都一致。`possible_field_misassignment` 仅在两个不同的数字参考被预测完整交换时标记疑似字段互换；两字段仍计误读，不声称已证实空间定位错误。

`output_unit_agreement` 只对参考为数字且明确给出单位的字段另计输出单位一致性，并保留 `unit_basis` 计数。`instrument_registry` 表示单位来自登记配置，不证明 OCR 看到了单位；单位匹配不使错误数字变正确，数字匹配也不掩盖错误单位。

字节哈希去重仍不能消除近重复：同一 burst 的连续照片、相邻视频帧和同一历史问题图的变体往往高度相关。已知问题图的选择偏差、模型训练／调参是否接触这些图、光照角度与数值分布、参考来源都应随报告保存。没有独立抽样、盲法人工核对和分布覆盖时，只报告这批参考一致性，不能外推长期现场准确率。实际评测结果另行记录，不用本页的合成示例代替。

## 2026-10-10 历史照片的本地分支回放

识别源码 `1e0da1427e7df8a18bbd1e11d828b88296c00f31` 的一次有界回放使用当前本地 CPU 面板检测和常驻 GPU OCR，关闭云端视觉主识别／兜底；测试专用仪器允许名单不证明生产扫码或实物绑定。36 张不同原始字节照片来自 2026-09-21 至 2026-09-25 的 43 个实际搅拌器输出任务，未覆盖更早记录。两次助手视觉复核在新回放前冻结了 38 个数值、10 个 OFF；另 24 个字段因模糊、像素不足或小数点不明确而不可评，未经使用者核对。

| 与视觉参考一致的范围 | 一致数 / 可评数 | 一致率 |
| --- | ---: | ---: |
| 温度（数值与 OFF） | 9 / 22 | 40.9% |
| 转速（数值与 OFF） | 7 / 26 | 26.9% |
| 全部可评字段 | 16 / 48 | 33.3% |
| 两项同时一致的图片 | 6 / 22 | 27.3% |

48 个可评字段中另有 3 个误读、29 个未识别；10 个 OFF 仅 2 个一致。36 个任务均完成，回执审计确认 98 次实际 OCR 调用和同一模型版本，不能把任务完成率解释为识别正确率。29 个未识别字段中，16 个因单窗角色未确认，13 个没有形成可用数字／OFF；3 个误读都来自同一组三连拍的 670 → 60。双窗口漏定位、低像素数字漏位及倒置／远景 OFF 是本批主要问题。首次评测环境启动失败的 0 调用回放单独排除，不参与成绩。

本结果为 `PARTIAL_EVIDENCE`。36 个原件按来源元数据形成 15 个描述性照片簇，不能视为 36 个独立场景。历史任务选择偏差、近重复连拍、未核实的训练／调参重叠以及助手参考限制仍存在，不能外推现场长期准确率，也不代表开启视觉模型后的混合识别结果。参考单位没有足够可见依据，因此没有单位识别成绩。原图、标签和回执按部署的私有评测留存约定保存，不随源码公开。

## 2026-10-10 搅拌器优化后的固定批次比较

候选识别源码为 `ea2272129011942347ac7a159a33891c24b71fa0`，基线仍为上述 `1e0da14`。本轮改进照片双窗口漏定位后的上下文检测、红色 LED 的源通道对比度与旋转 OCR，并加强低像素漏位、OFF／数字冲突的质量门。重试视图一致且通过几何和字段规则后才输出；不补造缺失数字、小数点或仪器身份。像素修复使用原件分辨率下限，放大图不能绕过该下限；正常 OCR 读数不因像素较低自动被拒绝。视频不启用新增的照片重试路径。

开发批次沿用冻结的全部 36 张原件和 48 个可评字段；完整运行前后比较，不按图片或字段挑选不同版本的最好输出。

| 开发批次范围 | 基线一致数 / 可评数 | 候选一致数 / 可评数 |
| --- | ---: | ---: |
| 温度（数值与 OFF） | 9 / 22（40.9%） | 16 / 22（72.7%） |
| 转速（数值与 OFF） | 7 / 26（26.9%） | 12 / 26（46.2%） |
| 全部可评字段 | 16 / 48（33.3%） | 28 / 48（58.3%） |
| 两项同时一致的图片 | 6 / 22（27.3%） | 10 / 22（45.5%） |
| OFF 字段 | 2 / 10（20.0%） | 2 / 10（20.0%） |

开发批次新增 12 个一致字段，原有 16 个一致字段全部保留；误读从 3 个降至 0 个，未识别从 29 个降至 20 个。其中三个 670 → 60 现在保留原候选和质量原因、拒绝正式数值，没有补回缺失的 7。OFF 没有改善，低像素、漏定位和倒置读数仍有未识别情况。

另选 2026-09-15 至 2026-09-20 的历史照片作本轮未用于调参的验证批次。按来源分组与时间顺序确定抽样，原始字节去重后为 11 张；助手在新基线回放前冻结 5 个数值和 4 个 OFF，另外 13 个字段无法可靠判读。该批次没有交给实现审查代理，候选源码在验证回放前冻结；验证结果出来后没有继续据此调参。抽样和字节去重仍不证明独立测试集，训练重叠、近重复和参考正确性均未核实。

| 历史验证范围 | 基线一致数 / 可评数 | 候选一致数 / 可评数 |
| --- | ---: | ---: |
| 温度（数值与 OFF） | 0 / 5（0.0%） | 0 / 5（0.0%） |
| 转速（数值与 OFF） | 2 / 4（50.0%） | 2 / 4（50.0%） |
| 全部可评字段 | 2 / 9（22.2%） | 2 / 9（22.2%） |
| 两项同时一致的图片 | 0 / 3（0.0%） | 0 / 3（0.0%） |
| OFF 字段 | 1 / 4（25.0%） | 1 / 4（25.0%） |

验证批次前后均有 1 个误读、6 个未识别，逐字段计分结果没有改变。**本轮没有证明普遍准确率提升，也没有消除所有误读。** 验证可评字段仅 9 个，不能把开发集的 58.3% 声称为现场准确率；两批仍为 `assistant_visual_review`、`PARTIAL_EVIDENCE`，独立测试集和单位识别均为 `NOT_PROVEN`。

重试带来额外调用与时延。下面计时是该本机顺序回放中单张任务的处理墙钟时间，含本地检测、OCR 请求／等待等，不含真实拍照、文件发现、归档和用户端展示；基线与候选顺序运行，没有控制共享 GPU 的其他负载，不能解释为生产端到端时延承诺。

| 成本 | 开发基线 → 候选 | 历史验证基线 → 候选 |
| --- | ---: | ---: |
| 实际 OCR 调用数 | 98 → 231 | 29 → 99 |
| 单张处理时间中位数 | 2.05 → 4.27 秒 | 1.87 → 5.68 秒 |
| 单张处理时间最大值 | 6.46 → 7.88 秒 | 3.98 → 8.88 秒 |

两个候选批次的 47 个任务全部完成；330 次实际 OCR 执行都有唯一回执，裁图哈希和模型版本检查通过，无迟到结果参与成绩。权重没有更换，云端视觉始终关闭，测试允许名单不构成二维码身份或生产绑定证据；任务、标签和 SQLite 均在隔离数据目录。原件、冻结参考、逐张输出、调用回执及比较报告按私有评测约定归档。

候选通过 929 项本地回归测试及 Python 语法、Ruff 检查。升级检查要求离线部署：代码同步不表示现场服务已切换，本轮未重启服务。进入现场使用前仍需要新的盲法人工核对与代表性样本，评估未识别、剩余误读和增加的耗时；当前结果不足以作为普遍准确率提升的上线依据。
