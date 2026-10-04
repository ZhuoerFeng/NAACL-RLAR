# 训练集筛选 workflow

入口：`python -m rlar_harness.data_selection`。实现独立于 reward 合成和 policy rollout。
示例配置：[configs/dataset_selection.yaml](configs/dataset_selection.yaml)。

## 已发布数据与后续实验入口

首批 1,000 条已发布到 [`codebase/data/datasets/rlar_train_1000_v1/`](../data/datasets/rlar_train_1000_v1/README.md)。后续实验统一读取该目录中的 `train_prompts.jsonl` 或 `train_prompts.parquet`，并在自己的运行 manifest 中记录 `dataset_manifest.json` 的数据集版本和输入文件 SHA-256。

该目录集中保存训练输入、独立参考信息、分类结果、筛选/验证报告和来源配置。历史 runs 中的 exports 作为过程快照保留，不再作为后续实验的数据入口。已发布版本只读；重新筛选或改动样本时发布新版本，并更新数据目录索引。

## 当前规则

- 保留具有实质任务内容、指令可理解、必要上下文完整的样本。
- 主观评价、开放生成、缺少标准答案或权威验证依据均不构成排除理由。
- 不做 policy 难度校准，不按答案质量筛题，不做人工复核。
- policy 输出约定为 no-thinking、最多 8,000 **生成** tokens；本 workflow 不生成 policy 回答。
- 只排除明确要求远超此输出预算的任务；输入长度不是预期输出长度。
- classifier 的输入长度限制是接口限制，超出者进入 `deferred_input_limit`，不截断，不记为质量不合格。
- 保留既有答案与标注的来源引用，classifier 和最终 policy 输入中没有最终参考答案。
- 已有人工/模型评分只对应原始回答，不自动适用于未来 rollout。

## 数据范围与配额

| 来源 | 首批目标 | 候选分层 |
|---|---:|---|
| HelpSteer2 train | 167 | 同 prompt 的多个回答归为一个任务 |
| No Robots train | 167 | 原始 category；移除最终示范回答，保留必要历史 |
| UltraFeedback | 166 | 原始 source；按任务组固定留出 20% |
| KodCode V1 train | 167 | 原始 subset；不使用 use_with_caution |
| TACO ALL/train | 167 | 原始 difficulty 仅作覆盖分层，不判断 policy 能力 |
| MathQA train | 166 | 原始 category；保留题目与选项，移除答案和公式 |

总计 1,000 个 prompt。所有值可在新运行前修改配置。默认前三个 general 来源中需要产出代码或解具体数学题的候选记为 `outside_general_scope`；知识解释仍可保留。`general_sources_only: false` 可关闭该分工。

BigCodeBench-Hard、ClassEval、DS-1000 以及各来源的官方验证/测试分区作为评测隔离库。UltraFeedback 无官方训练划分，以 `seed + group_id` 分配固定 holdout；不混入训练。所有分区先划分，再抽候选。

去重采用：完整来源上的规范化任务组精确去重；候选与 heldout、候选之间的 token 三元组近重复检索和 Jaccard 确认。历史 assistant 回答变化不拆散同一组用户指令。近重复是启发式检查，不是跨数据集语义污染已被穷尽排除的保证。

## 运行

从项目根目录执行（依赖现有 harness 环境的 `pyarrow`）：

```bash
codebase/reward_harness/.venv/bin/python -m rlar_harness.data_selection prepare \
  --config codebase/reward_harness/configs/dataset_selection.yaml
```

`prepare` 只读原始数据、计算 SHA-256、建立 SQLite 索引和候选池，不调用模型。它自动创建上海时间命名的 `codebase/reward_harness/runs/YYYY_MM_DD_HH_MM_DatasetSelection/`。同一分钟发生第二次独立运行时，任务名追加随机字符。

将下面的 `RUN` 替换为命令返回的运行目录。首次可限制 6 次请求完成接口联调：

```bash
codebase/reward_harness/.venv/bin/python -m rlar_harness.data_selection classify \
  --run RUN --env-file codebase/reward_harness/.env --max-new-calls 6
```

继续同一运行，补齐目标配额：

```bash
codebase/reward_harness/.venv/bin/python -m rlar_harness.data_selection classify \
  --run RUN --env-file codebase/reward_harness/.env
```

仅改变 classifier 模型或路由时，可复用冻结候选池，生成一个新运行而保留旧运行的失败证据：

```bash
codebase/reward_harness/.venv/bin/python -m rlar_harness.data_selection fork-classifier \
  --run RUN --config codebase/reward_harness/configs/dataset_selection.yaml
```

此命令要求采样配置保持一致；候选不变，旧标签与调用不会复制为新 classifier 的结果。后续 `classify` 使用返回的新运行目录。

仅重新导出或检查 trace，不发请求：

```bash
codebase/reward_harness/.venv/bin/python -m rlar_harness.data_selection status --run RUN
codebase/reward_harness/.venv/bin/python -m rlar_harness.data_selection export --run RUN
codebase/reward_harness/.venv/bin/python -m rlar_harness.data_selection replay --run RUN
```

`status` 可在分类运行期间读取实时进度，不占用运行的写锁。

## 分类、缓存与失败处理

分类 prompt 和 JSON schema 在 `prepare` 时冻结。请求只包含筛选规则与待评估的完整对话，没有参考答案、人工评分、原始数据集名称或测试答案。数据中的指令作为不可信内容，不能修改分类规则。

模型使用用户提供的精确标识 **us.openai.gpt-6.1-sol**，通过 Responses 接口和 `provider=aws_third` 调用，不进行模型或 provider fallback。配置采用 `reasoning.effort=low`、`max_output_tokens=4096`、`stream=false`、`store=false`；这些是 classifier 参数，与 policy 的 no-thinking/8,000 tokens 输出约定分开。复用现有 HTTP adapter，在 prompt 中要求 JSON 并用 Pydantic 严格验证。参考接口：[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。JSON mode 可显式配置，但默认遵循用户给定的 Responses 调用方式，不额外假定网关支持 `json_schema`。

分类结果为 `include / exclude / uncertain`。排除必须有明确原因。`uncertain` 单独保存，不自动改成 include 或 exclude。任务类型标签用于来源分工和统计，评价方向不直接作为后续 reward rubric。

先以每来源目标量的 3 倍抽候选；每个来源内部按原始类别轮转。分类时按来源轮转，只补尚未满足的配额；候选用完则自动补抽。默认并发 6、整个运行最多 4,000 次请求。首次真实请求单独发送，成功后再扩大并发。当前网关实测项目限额为 60 RPM，默认按 50 RPM 的滑动窗口节流；可通过 `classify --requests-per-minute 40` 降速。这个传输参数记录在 `transport_limits.json` 和 `invocations.jsonl`，不改变冻结的采样规则或模型请求内容。限流时至少退避一个 60 秒窗口。

完整请求和响应在 `requests/<cache_key>/`。缓存标识包含请求正文、模型、接口、provider 与 schema。已完成判断可恢复复用；源文件、配置或 prompt 改变时拒绝直接续跑。原始输入文件只读。

错误、超时、输出截断和 JSON/schema 错误不是负标签。对已收到的格式错误/字段矛盾，最多做 2 次纠正：在完整对话中附上先前输出和明确的校验反馈，要求重新分类；每次都计入请求预算并独立保存。纠正后成功也保留此前 invalid_output 记录。对明确未送达的连接错误、明确拒绝处理的限流或已收到的 HTTP 5xx 失败响应，退避后最多重试 2 次，同样保留每次请求记录；5xx 失败尝试的 token 用量若未返回则记为 unknown，不记为零。不重试结果未知的已发出请求。其余接口失败、未知结果或纠正/重试耗尽后停止继续派发，保留已完成结果；已发出的并发请求会完成并落盘。进程中断时，如果完整响应已落盘，可恢复其判断；否则记录 unknown。未解决的接口失败/unknown 需要先核对记录。服务配置或模型改变时应创建新运行。

`.env` 只在调用时读取字面量，不执行 shell。凭据不写配置、trace 或版本库；请求不保存 Authorization header，响应中意外回显的当前凭据会被替换。回放只检验已存请求/响应的完整性，不把接口失败变成生产测试通过。

## 产物

筛选过程产物先放在本次 runs 目录；确认用于后续实验的数据及随附报告发布到上述数据目录，完整请求 trace 仍留在 runs：

- `source_fingerprints.json`：源文件、split、大小和 SHA-256。
- `selection.sqlite`：来源记录、任务组、评测隔离库、候选状态和请求缓存。
- `candidates.jsonl`、`pool_report.json`：候选池及去重/延后统计。
- `structural_exclusions.jsonl`：结构异常及原始行号。
- `classifier_prompt.txt`、`classification_schema.json`、`config.json`：冻结规则。
- `transport_limits.json`、`invocations.jsonl`：请求节流和每次执行的代码指纹。
- `requests/`：完整分类请求与响应，含格式纠正的多轮历史和父请求引用；无工具调用。
- `selection_report.json`：完成度、来源和任务类型分布、缺口及失败原因。
- `replay_report.json`：离线回放结果，与真实模型可用性分开报告。
- `exports/train_prompts.jsonl`、`train_prompts.parquet`：只含 sample_id、data_source、prompt。
- `exports/evaluation_assets.jsonl`：指向未修改原始文件的任务组成员引用，可取回原始回答、公式、tests 和标注；没有复制全量答案。使用前须重建该原始行的 prompt，与 selected_prompt_sha256 比对，避免把同任务组内不同历史上下文的答案错配给当前输入。
- `exports/classification_labels.jsonl`：选中样本的分类理由，独立于 policy 输入。
- `exports/uncertain.jsonl`：不确定任务。
- `exports/rollout_contract.json`：no-thinking 和 8,000 生成 tokens 的下游约定；不是已执行 rollout 的证明。

官方验证/测试数据未导出为训练数据。报告状态只有满足全部目标配额时才为 `complete`；否则明确给出 shortfalls。

## 验证范围

`tests/test_data_selection.py` 用微型数据与 mock HTTP 验证输入/答案隔离、任务组去重、评测隔离、确定性抽样、续跑、失败停止和回放。它们属于离线工程测试，不能作为真实模型接口或分类质量的生产验收。真实接口证据只能来自单独运行中的实际请求和响应。本 workflow 尚未执行人工质量审计，也不声称验证了训练收益。
