# Tulu 替代数据提案

核查日期：2026-09-23。目标是补回通用指令遵循/问答能力，保持当前翻译、摘要、数学、写作和多轮数据的角色。当前仅调查数据卡、版本和少量样本，**未下载候选全量数据、未混入 train/valid，也未把少量预览当作全量质量认证**。

**首选：NVIDIA Nemotron-SFT-Instruction-Following-Chat-v2，使用 reasoning_off 部分。** 它同时覆盖开放聊天和明确指令，来源较新，有直接可用的用户—回答消息结构，最接近当前被删除 Tulu 子集的通用生成用途。不能只因为是官方发布就免除语义配对检查；本次 Tulu 的问题已说明这一点。

| 候选 | 时间证据 | 数据内容与适用范围 | 需要注意的差异 |
|---|---|---|---|
| [nvidia/Nemotron-SFT-Instruction-Following-Chat-v2](https://huggingface.co/datasets/nvidia/Nemotron-SFT-Instruction-Following-Chat-v2) | HF 仓库创建于 2026-03-08；数据卡记录生成日期 2025-11-20 | 数据卡报告整体约 199.9 万条；有 reasoning_off / reasoning_on，覆盖英文通用聊天和指令遵循 | 推荐 reasoning_off；199.9 万是数据卡整体数量，不是已核实的 reasoning_off 数量。生成模型包含 Kimi、GLM、Qwen3、GPT-OSS 等，答案是合成数据 |
| [allenai/Dolci-Instruct-SFT-No-Tools](https://huggingface.co/datasets/allenai/Dolci-Instruct-SFT-No-Tools) | HF 仓库创建于 2025-11-18 | 1,924,533 条；`messages` 格式；移除了工具调用，可补一般指令任务 | 是混合数据，包含大量旧来源、数学和代码，不宜整包随机抽样。应挑通用/Precise IF 来源，并与现有 WildChat 去重 |
| [HuggingFaceTB/smoltalk2](https://huggingface.co/datasets/HuggingFaceTB/smoltalk2) 的部分 SFT 子集 | HF 仓库创建于 2025-07-10 | 可选 `smoltalk_smollm3_smol_magpie_ultra_no_think`（406,843 条）和 `smoltalk_smollm3_explore_instruct_rewriting_no_think`（30,391 条） | 这是 2025 年的混合版本，部分底层 prompt 较早。避免 Preference 部分，后者明确复用了 Tulu 3 preference mixture |

上述时间把“仓库创建时间”和“数据生成时间”分开报告；lastModified 并不能证明全部样本是近期产生的。

**对首选的具体观察**

已读取固定 revision `1a9454ed054b8544503ab8d8c0a519d141a44c5b` 的 `data/reasoning_off.jsonl` 文件开头最多 256 KiB，保存前 3 条完整记录。Hugging Face 的数据预览接口当时返回 500，因此使用原文件的有界读取；没有下载 15GB 级的完整数据。

- 字段包含 `messages`、`uuid`、`license`、`used_in`、`reasoning`。预览中有空 system 消息；以后适配时应明确处理这种模板占位，它与空 user 请求不同。
- 前两条分别为商务文字润色、编译器优化参数问答，问题与回答主题吻合。
- 第三条含长串代码题与问答上下文，说明这一集合也需要任务类型和输入形式筛选，不能把 reasoning_off 等同于纯通用短问答。
- 推荐从英文、纯文本、通用问答/改写/解释/指令遵循部分抽样，避免把替代来源变成新的数学或代码主数据集；保留必要的 system 指令和历史，禁止把最后一个 assistant 参考答案放入待生成 prompt。

**Dolci 的具体限制**

[主数据卡](https://huggingface.co/datasets/allenai/Dolci-Instruct-SFT) 写明包含新 Dolci Precise IF（136,833 prompts）、更新回答的 WildChat，以及大量 Tulu persona、FLAN、Aya 等既有来源。这里的 Tulu persona 不等于此次出问题的 `tulu-3-sft-reused-on-policy-8b`，但也不能据此直接认定所有来源都已解决质量问题。

No-Tools 预览中，`source` 是较粗的汇总标签，不能保证仅靠它就能筛出具体底层来源；主集合有更细的 `source_dataset` 和 `domain`。若选 Dolci，需要按这些字段或原始 ID 明确筛选范围，而非直接从 No-Tools 全包随机抽样。

**SmolTalk2 的具体限制**

只推荐明确指定的 SFT 子集，不推荐完整 Preference 集合。官方卡明确列出 Preference 来源为 `llama-3.1-tulu-3-8b-preference-mixture` 及其 Qwen3 再生成版本。部分 SFT 子集还有工具定义、system 指令存于 `chat_template_kwargs`；将它们转为当前格式时不能丢弃影响任务含义的元数据。建议优先无工具、普通文本子集。

**补回方案建议，尚未执行**

第一阶段可从首选数据中准备 982 条 train、28 条 valid，等量填补被删除的 Tulu，避免同时改变任务混合规模。如此总量会变为 train 8,573、valid 265；仍少于最初 train 的 8,604，因为另有 31 条无效 PubMed/WildChat 已删除。这只是最小改动方案，28 条 valid 不足以支持细小提升的稳定结论，正式评估的验证集大小应另行确定。

新增样本应保存上游 revision、uuid/原始 ID、选择规则和固定抽样 seed；先按原始会话或同一问题的变体分组划分，再做全量 prompt/answer 结构校验及有记录的语义抽查，并与现有数据进行去重。token 长度先记录，按用户已确认的当前策略不自动删除超长样本。新增记录的 `function_call` 需要重新决定或生成，不能沿用被删除 Tulu 样本的旧标记。

**其它候选为什么暂不作为首选**

- [Nemotron-RL-instruction_following](https://huggingface.co/datasets/nvidia/Nemotron-RL-instruction_following)：2025 年，46,391 条可验证指令，适合专门研究约束遵循；但它基于 WildChat-1M 添加规则，和本项目现有 WildChat 可能同源，且不是通用问答参考答案的直接替换。
- [WildChat-4.8M](https://huggingface.co/datasets/allenai/WildChat-4.8M)：2025-08 发布更新，非 toxic 版本实际 3,199,860 条对话；适合后续更新多轮域，但会与现有 WildChat 高度同源。数据卡还明确保留部分空 user 输入，仍需过滤，不能作为本次 Tulu 替换的最省事选择。

**证据与可追溯性**

`candidate_sources/` 保存了官方 API 元数据、固定 revision 数据卡和有限预览。用于建议的主要 revision：

```text
nvidia/Nemotron-SFT-Instruction-Following-Chat-v2  1a9454ed054b8544503ab8d8c0a519d141a44c5b
allenai/Dolci-Instruct-SFT-No-Tools               9156a5a542b2503100d4f1fabbf50be5eb25d977
HuggingFaceTB/smoltalk2                          fc6cc2103c066455aade5d7fbb346039ae36ca5e
```
