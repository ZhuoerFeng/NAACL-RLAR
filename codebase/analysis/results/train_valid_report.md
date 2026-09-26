# 本地 train / valid 数据整体分析

> **历史快照：本报告基于清理前的数据。** 2026-09-23 已按用户指定规则完成清理；当前 train/valid 为 7,591 / 237 条，见 [清理与 Qwen token 报告](cleanup_20260923/cleanup_report.md)。翻译双向样本按任务预期保留，GovReport 重叠暂不处理，超长样本经用户确认保留。下文原始行号对应 `codebase/data/backups/pre_cleanup_20260923/` 中的备份。

分析日期：2026-09-23。分析对象为 `codebase/data/train.parquet` 与 `valid.parquet`；全量统计 8,869 条。所有行号从 0 开始。原始数据未修改。

**主要结论：这是一份 10 个来源、7 种 ability 的混合语料。总体来源比例接近，但存在 Tulu 参考答案语义不匹配、翻译反向句对跨集合重叠、GovReport 大段文本重叠，以及无正文的 PubMed 样本。需要先解决这些问题，再把 valid 当作可信的比较依据。**

**1. 规模与构成**

| 数据来源 | Train | Train 占比 | Valid | Valid 占比 |
|---|---:|---:|---:|---:|
| GSM8K | 1,000 | 11.62% | 30 | 11.32% |
| Tulu 3 preference 子集 | 982 | 11.41% | 28 | 10.57% |
| WildChat 多轮对话 | 957 | 11.12% | 36 | 13.58% |
| Essay：摘要→长文 | 933 | 10.84% | 28 | 10.57% |
| Essay：段落补全 | 929 | 10.80% | 28 | 10.57% |
| PubMed 摘要 | 790 | 9.18% | 22 | 8.30% |
| arXiv 摘要 | 760 | 8.83% | 21 | 7.92% |
| GovReport 摘要 | 746 | 8.67% | 20 | 7.55% |
| 英→法翻译 | 731 | 8.50% | 19 | 7.17% |
| 法→英翻译 | 776 | 9.02% | 33 | 12.45% |
| **合计** | **8,604** | **100%** | **265** | **100%** |

- Train 106.56 MiB，valid 3.03 MiB；valid 占总样本 2.99%。
- `ability` 为 summarization、translation、math、rlhf、multi-turn、writing、infilling；writing 与 infilling 可以归为条件生成，故语义上也可按 6 大类表述。
- 三个摘要来源合计 2,296 / 63 条；翻译两个方向合计 1,507 / 52 条。来源间数量相近，不意味着任务大类均匀。
- 本地没有标记为 Hendrycks-MATH、LeetCode、FLORES、UltraChat、AIME、MBPP 或 WMT-24 的来源。这些文件与本轮审计时正文列示的四领域主实验数据不同，更接近旧版附录的语料描述。不能用这些数量直接替换主实验数据表。

**2. 字段与格式**

两份文件 schema 相同，共 5 个顶层字段：

| 字段 | 含义 / 实际结构 |
|---|---|
| `prompt` | `list<{role, content}>`；完整模型输入，包括多轮历史 |
| `data_source` | 10 个来源标签 |
| `extra_info` | `answer`、`function_call`、`question`、`split` |
| `reward_model` | `ground_truth`、`style` |
| `ability` | 7 个任务标签 |

顶层字段无 null；全部 8,869 条的 `extra_info.answer` 与 `reward_model.ground_truth` 完全相同，`question` 与最后一条 prompt 内容一致。角色均以 user 开始、以 user 结束，并交替排列。

只有 WildChat 是多轮输入，train 957 条、valid 36 条；其余都是单条 user 消息。train 最长 69 条消息、valid 最长 49 条。只读取 `extra_info.question` 会丢失 WildChat 的历史上下文。

`extra_info.split` 在 train 全为 `train`；在 valid 中有 235 条 `train` 和 30 条 GSM8K `test`。它可能记录上游划分，不能作为当前文件 train/valid 归属依据，也不能单凭该值判定泄漏。文件中没有完整的数据集 revision、原始 ID、抽样 seed 或过滤日志。

**3. 数据质量**

| 发现 | Train | Valid | 解释 |
|---|---:|---:|---|
| PubMed 文章正文为空 | 18 | 0 | prompt 模板非空，但模型没有需要摘要的文章 |
| PubMed 正文只有期刊 supporting-information 声明 | 2 | 0 | 与所配的不同论文摘要无法形成正常摘要任务 |
| 最后一条用户消息为空 | 11 | 0 | 全部来自 WildChat |
| 任意一条历史/当前消息为空 | 26 | 0 | 这是受影响的样本数，包含上面的 11 条 |
| `function_call` 为空字符串 | 6 | 0 | 是否影响执行取决于调用代码与 fallback |

PubMed 两组规范化相同 prompt 分别含 18 条和 2 条，且各自的参考摘要不同；这不是简单保留一条就能修复的冗余，应该恢复正文或剔除无效输入。18 条空正文的行号及其它异常详见 `evidence.json` 的 `quality_flags`。

**Tulu 的语义问题与上游核对**

用 `random.Random(20260923)` 在 train 和 valid 的 Tulu 子集中各抽 10 条，人工复核发现 19 条明显答非所问，另 1 条明显答案错误：例如问二次函数最大值，参考答案却是 “Negative”，正确值为 5。该抽查不等于全部 1,010 条的语义错误率。完整抽样内容保存在 `evidence.json`。

按用户要求重新下载了官方 `allenai/tulu-3-sft-reused-on-policy-8b`：19,444 条，59,809,485 bytes，revision `41bb2c3ea0cd39fb823b0798ec6cedd56446f2a6`。文件 SHA-256 与 Hugging Face LFS 元数据一致。

**全量对照结果：982 条 train 与 28 条 valid 的问题都能唯一匹配到官方记录，全部参考答案也匹配该记录的 chosen 最后一条 assistant 消息。匹配时仅做 Unicode NFC 与空白规范化。这说明抽查的语义问题在官方发布文件中即可复现，不能据此归因于本地错位拼接；重新下载相同版本不会自动修复。**

| 本地位置 | 官方记录 ID 尾号 | 问题 | chosen 实际内容 |
|---|---:|---|---|
| train[13] | 19305 | 联合国安理会的职责 | 用越南语介绍羽毛球用羽毛 |
| train[2628] | 3520 | 如何拆解复杂想法 | 计算 Laura 购买衣物后的找零 |
| valid[15] | 1092 | 统计社会运动名称的词频 | 用 pandas 拆分 fips / row 列 |
| valid[62] | 372 | 计算矩形闸门水压力 | Python 判断元素是否在列表中 |

这些示例的 rejected 也围绕相同的错误主题，不能简单把 chosen 换成 rejected 当作修复。官方全量数据的结构是一问一答的 chosen/rejected 对，结构对齐不代表语义正确。逐条上游 ID 映射保存在 `tulu_upstream_comparison.json`。

**4. 重复与跨划分重叠**

完整 prompt 在 train/valid 之间没有完全重复，做 NFC 与空白规范化后仍为 0；valid 内部也没有完整 prompt 重复。train 内部精确重复有 17 条冗余，规范化后有 18 条冗余，均来自上述 PubMed 异常。

**但按原始内容检查，划分并不完全独立：**

| Train 行号 | Valid 行号 | 重叠类型 |
|---:|---:|---|
| 1076 | 79 | 同一英法句对，方向相反 |
| 1117 | 209 | 同一英法句对，方向相反 |
| 4275 | 40 | 同一英法句对，方向相反 |
| 6672 | 66 | 同一英法句对，方向相反 |
| 5181 | 4 | GovReport 文档与参考摘要大段重叠 |

4 条反向句对占全部翻译 valid 的 7.69%（占完整 valid 的 1.51%）。划分翻译数据时，应先按规范化后的英法句对分组，再把双向样本放入同一 split。train 内部另有 80 组双向句对；训练中双向复用本身不一定是问题。

GovReport 的 train[5181] 与 valid[4] 都涉及 Congress contempt power。去除提示模板并规范化空白后，验证正文 5,508 个不同的连续五词片段中，4,582 个也出现在训练正文，覆盖率 **83.19%**；其 TF-IDF cosine 为 0.646。它们不是逐字符相同的文件，但存在明显内容重叠，需要按原始报告或报告版本分组核查。这个覆盖率不是逐字或逐字符的重复比例。

筛查使用每个来源内的词级 unigram/bigram TF-IDF，验证集每条检索最相似训练输入；未发现 cosine ≥ 0.8 的候选，但 GovReport 实例说明这个阈值不能排除包含或改版文档。已检查 cosine ≥ 0.3 候选的连续五词片段覆盖。该方法不保证发现全部语义改写或跨语言重复。

WildChat 出现一组相同首条消息 “Hi” 和通用寒暄，另有一组末条消息同为 “another one”；对应话题与上下文不同，因此没有把这些计为同一会话泄漏。两种 essay 任务按可提取标题检查未发现重复，这不等于完成了上游文档 ID 审计。

**5. 输入与参考答案长度**

以下均为 Unicode 字符数；prompt 累加全部消息内容，不含 chat template，也不是 tokenizer token 数。

| 指标 | Train prompt | Valid prompt | Train reference | Valid reference |
|---|---:|---:|---:|---:|
| 均值 | 11,352.0 | 10,140.8 | 2,098.7 | 2,220.1 |
| 中位数 | 995.5 | 917.0 | 844.0 | 776.0 |
| P95 | 54,461.1 | 47,692.4 | 8,060.5 | 7,921.2 |
| P99 | 97,169.3 | 94,280.8 | 20,530.9 | 26,688.2 |
| 最大值 | 556,071.0 | 123,118.0 | 68,374.0 | 50,361.0 |

| 来源 | Train prompt 中位数 | Train reference 中位数 | Train 输入字符占比 |
|---|---:|---:|---:|
| GSM8K | 347.0 | 252.0 | 0.37% |
| Tulu 3 preference 子集 | 369.5 | 1,377.0 | 0.75% |
| WildChat 多轮对话 | 4,016.0 | 1,569.0 | 7.79% |
| Essay：摘要→长文 | 853.0 | 7,064.0 | 0.82% |
| Essay：段落补全 | 7,094.0 | 568.0 | 8.83% |
| PubMed 摘要 | 15,358.5 | 1,271.0 | 14.38% |
| arXiv 摘要 | 28,032.5 | 998.0 | 27.42% |
| GovReport 摘要 | 43,233.5 | 3,338.5 | 39.11% |
| 英→法翻译 | 327.0 | 122.0 | 0.25% |
| 法→英翻译 | 343.0 | 107.0 | 0.28% |

摘要任务仅占训练样本 **26.69%**，却贡献 **80.91%** 的输入字符；翻译占 **17.52%** 样本，仅占约 **0.54%** 的输入字符。模型实际 token 数、attention 成本与字符数并不等价，但按样本数均衡显然不能代表输入规模均衡。

Train 有 958 条 prompt 超过 32,000 字符、301 条超过 64,000 字符、81 条超过 100,000 字符。最长 train[7141] 为 arXiv 摘要，共 556,071 字符。论文配置列示 10,000 prompt tokens 与超长过滤；当前没有实际 tokenizer/chat template，因此不能直接给出真实过滤率，也不能假定文件中每一条都参与了训练。下一步需在实际 tokenizer 下统计各来源保留量。

长文生成则呈相反形态：输入较短，参考长文很长，训练参考最大值 68,374 字符。参考答案长度不是模型 rollout 的实测输出长度。

**6. Train/valid 分布及评估可用性**

来源比例的 total variation distance 为 0.0590，Jensen–Shannon divergence 为 0.00405 bits，来源列联表检验 p≈0.710；按来源比较 prompt 字符长度的 KS 检验也未发现明显差异。这表示目前没有检出大的分布偏移，不证明分布完全相同，尤其 valid 各来源样本很少。

每来源 valid 只有 19–36 条。若用二元正确率，一条样本会改变约 2.8–5.3 个百分点；GSM8K 的 30 条对应每题 3.33 个百分点。它可以用于初步开发观察，但无法单独支撑小幅性能提升的稳定结论。质量异常与上面的内容重叠应先处理。

**7. Reward 元数据**

- Train 的 style 为 `bleu` 5,665 条、`model` 2,939 条；摘要、写作与补全也标成 `bleu`。这只是文件字段，不能推出实际用了 BLEU 打分。
- Train `function_call` 最常见的是 `compute_skywork_llama_score`：4,520 / 8,604（52.53%），其次为 `compute_seed_score` 1,585 条、`compute_reward_reward_score` 1,485 条。非空函数标签共 23 种。
- GSM8K 的 1,000 / 30 条参考均保留完整解答与 `#### 数值` 末尾格式；train 中 967 条 function_call 是 `compute_skywork_llama_score`，valid 为 29 条。这与“全部数学样本直接 exact match”的解释不同，但是否真的如此运行仍需检查代码。
- Tulu 标记中出现 `explicit_irrelevance` 等函数。没有函数定义、调用日志和生成过程，不能断言这些标记是人工路由、运行时真实路由，或受异常参考答案影响。

**8. 建议的下一步顺序**

1. 确认这份旧语料在实验中的具体用途，以及它对应的 run/config。
2. 优先处理 Tulu 的语义不匹配：同版本重下载无效；应回溯原始 SFT 对话、选择经过核查的替代来源，或对配对进行重新审核。暂不把 chosen 当作无条件可靠的标准答案。
3. 按英法句对与报告文档分组重建独立划分；修复或移除 PubMed 缺正文、WildChat 空消息等异常。
4. 用实际 tokenizer 和 chat template 测量过滤后样本数，再确定任务采样与 valid 扩充策略。

本次没有覆盖原有 train/valid，没有重建划分，没有执行训练，也没有把这些数据写入论文主实验统计。

**9. 复现与产物**

- `codebase/analysis/analyze_train_valid.py`：全量统计、结构检查、重复/相似度检查与图表。
- `codebase/analysis/compare_tulu_upstream.py`：本地 Tulu 与官方记录的全量对照。
- `codebase/analysis/results/metrics.json`：完整统计与原文件 SHA-256。
- `codebase/analysis/results/evidence.json`：异常行号、重复组、候选相似度和固定抽查样本。
- `codebase/analysis/results/tulu_upstream_comparison.json`：1,010 条记录的上游映射。
- `codebase/analysis/results/data_overview.png` / `.pdf`：分布与长度图。
- `codebase/data/raw/tulu3-sft-reused-on-policy-8b/`：官方 parquet、README、metadata 与下载校验清单。

环境依赖见 `codebase/analysis/requirements.txt`，在项目根目录执行：

```bash
python codebase/analysis/analyze_train_valid.py
python codebase/analysis/compare_tulu_upstream.py
```

本轮依赖安装在临时环境 `/tmp/rlar-data-audit-venv`；上面的脚本可在另一个安装相同依赖的 Python 环境复跑。统计与匹配全在本地完成，未将数据提交给外部模型进行打分。

**原始文件指纹**

```text
train  671a134cebe65cc2727d69fd4bd2e1a0180cd1fd928abe8ffcb1656bbe13a52f
valid  0a6bc8bf1ee565f740a46bfb0e3742612f108e38392c69849e220f2629119a36
tulu3  624dddb758e4680ca0cdb5796e60edb020c05ad42efb91edd6075ce432743250
```
