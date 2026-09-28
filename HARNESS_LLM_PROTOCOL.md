# llm_call：公共前缀、时序一致性与调用效率

> 更新入口（2026-09-28）：[v2 更新需求](PRD_REWARD_HARNESS_UPDATE.md) 将调用方区分为两个 synthesizer、harness-verifier 和 rubric judge，统一底层调用但分别维护角色历史与输出协议。以下单 controller 描述属于 v1；只追加历史、不可变请求和物理调用留档约束继续适用。

状态：待实现协议。沿用 [HARNESS_DESIGN.md](HARNESS_DESIGN.md) 的流式输入和单 controller，以及 [HARNESS_RELIABILITY.md](HARNESS_RELIABILITY.md) 的有限重试/恢复规则。本协议将模型上下文构建集中到一个 `llm_call` 边界；工具数量增加不应自动增加模型数量或调用层数。

## 1. 一个 episode，一条只追加的模型可见历史

第 t 次 controller 决策的逻辑输入为：

```text
C_t = P_run + P_episode + H_t
H_(t+1) = H_t + A_t + O_t + S_(t+1)
```

- `P_run`：固定 controller 指令、工具 schema 及顺序、输出 schema、reward ABI、静态 model cards/catalogue。按相同版本和配置共享；与数据切分或权限有关的资源只在权限一致的运行中共享。
- `P_episode`：任务规范、当前 query/允许元数据、开发资源、reward 模式、初始预算、初始 library snapshot 及必要的检索结果。在当前 episode 内冻结。
- `H_t`：截至当前决策已确认的历史。`A_t` 为完整 assistant 输出及工具调用，`O_t` 为与调用 ID 对应的工具结果，`S_(t+1)` 为剩余预算、当前 artifact 等必要状态更新。尚无交互时历史为空。

因此，同一 episode 的后续逻辑输入保留先前输入作为前缀。旧消息的内容、角色、顺序、工具调用 ID 和可见截断版本不改写。不能每轮重写 system prompt、把新错误插回旧消息、替换最初的草稿，或只给最新一步临时组装一个孤立 prompt。最新状态追加在末尾并带版本；旧状态保留其当时的含义。

`S` 可附在该轮最后一个工具返回的固定 metadata 字段中，或使用 adapter 支持的显式 driver 消息；在运行前固定表达方式，不插入不符合 API 协议的角色或伪造工具结果。无效动作的诊断也通过同一已声明通道返回。

跨 query 只共享静态 `P_run`，以及确实相同、可共享的任务前缀；每条 query 建立自己的 episode。累进 library 通过新 episode 的快照进入上下文，不把整个数据流累积成一个聊天会话。当前 episode 的检索绑定已冻结快照，当前新草稿/报告通过后续 observation 可见，不回填前缀。

这是消息和因果历史的约定，不假设不同厂商的 HTTP 请求字节、chat template 或 KV cache 完全相同。固定序列化器与 API adapter 版本，保存实际请求及其 hash。若接口支持 prefix caching，可复用相同的前导 token；命中与收益以服务返回的 usage 为准。请求 ID、时间戳和退避次数留在调用元数据/trace 中，不能放在公共前缀开头导致每次失配。provider 的 continuation ID 仅作为优化，本地历史仍是恢复依据。

## 2. llm_call 的职责与 I/O

概念接口：

```text
llm_call(
  episode_id, expected_history_cursor,
  prefix_ref, history_ref, model_config_ref,
  logical_call_id, remaining_budget, deadline
) -> {
  status, assistant_message_ref, proposed_actions,
  request_digest, provider_request_id,
  usage, finish_reason, error, trace_ref
}
```

| 边界 | 约定 |
|---|---|
| 输入 | 只引用已确认、不可变的前缀与历史；model revision、采样配置、工具 schema/顺序和 response format 固定。剩余预算用于 admission 和输出上限；可见预算状态在历史尾部。 |
| 模型输出 | 保留完整可观察 assistant 消息和结构化动作，可包含一个有界动作 batch；不依赖隐藏推理。`complete / incomplete / failed / unknown` 与 reward 质量状态分开。 |
| 校验 | adapter 检查协议与 schema，不另调 LLM 修 JSON。完整但非法的输出及诊断进入历史，由下一次 controller 决策修复；截断片段只存诊断，不作为完整工具动作执行。 |
| 权限 | `llm_call` 只提出动作；harness 检查版本、依赖、权限和预算后执行。解析到工具名称不等于已经执行。 |
| 串行性 | 同一 episode 最多一个 controller 请求在途；history cursor 不匹配的返回不得附到新历史或触发动作。外层未来可并发不同 episode，但不能共享可变 history。 |
| 重试 | 同一个逻辑请求的传输重试沿用相同输入、配置和 request digest；物理 attempt 单独计费/记录。在剩余预算内重试，不能悄悄改 prompt 变成一次新决策。 |

异常不一概触发新 controller 轮次。harness 内部重试及预算流水始终记入 trace；只有决策所需的已确认结果/诊断进入模型历史。传输失败但未产生完整 assistant 输出，不补造一条 assistant 消息。

若 provider 截断输出，需要继续生成时，以最近完整历史追加一条明确的截断诊断，发起新的逻辑请求；这不是同请求的透明重试。若已经没有上下文或输出预算，则直接终止。

## 3. 工具批次保持因果顺序

一次 assistant 输出可以申请多个相互独立的工具调用。保留 provider 返回的原始调用 ID；自定义协议才由 harness 分配稳定 ID，内部 action ID 与原 ID 的映射另存。允许独立只读请求/隔离测试并发执行；全部调用取得结果或明确终态后，再发下一次 `llm_call`。给模型的结果按原调用顺序追加，实际开始/结束时间另存 trace。不能因网络返回快慢改变上下文顺序，也不能在部分结果到达时让 controller 抢先作下一步决策。

互相依赖的操作不能当作独立 batch：例如修改同一文件、使用前一步才产生的 hash、读取尚未生成的测试报告。已知的确定性依赖由程序顺序执行；下一步需要解释新信息时才交给 controller。首版优先将完整 reward 定义直接传入 `test_reward`，减少 Write → Read → Test 的往返。每次测试内部批量执行 checklist 子项/开发样本，返回一份版本绑定的报告。

中断恢复保留 batch 内每个 action 的状态。已有结果不重跑；结果未知的请求先按恢复协议核对。尚未到达 batch barrier 时不发下一轮 controller 请求。

## 4. 调用只用于需要模型作出的决策

| 阶段 | 默认实现 | controller 调用 |
|---|---|---|
| 输入校验、按 task contract 查库、校验复用证据 | 普通程序；只有明确兼容且已有有效证据才走复用快路 | 明确命中时 0 次；匹配不确定时交给统一 controller |
| 首次构造 | 一次输出评价计划和全部 Python 源码；计划在结构上先于实现，但不强制单独一轮 | 1 次 |
| 静态检查、开发测试、组件平均、报告与提交校验 | 确定性工具与可信 evaluator | 0 次；如 reward 调用 RM API，另记模型评分成本 |
| 测试通过 | 依据预先声明的提交规则直接 finalization | 0 次，不让模型再回复“完成” |
| 可修复的代码/质量失败 | 合并该版本的有界诊断；一次修订可修多个组件 | 每个需要决策的修订轮次 1 次 |
| 临时网络错误、请求重试、hash、日志、checkpoint、落盘 | harness 状态机 | 不增加逻辑决策次数；实际 API 重试照常计数 |

`test_reward` 增加 `on_pass = submit / return`，schema 在 episode 开始前固定。默认效率策略为 `submit`：调用中的定义已是 agent 预选的候选，完整验证及提交检查通过后，harness 执行与 `submit_reward` 相同的 finalizer 并关闭 episode。未通过时追加报告，agent 在预算内修订。它不能跳过适用性、版本、完整开发测试或质量阈值检查；checklist 的 partial 状态也不自动触发通过。

若同一 batch 有多个候选，按预声明的选择规则在相关验证全部完成后选定，不以“谁先返回”决定结果。外层 driver 仍负责最终 commit；测试通过不等于已经落盘。`on_pass=return` 保留显式 `submit_reward`，用于需要观察结果再比较候选的实验；提交策略在方法间明确记录，不能暗中变化。

正常的最短路径是：

```text
确定性查库 → 有效复用 → commit                         # 0 次 controller 调用
          → llm_call：计划 + 完整定义 + test(on_pass=submit)
                    → 完整验证通过 → finalizer → commit # 1 次
                    → 反馈 → llm_call：修订 + test ...  # 按需增加
```

不额外设置 LLM planner、task analyzer、model-card reader、tool router、test summarizer 或 completion judge。短报告由确定性模板、统计量和代表性失败项组成；完整的允许观测保存在可按引用读取的资源中。每个 observation 首次进入上下文时按固定规则限长，此后不重新压缩旧消息。

固定资源与适用性索引可在首次调用前按元数据预取；基线获得相同信息，预取不能引入隐藏的 LLM helper。结果缓存只复用相同版本/输入/随机条件下允许复用的工具结果，不复用本来要求独立采样的新 controller 生成。prefix cache 复用计算，不等于复用上次答案。

短 episode 默认不使用 LLM 摘要或静默滑窗。每次调用预检输入长度，并为最大输出和有界工具返回预留空间；超限时返回 `context_budget_exhausted`，按已有合格候选/失败规则终止并保留完整 trace。以后若要增加 compaction，应另立显式上下文 epoch 和实验条件；不能声称压缩前后仍满足完整前缀不变。

## 5. 恢复、trace、消融与验证

每次调用必须在 adapter 最终组装后、transport 派发前，持久化实际发送的完整 message list，以及 messages 之外的模型可见工具/schema/system 字段；响应原文与该请求逐一关联。不能只保存摘要、hash 或最终程序。具体记录字段、逐请求完整导出和 Qwen3-8B 样本协议见 [HARNESS_TRACE_SPEC.md](HARNESS_TRACE_SPEC.md)。

checkpoint 在原有状态之外保存 `prefix_digest, history_cursor, history_digest, adapter_version, logical_call_id, request_digest`、实际消息/请求引用以及 pending batch 的调用顺序/状态。恢复时重建相同前缀和已提交历史；不调用 LLM 生成“此前发生了什么”的摘要，不重复追加已记录的 assistant/tool 消息。用 cursor 校验和幂等事件 ID 保证单次提交；一个逻辑请求只有一个被接受的 assistant 结果。

trace 区分：controller 逻辑决策数、物理 API 请求数及重试、工具调用数、RM/其他模型评分请求数，以及模型输入/输出 token、缓存 token、费用和耗时。合并工具请求减少的是往返；并发主要减少等待；prefix caching 主要减少 prefill 成本。均不能伪称减少了必要评分样本数或已经取得某个加速比例。

SFT 按实际可见的前缀、assistant 动作、工具观测训练，保留 batch 顺序。默认逐调用导出 `(本轮完整输入, 本轮 assistant 输出)`，只对当前输出计算 loss，历史 assistant 也是被 mask 的条件输入，避免完整 message list 重复造成早期动作反复监督。整段 full-trace 可另行导出。harness 的自动提交事件不伪装成 assistant 生成，也不计算 assistant loss。单轮 baseline 使用相同起始信息、接口和最终验证；不能得到失败反馈后继续修改。agentic 方法只在需要反馈修订时增加轮次，以质量—总成本曲线检验收益。

待实现的最小验收场景：

- 连续调用的逻辑消息序列满足前缀扩展；任务前缀、工具 schema 与历史原文不被改写。
- 独立工具按不同完成顺序返回，模型可见 observation 顺序仍一致；有依赖的动作不被误并发。
- 网络重试维持 request digest；恢复后不重复追加消息，不重置预算。
- 有效复用为 0 次、新定义一次通过为 1 次 controller 调用；失败后才进入修订，自动提交仍有完整验证依据。
- 上下文将溢出时有明确终态，没有静默丢历史、偷偷摘要或额外模型调用。

以上为设计目标和验收标准，尚未运行效率实验或实现相关接口。
