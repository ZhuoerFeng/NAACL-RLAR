# 追加需求 A01：完整 LLM 调用 trace 与蒸馏数据导出

> 更新入口（2026-09-28）：以下为 v1 追加需求。[v2 更新需求](PRD_REWARD_HARNESS_UPDATE.md) 继续复用这套 trace 底座，并按 actor 区分两个 synthesizer、harness-verifier、rubric judge。默认训练目标仅含两个 synthesizer，不能将所有留档模型回复自动混入蒸馏数据。

日期：2026-09-23。交付对象：正在构建 RLAR harness 的 Claude Code。

**请继续当前构建，在现有实现上增量完成本需求，不重新初始化项目或从头执行启动说明。** 原 PRD 的构造、评分、预算、恢复和提交语义继续有效；本追加单只补充 LLM 输入/输出留档及训练数据导出。已实现的等价能力直接复用，不另建一套并行 trace 系统。

本追加单可单独交付开发，无须重读整份 PRD。更多字段解释见 [HARNESS_TRACE_SPEC.md](HARNESS_TRACE_SPEC.md)。根目录 PRD v1.1 中的 INV-13、AT-33–AT-36 是本需求的同一版本，不是另一组重复任务。

## 1. 目的与交付范围

用户需要保留 **harness 每次实际发送给 controller LLM 的完整 message list，以及该次返回**，用于后续 Qwen3-8B 的工具调用、reward 构造与失败修复蒸馏实验。

本次新增三个交付：

1. 在现有 `llm_call`/provider adapter 中采集完整请求和响应。
2. 增加 `export-llm-calls`，可直接查看每次完整输入和输出。
3. 扩展已有 `export-sft`，增加默认 `per_call` 数据视图及监督范围标记。

不要求本次训练 Qwen3-8B、下载 tokenizer/模型、部署新服务或改变 agent 的决策流程。不额外调用 LLM 生成摘要、解释或训练标签。现有 full-trace/final-program-only 导出若已实现，继续保留。

## 2. A01-R1：在真正发请求的位置记录输入

采集点必须是 **adapter 最终组装后、transport 派发前**，不能仅记录 controller 层的早期 prompt。

保存以下信息：

- 最终完整 request body，尤其是原样 `messages`：角色、内容、顺序、工具调用 ID、参数原文、工具返回。
- messages 外的模型可见字段，如顶层 system/instructions、tools、response schema；不存在时明确为空或不适用。
- 实际使用的模型/revision、adapter 版本、生成参数、history cursor，以及请求/消息 hash。
- `run_id, episode_id, task_id, split, logical_call_id, physical_attempt_id`，沿用现有 ID 规则。

保存的是模型实际收到的工具 observation 版本；若此前已限长，不在导出时替换为完整原始工具输出。原始全量工具结果可继续单独存储。

冻结请求，持久化完整 body 与派发意图，然后发送相同内容。写入失败按现有存储异常规则处理，不能先调用模型再补造记录。不得只保存 hash、blob 路径而不保存其内容，也不得用摘要代替原文。鉴权 header/API key 不写入日志。

若 provider 使用 continuation ID，保存实际 wire request，并同时保留本地可确认的完整逻辑上下文；两者区分存储。SDK 内部重试应关闭或纳入物理 attempt 记录，不能漏记。

## 3. A01-R2：关联响应、重试与历史提交

响应到达后，先保存可观察原文、usage/finish reason 和完整性，再解析/提交历史。必须分别记录：

```text
response_complete
committed_to_history
schema_valid
actions_dispatched
```

它们不能混成一个 success：完整但非法 JSON 可以用于下一轮修复，不能执行；完整的迟到返回可以留档，但不能再次进入历史。

每次物理请求均有记录，包括重试、失败、unknown、没有响应的请求。传输重试沿用原 `logical_call_id`，物理 attempt 分开；一个逻辑决策最多接受一个 assistant 返回进入历史。原有预算扣减、unknown 用量和幂等规则不改变。

截断/断流只保存确实收到的片段并标记 incomplete/unknown，不补写未收到的内容，不把片段当完整动作。不需要取得服务商隐藏推理；只记录 API 可观察输入输出。

## 4. A01-R3：复用现有存储，支持完整导出

优先复用现有 journal、blob store、checkpoint 和原子写入方法。新增字段/事件保持版本化；下面是责任示意，不要求重命名已实现的等价文件：

```text
trace.jsonl                 # request prepared/dispatched/response/history commit
blobs/                      # 不可变的完整 request/response/messages
exports/llm_calls.jsonl      # 从 journal/blob 展开的派生文件
exports/sft_per_call.jsonl   # 逐调用蒸馏文件
exports/export_manifest.json
```

允许按内容 hash 去重、压缩或共享前缀，前提是每次输入都能无损展开。首版直接存完整请求 blob 即可，不需要开发复杂前缀存储引擎。

新增命令（已有 CLI 风格不同时可以等价适配并在 README 写明）：

```bash
python -m rlar_harness export-llm-calls --run-dir runs/demo --output runs/demo/exports/llm_calls.jsonl
```

每行对应一个物理 attempt，直接展开完整 request/messages、可观察 response（可为空）、上述身份/状态/来源信息。不能只输出引用。导出不发网络请求，不重新执行工具；新格式记录的 blob 缺失或 hash 不符必须报错，不能生成“近似 prompt”。

## 5. A01-R4：逐调用蒸馏样本

```bash
python -m rlar_harness export-sft --run-dir runs/demo --format per_call --output runs/demo/exports/sft_per_call.jsonl
```

最小样本字段：

```text
schema_version, sample_id, episode_id, logical_call_id, split,
prompt_messages, target_message, tools/response_schema（如适用）,
loss_scope=target_assistant_only,
provenance（原请求/响应 hash、adapter/转换版本、模型配置引用）
```

`prompt_messages` 是本轮生成之前的完整输入；`target_message` 是本轮被采用的完整 assistant 输出。必须遵守：

1. **只监督本轮 target。** 历史 assistant、system/user/tool 都是条件输入；不能因每轮复制完整历史而重复监督早期 assistant。
2. 保留“先前动作 → 实际失败反馈 → 本轮修复”的因果顺序。不得向较早 prompt 加入未来工具结果、最终成功标签或 audit 信息。
3. 默认选 training split 中开发验证成功的 episode；合法动作导致的中间测试失败及修复仍保留。非法 schema、截断、未提交的重试/迟到返回不作为默认正向目标；若已出现在后续真实历史里，则仍作为条件输入保留。
4. 按 `logical_call_id` 和已提交 response 去重；零调用复用不伪造 assistant 样本。原始失败记录不因导出筛选而删除。
5. 按任务家族/基础问题划分数据，再展开 calls；同一 episode 不跨 train/dev/test，不能随机逐 call 切分。
6. 导出 manifest 记录筛选规则、各类保留/排除数量。现有 full-trace 模式每条轨迹只存一次，与 per-call 作为不同训练视图，不默认混合重复加权。

这里交付的是训练数据和监督范围定义。Qwen3 的 tokenizer/chat template、工具编码、thinking 配置与 token-level loss mask 在使用真实 tokenizer 时单独验证；不要将“已导出 JSONL”报告成“已完成训练适配验证”。

## 6. 接入当前构建与兼容旧 run

- 先检查已实现的 LLMClient、adapter、storage、trace/export 和 CLI，列出已覆盖部分；缺什么补什么。模块路径以当前代码为准，不为遵循示意目录而重构已完成模块。
- 已有完整、不可变输入记录且能证明与当时发送内容相符时，可以用于旧 run 导出。只有摘要/不完整历史时，标记 `legacy_trace_unavailable` 并在 manifest 计数，不按当前模板补造旧请求。
- trace schema 新增版本保持读取旧记录的能力；旧 schema 的字段缺失，与新 schema 声称存在但丢失 blob 是两类问题，后者不能被当作普通 legacy 缺失跳过。
- 不因启用采集而改写已有 run/job IDs、原预算、完成结果或历史。兼容恢复时记录采集能力开始生效的事件/版本，之后的新调用完整采集；不可兼容时明确报告，不自动清理旧状态或重跑付费请求。
- 源数据、reward 表示、等权 partial mean、agentic/single-completion 对照、自动提交和终止规则均保持原约定。

## 7. 新增验收：AT-33–AT-36

复用项目现有测试框架和 fake transport，无需真实 API key。

| ID | 验收 |
|---|---|
| AT-33 输入准确性 | fake transport 真正收到的 messages 与导出逐项一致；覆盖 adapter 追加信息、顶层工具/schema、限长 observation；日志中无鉴权凭据。 |
| AT-34 物理请求与逻辑响应 | 成功、重试、unknown、截断、迟到返回全部留档；response 正确关联 attempt；默认训练目标不重复、不包含未采用返回，原预算规则不变。 |
| AT-35 因果关系与监督范围 | 两轮修复中，第二轮 prompt 含第一轮动作和真实反馈，但仅第二轮 target 被监督；第一轮 prompt 无未来信息；同 episode 不跨 split，无 audit/自动提交/零调用伪目标。 |
| AT-36 存储与兼容性 | 压缩/去重后仍能无损展开；新格式缺 blob/hash 错误时导出失败；旧数据无法恢复时明确计数；所有导出均无新增模型调用。 |

再提供一份可运行 demo：两轮“生成失败 → 工具反馈 → 修复成功”，导出逐请求 JSONL 与 per-call SFT JSONL，使用户可以直接查看两轮的完整 message list。这些验收是追加项，不替代原 AT-01–AT-32。

## 8. 本追加需求完成时汇报

汇报接入了哪些现有模块、增加的字段/CLI、AT-33–AT-36 的实际测试结果、demo 输出路径，以及旧 trace 可用性/缺失情况。区分完成采集与导出、未运行的真实服务/tokenizer 验证和未进行的模型训练。若当前构建已覆盖某项，给出对应代码/测试证据，不重复建设。
