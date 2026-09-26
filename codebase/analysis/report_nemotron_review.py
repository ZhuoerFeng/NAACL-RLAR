#!/usr/bin/env python3
"""Build the review artifact from completed, verified audit outputs."""
import json
from pathlib import Path
import time

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "analysis/results/nemotron_sampling_review"
NAMES = {"translation":"翻译", "summarization":"摘要", "rewrite_edit":"改写／校对", "code_technical":"代码／技术", "math_quantitative":"数学／定量", "extraction_format":"抽取／格式", "creative_generation":"创作／写作", "planning_advice":"规划／建议", "knowledge_explanation":"知识／解释", "other_uncertain":"其他／未识别"}

def percent(n, total):
    return f"{n:,}（{100*n/total:.2f}%）"

def main():
    while not (OUT / "diagnostic_metrics.json").exists():
        time.sleep(5)
    m = json.loads((OUT / "full_audit_metrics.json").read_text())
    d = json.loads((OUT / "diagnostic_metrics.json").read_text())
    s = m["sections"]
    total = sum(x["rows"] for x in s.values())
    lines = ["# Nemotron Instruction-Following Chat v2：采样前检查与方案", "",
        "状态：原始全集已下载并验证 SHA-256；全量结构扫描、诊断样本统计已完成。尚未产生最终 1,000 条样本，等待协商采样方案。", "",
        "## 来源与统计口径", "",
        "- 官方仓库：https://huggingface.co/datasets/nvidia/Nemotron-SFT-Instruction-Following-Chat-v2",
        f"- 固定 revision：`{m['download']['revision']}`。两个文件的完整哈希已与 HF LFS 对照。",
        "- HF 仓库创建于 2026-03-08；数据卡记录生成日期 2025-11-20。发布时间不代表所有底层问题均为新问题。",
        "- 许可 ODC-By；原始 license、uuid、reasoning、used_in 元数据保留。",
        "- 精确全量统计：记录数、角色、空值、轮数、规范化文本重复。",
        "- 规则估计：任务分类由关键词规则产生，不是官方标签；一个任务可命中多个信号，主类采用优先规则。",
        "- 样本估计：每个 reasoning 部分随机诊断 8,192 条，用本地 Qwen tokenizer 和 langid 统计长度／语言。诊断样本不是最终 1,000 条。",
        "- 质量个案：只证明特定失败存在，不能外推全集错误率。结构有效也不代表答案正确。", "",
        "## 全量结构与重复", "",
        "| 指标 | reasoning_off | reasoning_on |", "|---|---:|---:|"]
    for label, key in [("记录数", "rows"),("结构有效记录", "structurally_valid_rows"),("不同完整 prompt", "unique_prompts"),("重复完整 prompt 的冗余行", "redundant_prompt_rows"),("不同首轮会话输入", "unique_conversation_seeds"),("相同首轮输入的冗余行", "redundant_conversation_seed_rows"),("不同 UUID", "unique_uuids")]:
        lines.append(f"| {label} | {s['reasoning_off'][key]:,} | {s['reasoning_on'][key]:,} |")
    lines += ["", f"合计 **{total:,}** 条；数据卡宣称 1,998,568 条，差值为 {total-1998568:+,} 条。", "",
        "完整 prompt 指去掉最后 assistant 后的上下文，NFC＋空白归一化，并忽略空 system 占位。首轮输入指首次 assistant 之前的消息；这是文本分组，不保证语义近似去重。", "",
        "跨 reasoning 部分的重叠：", "", "```json", json.dumps(m["between_sections"],ensure_ascii=False,indent=2), "```", "",
        "### 对话轮数", "", "| 用户轮数 | reasoning_off | reasoning_on |", "|---|---:|---:|"]
    for turn in sorted(set(s['reasoning_off']['user_turns']) | set(s['reasoning_on']['user_turns']),key=int):
        lines.append(f"| {turn} | {percent(s['reasoning_off']['user_turns'].get(turn,0),s['reasoning_off']['rows'])} | {percent(s['reasoning_on']['user_turns'].get(turn,0),s['reasoning_on']['rows'])} |")
    lines += ["", "### 结构异常与待审信号", "", "| 信号 | reasoning_off | reasoning_on |", "|---|---:|---:|"]
    for key in sorted(set(s['reasoning_off']['flags']) | set(s['reasoning_on']['flags'])):
        lines.append(f"| `{key}` | {s['reasoning_off']['flags'].get(key,0):,} | {s['reasoning_on']['flags'].get(key,0):,} |")
    lines += ["", "空 system 可作为占位处理；短答案、非拉丁文字、内嵌问答和拒答均只是待审信号，不默认判为低质量。与现有 cleaned 数据的重叠仅检测完整 prompt 的规范化匹配，未证明不存在改写／近似重叠。", "",
        "## 任务覆盖：规则分类，不是人工真值", "", "| 规则主类 | reasoning_off | reasoning_on |", "|---|---:|---:|"]
    for key, name in NAMES.items():
        lines.append(f"| {name} | {percent(s['reasoning_off']['task_hints'].get(key,0),s['reasoning_off']['rows'])} | {percent(s['reasoning_on']['task_hints'].get(key,0),s['reasoning_on']['rows'])} |")
    lines += ["", "限制：分类基于最后 user 前 4,000 字符，短追问会附上首轮 user。内嵌多个 Q/A、非英语指令、引用文本和依赖上下文的请求可能误分。不能直接将这些规则主类当作严格语义均衡配额。", "",
        "## Qwen 长度与语言：随机诊断样本", "", "Qwen3 本地 tokenizer；完整历史加 chat template 和 generation prompt；无截断。最终答案 content 与 reasoning_content 分开计数。", "",
        "| 指标（tokens） | reasoning_off | reasoning_on |", "|---|---:|---:|"]
    for label, field, stat in [("prompt 中位数", "prompt_tokens_thinking", "p50"),("prompt P95", "prompt_tokens_thinking", "p95"),("prompt P99", "prompt_tokens_thinking", "p99"),("最终答案中位数", "answer_tokens", "p50"),("最终答案 P95", "answer_tokens", "p95"),("独立 reasoning 中位数", "reasoning_tokens", "p50")]:
        lines.append(f"| {label} | {d['sections']['reasoning_off'][field][stat]:,.0f} | {d['sections']['reasoning_on'][field][stat]:,.0f} |")
    lines += ["", "| Prompt 长度桶 | reasoning_off / 8,192 | reasoning_on / 8,192 |", "|---|---:|---:|"]
    for bucket in d['sections']['reasoning_off']['prompt_token_bins_thinking']:
        lines.append(f"| {bucket} | {percent(d['sections']['reasoning_off']['prompt_token_bins_thinking'][bucket],8192)} | {percent(d['sections']['reasoning_on']['prompt_token_bins_thinking'][bucket],8192)} |")
    lines += ["", "语言提示（langid 对最后 user 前 4,000 字符判别；短输入和代码误差更大）：", "", "| 语言代码 | reasoning_off / 8,192 | reasoning_on / 8,192 |", "|---|---:|---:|"]
    langs = sorted(set(d['sections']['reasoning_off']['language_hints']) | set(d['sections']['reasoning_on']['language_hints']),key=lambda l: -sum(x['language_hints'].get(l,0) for x in d['sections'].values()))
    for lang in langs[:12]:
        lines.append(f"| {lang} | {percent(d['sections']['reasoning_off']['language_hints'].get(lang,0),8192)} | {percent(d['sections']['reasoning_on']['language_hints'].get(lang,0),8192)} |")
    lines += ["", "## 已核实的质量个案", "",
        "1. `3e70b735-9eae-4d59-9c36-f5b9060179e5`：先将托盘能放多少包裹译成德语，随后要求法语版本，答案却翻译了追问本身。完整上下文依赖处理失败。",
        "2. `92a69ba8-1685-4a27-8447-1a5b4e0f8ec1`：最终请求为 2,000 词文章，答案仅 437 个空白分隔词；结束语约束满足，长度约束明显未满足。",
        "3. `a946615e-2794-4eab-87d4-9f6009cd09c5`：明确要求单字母分类，答案 B 符合格式，不能按短答案过滤。",
        "4. `6e0ef890-adb3-4c9a-b930-02dd2ebbb808`：user 内含多个 Q/A，开头是音乐问题、结尾才是法语医学请求；不能只读开头就认定答非所问。",
        "", "以上来自下载过程中按数据块选取的探索性个案，不是均匀记录抽样，不用于计算质量失败率。完整证据与检查范围见 `exploratory_quality_findings.json`。", "",
        "## 待协商的采样方案", "",
        "### 推荐方向：补回通用指令用途", "",
        "- 范围建议：reasoning_off；语言范围由用户确认。reasoning_off 表示没有独立 reasoning 字段，不保证 content 内没有逐步解释。",
        "- 抽样单位：同一首轮问题／完整 prompt 的组，组间不放回抽样，组内随机取一条。记录组定义；这样追求问题覆盖，会改变按原始记录抽样的分布。",
        "- 明确排除结构不可用记录、确认答非所问或显著违反要求的回答；空 system、短答案、拒答、长样本不自动删除。",
        "- 推荐先以可靠的对话轮数分层，按选定范围内的比例随机抽取；任务类别作为人工抽检与覆盖报告，不直接按关键词十类等量。reasoning_off 原始单轮／两轮约 67.8%／32.2%，可用 680／320 作为明确配额提案；若限定英文，这一比例是人为保留的来源轮数配比，并非声称等于英文子集的真实比例。",
        "- 若用户优先要求任务类别均衡，需要先确认语义分类定义并复核候选标签，再另定各类配额。长度保留自然分布，避免轮数×类别×长度交叉成很多小层。超过 10,000 tokens 继续保留并标记，沿用已有长度偏好。",
        "- 固定 seed 20260923；保存 revision、uuid、原始文件与字节位置、组 ID、类别、长度、筛选原因和补抽次序。",
        "- 合格池／各层内随机不放回，检查失败则按同一层的确定性随机顺序补抽；这将是经过质量筛选的样本，不能声称无条件代表原始全集。",
        "- 质量检查提案：1,000 条全做结构、精确去重、长度记录；另外随机抽检 100 条完整问答，并复核所有自动标记的可疑项。若发现问题集中在某类，应暂停并扩大该类复核。不能把这等同于 1,000 条事实答案已逐条核实。",
        "- 最终 1,000 条先独立保存 JSONL／Parquet 和 manifest，不直接混入现有 train/valid，不擅自决定 train/valid 划分。", "",
        "### 两个需区分的目标", "",
        "| 方案 | 优点 | 代价 |", "|---|---|---|",
        "| 代表原始分布：全集记录均匀随机 1,000 条 | 设计简单、抽样概率清楚 | 保留原有语言、任务和重复问题的权重；小类可能仅几条 |",
        "| 通用能力覆盖：指定语言／模式、问题去重、宽类别分层随机 1,000 条 | 更适合补 Tulu，覆盖可控 | 改变原始分布，需要先确认配额和质量规则 |", "",
        "当前等待范围与方案确认。所有诊断数据均仅用于分析；没有输出最终 1,000 条训练样本。", "",
        "## 文件", "",
        "- 原始数据：`../../../data/raw/nemotron-instruction-following-chat-v2/data/`",
        "- `full_audit_metrics.json`、`full_row_index.parquet`：全量统计和索引。",
        "- `diagnostic_reservoir.jsonl`、`diagnostic_metrics.json`、`diagnostic_token_lengths.parquet`：可复现的诊断样本与估计。",
        "- `early_exploratory_records.jsonl`、`exploratory_quality_findings.json`：探索性质量证据。",
        "- 脚本：`audit_nemotron.py`、`profile_nemotron_diagnostics.py`、`report_nemotron_review.py`。", ""]
    report = "\n".join(lines)
    highlights = (
        "## 核心结论\n\n"
        "完整文件独立按换行计数、JSON 解析计数和 Parquet 索引行数一致：1,997,510 条，比数据卡少 1,058 条。文件 SHA-256 均与官方一致；已确认此固定版本的实际条数与数据卡不一致，上游原因尚未确认。\n\n"
        "reasoning_on 有 2 条最终 content 为空，已记录为待排除候选；原始文件保持完整。两部分共享 671,081 个规范化首轮输入、323,396 个完整 prompt，但 UUID 完全不同，因此混合两部分时不能仅按 UUID 去重。\n\n"
        "关键词分类在 off / on 中分别有 40.4% / 33.6% 未识别。抽查还发现脑功能中的 function 被误归代码、VBA 调试被归建议；见 taxonomy_review_findings.json。建议用可靠的对话轮数进行分层，任务类别先报告覆盖而不强制等量。\n\n"
        f"![类别与 Qwen 长度分布]({OUT / 'dataset_overview.png'})\n\n"
        "## 全量结构与重复"
    )
    report = report.replace("## 全量结构与重复", highlights, 1)
    report = report.replace("最终答案 content 与 reasoning_content 分开计数。", "最终答案 content 与 reasoning_content 分开计数。下表 prompt 使用 enable_thinking=True；关闭 thinking 后本次所有诊断输入均增加 4 tokens。", 1)
    report = report.replace("当前等待范围与方案确认。", "具体推荐草案：英文 reasoning_off，单轮 680 条、两轮 320 条；这是待确认的配额提案，详见 sampling_plan_proposal.json。\n\n当前等待范围与方案确认。", 1)
    (OUT / "sampling_review.md").write_text(report)
    print(f"Saved {OUT / 'sampling_review.md'}")

if __name__ == "__main__":
    main()
