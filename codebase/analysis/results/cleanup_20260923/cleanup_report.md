# 数据清理与 Qwen token 长度报告

执行日期：2026-09-23。已直接更新 `codebase/data/train.parquet` 和 `codebase/data/valid.parquet`。

**清理结果**

| 删除原因 | Train | Valid |
|---|---:|---:|
| PubMed 无文章正文 | 18 | 0 |
| PubMed 只有 supporting-information 声明 | 2 | 0 |
| WildChat 最后一条用户消息为空 | 11 | 0 |
| 全部 Tulu 子集 | 982 | 28 |
| **总删除** | **1,013** | **28** |
| **清理前** | **8,604** | **265** |
| **清理后** | **7,591** | **237** |

清理仅移除整行。已重新读取写出的 Parquet，验证 schema、metadata、保留行顺序与全部字段值和原始文件逐条一致。重新应用相同规则，待删除数为 0。

完整原始文件保存在 `codebase/data/backups/pre_cleanup_20260923/`；原文件 SHA-256 与首次审计一致。当前 schema 仍为原来的 5 个顶层字段，新增统计没有写入训练样本。

**按用户要求保留的内容**

- 双向翻译样本全部保留，包括同一句对以反方向出现在两个文件中的情况；本次按预期任务设计处理。
- GovReport train/valid 的重叠暂不处理。
- 空 `function_call` 不作为删除条件；清理后剩 5 条，减少的 1 条属于整批删除的 Tulu。
- 最后一条 user 非空、但历史中存在空消息的 WildChat 样本保留，共 15 条。
- 用户已确认保留全部超长样本；没有截断或根据 token 数删除记录。
- 原下载的官方 Tulu 文件作为溯源资料保留在 raw 目录；当前 train/valid 中 Tulu 已为 0。

**清理后的来源数量与 prompt token 分布**

| 来源 | Train | Valid | Train prompt P50 | Train prompt P95 | Train >10k | Valid >10k |
|---|---:|---:|---:|---:|---:|---:|
| GSM8K | 1,000 | 30 | 96.0 | 140.0 | 0 | 0 |
| WildChat | 946 | 36 | 936.5 | 5,788.0 | 19 | 0 |
| PubMed | 770 | 22 | 3,556.5 | 10,053.2 | 40 | 4 |
| arXiv | 760 | 21 | 7,406.0 | 21,434.8 | 255 | 5 |
| GovReport | 746 | 20 | 8,449.5 | 22,137.0 | 291 | 8 |
| Essay writing | 933 | 28 | 156.0 | 173.0 | 0 | 0 |
| Essay infilling | 929 | 28 | 1,436.0 | 4,492.2 | 2 | 0 |
| EN→FR | 731 | 19 | 79.0 | 104.0 | 0 | 0 |
| FR→EN | 776 | 33 | 89.0 | 123.2 | 0 | 0 |

**Tokenizer 统计口径**

直接使用本地 `codebase/model_tokeniser/`，其模型配置为 `qwen3`、36 层、hidden size 4096，与 Qwen3-8B 相符。Transformers 加载的实现类名为 `Qwen2TokenizerFast`，这是实现类名，不表示本次改用了 Qwen2 模型。未下载或加载模型权重。

Prompt 包含完整历史对话和模型原始 chat template，使用 `add_generation_prompt=True`，分别统计 `enable_thinking=True` 和 `False`，不添加额外 system prompt 或工具描述，不做 padding 或 truncation。参考答案单独使用 `add_special_tokens=False` 编码，不能把它当作实际 rollout 的生成长度。

主表使用 thinking 开启模式。关闭 thinking 时模板会额外加入空 `<think>…</think>`，所有样本恰好多 4 tokens；这仅是输入模板差异，不预测模型的思维链长度。

| 指标 | Train prompt | Valid prompt | Train reference | Valid reference |
|---|---:|---:|---:|---:|
| 均值 | 2,894.0 | 2,519.7 | 453.0 | 481.9 |
| P50 | 548.0 | 282.0 | 175.0 | 155.0 |
| P95 | 12,866.5 | 10,730.6 | 1,799.5 | 1,658.8 |
| P99 | 22,139.6 | 21,037.4 | 4,374.3 | 5,428.8 |
| 最大值 | 187,328.0 | 23,198.0 | 13,094.0 | 9,587.0 |

超过 10,000 prompt tokens 的记录：train **607 / 7,591（8.00%）**，valid **17 / 237（7.17%）**。关闭 thinking 时，这一阈值的超长数量不变。用户确认这些记录全部保留，名单单独存放在 `overlong_prompts.json`。

超过 10k 的 train 主要来自 GovReport 291 条、arXiv 255 条、PubMed 40 条、WildChat 19 条、essay infilling 2 条。若未来按 10k 执行过滤，来源混合比例会改变；当前未执行。

本地 tokenizer 的 `model_max_length=131072`，模型 config 的 `max_position_embeddings=40960` 且 `rope_scaling=null`；二者不同，不能把 tokenizer 的长度声明直接当作当前模型已配置的上下文窗口。train 有 16 条 prompt 超过 40,960 tokens，其中 2 条超过 131,072 tokens。这些仍按用户确认保留，后续训练配置需明确如何处理。

最长 prompt 为当前 train[5419]（原 train[6261]），共 187,328 tokens；另一条 train[6188]（原 train[7141]）为 151,480 tokens。真实 token 最长样本与字符数最长样本不同。

参考答案超过 5,000 tokens：train 46 条（essay writing 39、arXiv 7），valid 4 条（均为 essay writing）。这项仅用于后续输出长度预算的参考，不作为删除条件。

**复查结果与记录**

指定的四类待删记录均为 0；完整 prompt 的集合内/集合间精确重复数均为 0。按用户要求保留的反向句对、GovReport 重叠和其它未指定清理项仍记录在 profile 下。

- `cleanup_manifest.json`：清理规则、原/新指纹、删除数、备份位置，以及用户确认的长度策略。
- `removed_rows.json`：每条删除记录的原始行号与原因。
- `retained_row_map.json`：当前行号到清理前行号的映射。
- `qwen_token_lengths.json`：所有保留记录的两种 prompt token 长度及 reference 长度。
- `overlong_prompts.json`：607 / 17 条超长记录，含当前/原始行号。
- `qwen_token_metrics.json`：分来源统计、模板参数、tokenizer 文件指纹和依赖版本。
- `qwen_token_overview.png` / `.pdf`：token 长度与超长比例图。
- `profile/`：清理后的全量结构、重复与字符统计。
- [Tulu 替代数据提案](replacement_proposal.md)：候选数据集、来源和推荐范围。

复跑统计（不会过滤或修改数据）：

```bash
python codebase/analysis/profile_qwen_tokens.py
python codebase/analysis/analyze_train_valid.py --output-dir codebase/analysis/results/cleanup_20260923/profile
```

清理脚本 `clean_train_valid.py` 默认为 dry run；`--apply` 才写数据，且拒绝覆盖已有备份与清理日志。对当前数据 dry run 返回删除数 0。Python 依赖见 `requirements-tokenizer.txt`。
