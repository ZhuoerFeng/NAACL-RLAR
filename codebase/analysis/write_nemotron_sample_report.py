"""Package validated sample documentation and refresh checksums, without editing data."""
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "data/nemotron_english_off_1000"
WORK = BASE / "analysis/results/nemotron_sample_1000"
RAW = BASE / "data/raw/nemotron-instruction-following-chat-v2"


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def main():
    manifest = json.loads((OUT / "manifest.json").read_text())
    validation = json.loads((OUT / "validation_report.json").read_text())
    assert validation["status"] == "passed"
    provenance = [json.loads(l) for l in (OUT / "provenance.jsonl").open()]
    reviews = [json.loads(l) for l in (OUT / "review_decisions.jsonl").open()]
    rejected = [json.loads(l) for l in (OUT / "rejection_log.jsonl").open()]
    auto = sum(any(not reason.startswith("review_reject:") for reason in r["reasons"]) for r in rejected)
    reviewed_rejects = sum(any(reason.startswith("review_reject:") for reason in r["reasons"]) for r in rejected)
    full_accepts = sum(p["review_decision"] is not None and p["review_decision"]["full_conversation_reviewed"] for p in provenance)
    reviewed_accepts = sum(p["review_decision"] is not None for p in provenance)
    token_stats = manifest["token_stats"]
    categories = "\n".join(f"| `{k}` | {v} | {v/10:.1f}% |" for k,v in sorted(manifest["task_hints"].items(), key=lambda x:-x[1]))
    token_table = "\n".join(f"| {label} | {s['mean']:.1f} | {s['p50']:.1f} | {s['p95']:.1f} | {s['p99']:.1f} | {s['max']:.0f} |"
                            for key,label in [("prompt_tokens_nonthinking","Prompt，Qwen thinking 关闭"),
                                              ("prompt_tokens_thinking","Prompt，Qwen thinking 开启"),
                                              ("answer_tokens","最后一条 assistant 回答")]
                            for s in [token_stats[key]])
    repo = "https://huggingface.co/datasets/nvidia/Nemotron-SFT-Instruction-Following-Chat-v2"
    readme = f"""# Nemotron 英文 reasoning_off：1,000 条独立抽样

已执行用户确认的 **680 条单轮 / 320 条双轮**方案，随机种子 **20260923**。数据保持上游原始完整对话和字段，没有混入现有 train/valid，也没有划分新的 train/valid。

## 使用文件

- **[sample.parquet](sample.parquet)**：推荐程序读取，1,000 行。
- **[sample.jsonl](sample.jsonl)**：等价原始 JSONL，一行一条完整对话。
- [provenance.parquet](provenance.parquet) / [provenance.jsonl](provenance.jsonl)：与样本行一一对应的来源、UUID、原始行号、字节偏移、去重哈希、复核记录及 token 长度。
- [sampling_report.md](sampling_report.md)：采样方法、分类分布、质量复核范围和统计。
- [overlong_prompts.jsonl](overlong_prompts.jsonl)：任一 Qwen prompt 模式超过 10,000 tokens 的清单，本次 **{manifest['over_10000_count']} 条**，空文件是预期结果。
- [rejection_log.jsonl](rejection_log.jsonl) / [review_decisions.jsonl](review_decisions.jsonl)：剔除理由与逐条复核记录。
- [validation_report.json](validation_report.json) / [manifest.json](manifest.json)：完整性验证、版本与 SHA-256。

## 读取方式

```python
import pyarrow.parquet as pq

rows = pq.read_table("codebase/data/nemotron_english_off_1000/sample.parquet").to_pylist()
record = rows[0]
messages = record["messages"]       # system/user/assistant 的完整原始对话
prompt = messages[:-1]             # 包含所有前序历史，不含最终回答
target = messages[-1]["content"]   # 最后一条 assistant 回答
```

原始字段为 `messages`, `uuid`, `license`, `used_in`, `reasoning`。这份文件保持上游 schema；与现有 RL 数据的 `prompt` 等列并不相同。`sample_index` 与原始 `row` 均从 0 开始；`row` 属于上游 reasoning_off 文件。

来源：[NVIDIA Nemotron-SFT-Instruction-Following-Chat-v2]({repo})，固定 revision `{manifest['source_download_manifest']['revision']}`。原始 `license=ODC-By` 保留，归属 NVIDIA 及上游贡献者；详见随包保存的 [原始数据卡](SOURCE_DATASET_CARD.md) 和 [ODC-By](https://opendatacommons.org/licenses/by/1-0/)。

这是一份经质量筛选的分层随机样本。所有样本做结构和精确去重检查，最终随机 100 条完整对话加所有标记项由 Codex 阅读复核；不代表对全部回答进行了穷尽事实验证。
"""
    report = f"""# 采样执行报告

## 最终结果

| 项目 | 结果 |
|---|---:|
| 总样本 | 1,000 |
| 单轮 / 双轮（按 user 消息数） | 680 / 320 |
| 上游 reasoning 字段 | 全部 off |
| 种子 | 20260923 |
| 唯一 UUID / 标准化完整 prompt / 初始输入 | 各 1,000 |
| 与现有 cleaned train/valid 的精确 prompt 或初始输入重合 | 0 |
| 源文件逐条匹配、JSONL/Parquet 内容匹配 | 1,000 / 1,000 |
| Qwen prompt 超过 10,000 tokens | {manifest['over_10000_count']} |
| 修改或并入现有 train/valid | 无 |

`reasoning_off` 是上游模式标签，回答中仍可按用户要求出现数学步骤或解释；本次不改写消息文本。

## 来源与抽样

来源：[NVIDIA 数据集]({repo})，revision `{manifest['source_download_manifest']['revision']}`。本地已下载并校验 reasoning_off 与 reasoning_on 两个文件的官方 LFS SHA-256；本次只使用 reasoning_off。该版本实际 off 为 1,068,273 条、on 为 929,237 条，总计 1,997,510 条，比数据卡所列总数少 1,058 条；这是已核对的上游文件与数据卡差异。

1. 对 reasoning_off 全部 1,068,273 条建立索引，按首条 assistant 之前的 system/user 初始输入去重，得到 1,065,731 组。哈希只用于匹配：Unicode NFC、空白归一化、忽略空 system；输出文本不变。
2. 每组按 `SHA256(seed:member:uuid)` 选一个代表，再按其 user 轮数分层；代表分布为单轮 722,512 / 双轮 343,219。用 `SHA256(seed:group:initial_input_hash)` 排序，以固定伪随机顺序访问候选。
3. 逐个检查语言和结构，排除精确重合、明确质量问题及证据不足的高风险事实扩写，按同一层的顺序补足 680 / 320。预缓存 1,700 条候选，最终访问了 {1000+len(rejected):,} 条，其中剔除 {len(rejected)} 条。
4. 最终输出再按 `SHA256(seed:output:uuid)` 排序，因此文件不是先单轮后双轮。

680 / 320 近似保留原 reasoning_off 的轮数比例，**不是按照语义类别平均分配**，也不是全量英文子集的实测轮数比例。质量筛选及定向复核会改变分布，结果不应作为原数据集的无偏样本。

## 英文范围、去重和复核

英文范围按实际指令和生成正文判断。代码、数学符号、专名，以及作为英文翻译/改写/概念转换材料引用的外语文本允许保留；整段非英文任务指令、非英文对话历史或要求生成非英文回答的样本排除。语言识别结合 `langid`、短文本/代码处理和标记复核，仍属于实用筛选而非语言金标准标注。

所有候选做角色顺序、消息非空、模式、初始输入与完整 prompt 去重检查。与现有 cleaned train/valid 的近重复检查使用最后一条 user 文本的词级 TF-IDF 1/2-gram，取首尾各 4,000 字符；余弦相似度 ≥0.85 且文本 ≥120 字符才触发复核。这是近似检查，短寒暄和共享模板仍可相似，不能据此声称不存在语义重合。

最终样本按 `SHA256(seed:semantic_review:uuid)` 取前 100 条进行完整对话阅读复核；剔除替补后重新计算该集合，直到最终 100 条均有完整接受记录。所有自动标记项额外复核，包括可疑语言、短回答、拒答、复制输入、嵌入多题、格式/字数、外部上下文和不闭合代码围栏。化工企业简介与化工文章集中出现事实问题，因此增加了针对直接同类任务的复核触发器，没有按类别直接删除。

- 复核日志共 **{len(reviews)}** 条决定；最终保留样本中 **{reviewed_accepts}** 条有阅读复核记录，其中 **{full_accepts}** 条完整阅读，**{reviewed_accepts-full_accepts}** 条仅针对语言等标记阅读片段。最终随机 100 条包含在完整阅读集合中。
- 顺序访问中自动排除 **{auto}** 条，阅读复核排除 **{reviewed_rejects}** 条；两类原因是否重叠以逐条日志为准。
- 排除的问题包括非英文范围、损坏输入、事实/计算/代码/格式错误、未完成回答、过度拒答、缺少关键上下文。部分企业简介仅凭名称和地址扩写了具体设施、认证、研发与经营事实，因依据不足保守排除；不表示这些扩写中的每一项都已被外部证伪。
- 复核由 Codex 执行，不是外部专家或人工标注金标准；没有执行数据内代码，也没有对所有事实进行联网逐项核验。轻微表述问题可能保留。
- **剔除率不能估计全量数据错误率**：包括语言范围过滤、自动标记及定向扩展；最终 100 条是经过筛选和替补后的集合。

## 任务分布（启发式，非官方标签）

| task_hint | 条数 | 占比 |
|---|---:|---:|
{categories}

规则取最后一条 user 消息前 4,000 字符按关键词首次命中分类；最后一条少于 80 字符时拼接第一条。`other_uncertain` 表示规则未分类，不表示低质量；`script` 等词也可能使创作任务被标为技术任务。上述数值仅用于透明展示粗略覆盖，不能作为可靠的语义均衡性结论。完整规则在 `analysis/audit_nemotron.py`，本次没有按这些粗标签硬性配额。

## Qwen token 长度

使用本地 `codebase/model_tokeniser` 的 `Qwen2TokenizerFast` 与原 chat template；记录 tokenizer 文件哈希和库版本。prompt 是完整历史 `messages[:-1]` 加 generation prompt，分别统计 thinking 开启/关闭，不截断、不按长度剔除。最后回答单独计数，不加特殊 token；不把两者之和声称为完整训练序列长度。

| 统计对象 | 均值 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|---:|
{token_table}

两种 prompt 模式任一超过 10,000 即进入超长清单；本次没有此类样本。Qwen thinking 开关只用于统计模板，不改变上游 reasoning_off 标签及文本。

## 验证与复现

[validation_report.json](validation_report.json) 已通过：1,000 条与原始字节偏移读取结果逐条匹配、两种存储格式一致、唯一性和 680/320 校验、全部标记已处理、最终随机 100 条完整复核、Qwen token 独立重算。`data/cleaned` 与 `data` 两处 train/valid 共四个文件的 SHA-256 均保持不变。

从项目根目录使用现有环境：

```bash
/tmp/rlar-data-audit-venv/bin/python codebase/analysis/sample_nemotron_1000.py prepare
/tmp/rlar-data-audit-venv/bin/python codebase/analysis/validate_nemotron_sample.py
```

`prepare` 使用已保存决定重建候选顺序和清单；`finalize` 在复核队列为空时导出，并拒绝静默覆盖已有 `sample.jsonl`。复现精确选择需要保留固定源版本、`ordered_groups.parquet`、候选缓存和 `review_decisions.jsonl`；语义判断本身不由随机种子自动重现。脚本、tokenizer、决定日志、排序索引和产物 SHA-256 保存在 manifest。

原始字段与 ODC-By 许可证完整保留；数据出处与归属见 [README](README.md) 及 [上游数据卡](SOURCE_DATASET_CARD.md)。
"""
    (OUT / "README.md").write_text(readme)
    (OUT / "sampling_report.md").write_text(report)
    (OUT / "SOURCE_DATASET_CARD.md").write_bytes((RAW / "README.md").read_bytes())
    execution = {"status": "executed", "completed_utc": datetime.now(timezone.utc).isoformat(),
                 "approved_scope": "English reasoning_off, 680 single-turn / 320 two-turn", "rows": 1000,
                 "seed": 20260923, "output_directory": str(OUT), "no_train_valid_merge": True,
                 "sample_jsonl_sha256": sha(OUT / "sample.jsonl"), "validation_status": "passed"}
    for path in [OUT / "execution_record.json", BASE / "analysis/results/nemotron_sampling_review/execution_record.json"]:
        path.write_text(json.dumps(execution, indent=2) + "\n")
    manifest["quality_review"].update({"decision_log_rows": len(reviews), "visited_candidates": 1000+len(rejected),
        "visited_rejections": len(rejected), "selected_complete_conversation_reviews": full_accepts,
        "selected_focused_reviews": reviewed_accepts-full_accepts,
        "reviewer": "Codex agent, not external human/expert gold annotation",
        "targeted_expansion": "Direct chemical-company introductions and chemical-industry articles after observed clustered unsupported claims."})
    manifest["reproduction_files_sha256"].update({str(p.relative_to(BASE)):sha(p) for p in [Path(__file__), BASE / "analysis/validate_nemotron_sample.py"]})
    manifest["files"] = {p.name:{"bytes":p.stat().st_size,"sha256":sha(p)} for p in sorted(OUT.iterdir()) if p.is_file() and p.name != "manifest.json"}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"out":str(OUT), "reviewed_selected":reviewed_accepts,"full_reviewed_selected":full_accepts,
                      "rejected":len(rejected),"auto":auto,"review_rejected":reviewed_rejects,
                      "tokens":token_stats,"files":len(manifest["files"])},indent=2))


if __name__ == "__main__":
    main()
