# LLM 请求快照与 Qwen3-8B 蒸馏数据协议

> 更新入口（2026-09-28）：角色分类与监督目标以 [v2 更新需求第 9 节](PRD_REWARD_HARNESS_UPDATE.md#9-模型配置trace-与定向蒸馏) 为准。所有模型调用留档，但默认只监督两个 synthesizer；harness-verifier 与 rubric judge 的回复不作为 base 学生 target。以下保留 v1 采集与导出要求，不再用“所有 assistant”代替角色筛选。

状态：P0 实现要求，尚未实现采集或生成真实训练数据。本文补充 [llm_call 协议](HARNESS_LLM_PROTOCOL.md) 和 [实现 PRD](PRD_REWARD_HARNESS.md)，明确记录 harness 实际发送的每一份完整 message list，不能只保存 prompt 摘要、hash 或最终 reward 代码。

## 1. 记录边界：最终组装后、请求派发前

所有 controller 请求经过统一 `llm_call`/transport 边界。在 adapter 完成角色映射、工具 schema 注入、预算状态追加和序列化后，冻结并持久化实际发送的请求 body，然后才允许派发。存储失败时停止请求，不能先消耗模型再补造日志。

记录分为两种关联视图：

| 视图 | 必须保存的内容 | 用途 |
|---|---|---|
| 实际请求/响应 | 最终发送的 body，包含原样 messages、模型可见的顶层 system/instructions/tools/response schema 等字段，以及实际收到的可观察响应 | 检查模型当时收到什么、输出什么；核对 adapter，排查错误。 |
| 规范化训练视图 | 完整历史、角色、tool call IDs、工具定义和本轮 assistant 输出，附带与原请求的映射版本 | 转换为 Qwen3 的 chat template 和监督样本。 |

不能只在较早的 controller 层记录 messages：adapter 后续可能加 system 信息或改变消息结构。也不能只记录 SDK 调用参数却忽略 SDK 内部重试；SDK 隐式重试须关闭或纳入同一个物理 attempt 日志。

必须保留实际发送的消息内容、顺序、角色、内容块、工具名、工具参数原文、call IDs；不能只保存解析后的 dict，再丢失原参数字符串。若工具结果给模型前被限长，快照保存模型实际看到的限长版本；完整工具原结果另外保存，不在导出时偷偷替换进去。工具 schema 若在 messages 之外，也必须记录，不能仅凭 messages 声称完整保留输入。

鉴权 header、API key 不属于模型输入，不写入日志；凭据本来就不应进入 prompt。原始快照保持不可变；若未来需要对数据做脱敏，另存有版本和来源的派生视图，不能悄悄改动原记录并继续声称逐字一致。只能保证 harness 可观察/控制的请求与响应；不假设可以取得服务商隐含提示、未公开推理或内部 token 序列。

## 2. 记录对象与落盘

沿用单一 `trace.jsonl` journal 与内容寻址 `blobs/`，不再增加一个需要双重 commit 的数据库。

```text
trace.jsonl                       # 请求准备、派发、响应、历史提交事件
blobs/<digest>.json               # 不可变的完整 request/response/messages
exports/llm_calls.jsonl            # 可重建的逐物理请求完整视图
exports/sft_per_call.jsonl         # 默认蒸馏视图
exports/sft_full_trace.jsonl       # 可选：整段轨迹视图
exports/export_manifest.json      # 选择规则、split、版本、排除计数
```

首版可直接为每个不同请求 body 保存完整 blob，按内容 hash 去重。后续可以压缩或共享前缀存储，但必须无损恢复当时的完整 messages 和其他输入字段；不能以节省空间为由仅留摘要。日志空间不足按存储故障处理，不静默截断训练源数据。

每个物理请求的索引至少包含：

```text
schema_version, run_id, episode_id, task_id, split, step,
logical_call_id, physical_attempt_id, expected_history_cursor,
prefix_digest, history_digest, adapter_version,
request_body_ref, request_digest, messages_ref,
model_config_ref, tools_ref|null, generation_config_ref,
dispatch_status, provider_request_id|null,
response_ref|null, response_complete, finish_reason|null,
committed_to_history, schema_valid|null, actions_dispatched,
usage, latency, error|null
```

`response_complete`、`committed_to_history`、`schema_valid`、reward 验证是否成功是不同维度。完整但动作 JSON 非法的回复可能进入历史用于修复，却不能直接执行动作。一次逻辑请求最多一个返回被提交为该决策的 assistant 消息；重试、迟到或取消后的返回仍保留，但不能重复进入历史。

写入顺序：

1. 保存不可变请求 body/messages 和预算预留，持久化 `request_prepared`。
2. 为物理 attempt 保存派发意图，再交给 transport。派发阶段崩溃可能导致是否真正送达未知，按 unknown 处理，不推断为未发送。
3. 收到响应后先持久化可观察原文、完整性和用量，再解析/校验并提交历史。响应记录存在但历史未提交时，可以恢复消费它。
4. 工具真实执行结果沿既有协议落盘；下一轮请求快照包含已确认 observation。

流式调用可附存 chunk 顺序和时间；P0 非流式不需要人为生成 chunk。中途断流记录已收到的片段和 incomplete/unknown 状态，不能当成完整 assistant 样本。客户端未收到的服务端输出无法事后补造。

## 3. 完整请求导出

增加命令：

```bash
python -m rlar_harness export-llm-calls --run-dir runs/demo --output runs/demo/exports/llm_calls.jsonl
```

默认逐物理 attempt 导出，成功、失败、unknown 和没有响应的请求均保留。每行直接展开 `request.messages` 及其他模型输入字段、对应 response 和索引元数据，不能只有 blob 路径。引用缺失/hash 不匹配须报错；不能靠重新调用 LLM 或按当前模板重建“近似 prompt”。导出不发网络请求。

一个逻辑调用发生两次物理重试时，原始导出保留全部 attempts；无损关联到同一 `logical_call_id`。provider prefix cache 或 continuation 优化不能导致丢失本地展开后的完整历史：同时保留实际 wire body 和已知有效的完整 logical context，区分二者。若无法完整重建某次有效 context，该记录标为不可用于默认蒸馏。

## 4. 默认蒸馏单元：本轮输入 → 本轮输出

对每个被选中且提交到历史的完整 controller 返回，构造：

```text
x_t = 本轮发送前的完整消息上下文 + 必需工具/输出 schema
y_t = 本轮 assistant 输出（动作 JSON、源码或显式方案内容）
```

训练视图的最小结构：

```json
{
  "schema_version": "rlar.sft.v1",
  "sample_id": "episode_001:call_002",
  "episode_id": "episode_001",
  "logical_call_id": "call_002",
  "split": "train",
  "prompt_messages": [
    {"role": "system", "content": "Illustrative fixed harness instructions."},
    {"role": "user", "content": "Illustrative task and allowed resources."},
    {"role": "assistant", "content": "Illustrative previous action."},
    {"role": "user", "content": "Illustrative harness observation for that action."}
  ],
  "target_message": {"role": "assistant", "content": "Illustrative repair action."},
  "tools": [],
  "loss_scope": "target_assistant_only",
  "provenance": {"request_digest": "illustrative", "response_digest": "illustrative"}
}
```

这是字段示意，内容不是可执行 demo 或真实 trace。实际导出还须携带目标所需的 response schema、角色转换版本，以及教师/学生配置引用；没有工具时才允许空 tools。

默认 `per_call` 模式仅对当前 `target_message` 计算 loss。历史中的 system/user/tool/旧 assistant 都是条件输入并被 mask，不能因完整历史在每轮重复出现而重复监督旧 assistant。当前 assistant 的多个工具动作属于同一个输出，保留批次顺序；不能拆成缺少前因的独立对话。

`full_trace` 模式每个 episode 导出一份完整轨迹，对每个被选为目标的 assistant 输出计算一次 loss。它与 `per_call` 是两个可选训练视图，不默认混合训练以免重复加权。`final_program_only` 保留为既有消融，不能代替交互 trace。

### 样本选择与因果边界

- 全部原始请求/响应均归档；默认正向训练选 training split 中开发验证成功的 episode。
- 其中合法动作产生的失败测试和后续修复按真实顺序保留；缺陷代码属于轨迹的一部分，选择是否对其监督的策略写入 export manifest，默认保留合法动作。
- 默认不对非法 schema、截断、不曾提交到历史的重试/迟到返回做正向监督。已进入历史的非法回复可作为后续修复的输入，不从历史中抹掉。被排除的目标单独计数。
- `x_t` 只能含本轮生成前已有的信息；之后的工具反馈、最终成功标签和 audit 结果都不能拼进它。验证 outcome 可作为选样元数据，不作为模型输入。
- train/dev/test 按任务家族/基础问题划分，再展开调用样本。同一 episode 的各轮与同源变体不能跨 split；不按每次 call 随机切分。
- 重试按逻辑 call 和已提交 response 去重；零调用复用没有 assistant 生成样本，不能伪造一个“复用决策”来训练。

## 5. Qwen3-8B 的格式适配

保存供应商原消息格式和规范化格式，训练前显式转换为目标 Qwen3 tokenizer/chat template，固定模型 revision、模板版本、工具序列化方式、thinking 配置和上下文上限。P0 保存配置与无损消息；未提供实际 tokenizer 时不宣称已验证 token 对齐。

JSON action envelope 与原生 tool calls 不可在导出时无说明混用。角色或工具编码转换必须可追溯到原始事件，并在学生推理时使用同样 adapter。不得凭空补写 `<think>` 内容或依赖教师隐藏推理。

loss mask 须在目标 chat template/tokenization 后核验，不能只用字符串角色判断就宣称 token 标签正确。检查 assistant 动作/源码的目标区间、特殊 token 边界及 EOS 策略；上下文超长则按已声明规则排除并计数，不静默截去公共前缀或失败—修复关联。

适配实验应将 Qwen3-8B 接回同一 harness，使用真实工具反馈测量 schema 有效率、构造成功率、修复能力、独立 reward 质量与调用成本；仅离线复现教师文本不足以验证 harness 适配能力。模型训练本身仍不属于当前 P0。

## 6. 必须增加的验收

1. fake transport 实际收到的 messages 及工具/schema 输入，与请求导出逐项一致；覆盖 adapter 最后追加的字段和限长 observation。
2. 正常响应、两次物理重试、截断、迟到返回都完整留档；默认训练只选择规定的逻辑返回，计数能对齐原始日志。
3. 两轮修复轨迹导出时，第二轮 prompt 包含第一轮动作和真实反馈，但第一轮动作不在 per-call 第二条样本中再次计算 loss；未来 observation 不进入第一条 prompt。
4. 按 task/episode 固定 split；成功条件只用于选样；无 audit 输入、无伪造自动提交目标、无零调用伪样本。
5. 删除/损坏 blob 后导出明确失败；前缀去重/压缩后仍可无损展开完整 messages；导出不发任何模型请求。

这些要求已加入 PRD 的 AT-33–AT-36，作为 Claude Code 的 P0 交付条件。
