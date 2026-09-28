# Reward 合成过程的中断与异常处理原则

> 更新入口（2026-09-28）：双角色阶段、harness-verifier、done/failed 映射及分项限额以 [v2 更新需求](PRD_REWARD_HARNESS_UPDATE.md) 为准。以下 v1 的有限重试、预算不重置、未决状态和持久化恢复约束继续适用。

本文件专门规定一次 reward 合成遇到报错/中断后，谁处理、从哪里继续、何时终止。它是待实现的恢复协议，不是运行平台稳定性建设方案。数据流、等权 checklist 与函数字符串格式见 [HARNESS_DESIGN.md](HARNESS_DESIGN.md)。

## 1. 三条基本原则

1. **保留当前合成进度，按错误来源处理。** reward 草稿有问题才要求 agent 修订；网络或执行环境有问题时，优先重试同一动作，不误导 agent 修改 reward 逻辑。
2. **只从已确认完成的状态继续。** 工具请求已发出但结果未知、LLM 只生成了半段 JSON、进程被杀，都不算动作成功。已验证的旧版本也不能替新草稿背书。
3. **恢复和重试共享原预算。** 有限的 step、修订次数、调用次数与 deadline 约束整个 episode；中断、换 worker 或重新连接 API 不能将预算清零。

## 2. 异常分类与处理归属

| 异常 | 由谁处理 | 恢复动作 | 达到上限后的处理 |
|---|---|---|---|
| agent 返回无效 JSON、未知工具、参数不符合 schema | agent 修复，harness 提供结构化诊断 | 保留原状态，将具体错误作为下一步 observation；不执行非法动作 | 本次合成 protocol_error；保留草稿与 trace，外层可继续下一条 query |
| reward 代码语法、入口、返回类型错误，或开发测试指出逻辑错误 | agent 修订 | 返回与草稿 hash 绑定的诊断，编辑该版本并重测 | 选择此前完整通过验证的版本；没有则失败或保留 unvalidated 草稿 |
| 网络超时、限流、临时 5xx、短暂 worker 不可用 | harness 自动处理 | 在剩余预算内退避并重试同一个逻辑动作、同一份不可变输入 | 标记 environment_unavailable，不要求 agent 因此修改 reward；按影响范围中断当前 query 或暂停外层 |
| 工具执行超时 | harness 先判定阶段和原因 | reward 自身死循环/资源超限：回传 agent 修复；基础设施故障：有限重试；原因不明：记录 unknown，不能编造代码错误 | 遵循对应类别的终止规则，不能统一无限重试 |
| 必需 API 鉴权失败、固定依赖缺失、环境版本不兼容 | harness 停下并报告环境问题 | 保存进度，等待修复配置；不自动更换模型、依赖或评测标准 | interrupt/environment_error，不记作 reward 语义验证失败 |
| 用户中断、进程退出、连接断开 | harness 保存或恢复 checkpoint | 从最后已确认完成的步骤恢复，核实 pending action 的状态 | 中断不是成功；用户取消后不自动在后台重启 |
| 写结果/trace 失败，或 harness 内部未知异常 | harness 停止 | 保存可得诊断，避免将未落盘状态报告为完成 | 不吞掉异常继续整批循环；修复后再恢复 |
| 步数、修订、调用、token 或耗时预算耗尽 | harness 终止 episode | 按预先规则选已有合格候选；没有则返回失败及草稿/失败原因 | 不重新创建 episode 绕过预算 |

“可继续”不等于永远重试。同一来源的反复失败和无进展动作有有限阈值；硬 deadline 始终优先。

## 3. 避免两层重试互相放大

每个错误明确 `retry_owner = agent / harness / none`：

- `agent`：harness 将完整且有界的诊断作为 observation 返回，让模型决定如何改代码或参数；不在背后自动改 reward。
- `harness`：自动重试同一动作，内部重试过程记录 trace；临时故障消失后，只把确定的结果交给 agent。达到上限后报告环境中断，不让 agent 再无限重复同一故障动作。
- `none`：不可恢复错误或预算终止，直接结束/中断本次合成。

恢复单位是一次明确 action，不是默认重跑整个 query，更不是从数据文件第一条重新开始。单条 query 的确定性失败可记录后继续；共同依赖故障影响全部 query 时，暂停外层遍历，避免每条重复遭遇同一错误。

结构化错误至少包含：

```text
category, code, phase, retry_owner,
action_id, action_outcome, reward_key,
message, remaining_budget, suggested_recovery
```

`action_outcome` 区分 `failed` 与 `unknown`。操作结果未知时，不能把“客户端没有收到结果”当成“服务端没有执行”。

## 4. 最小 checkpoint：保存什么、何时保存

每个 episode 至少保存：

```text
query/job/attempt ID 与输入、配置、library snapshot 指纹
最近一次确认完成的 agent/tool history
当前 reward 定义和 hash、已有验证通过的候选引用
pending action ID、不可变参数/输入 hash、外部 request ID
prefix/history/request hash、history cursor、消息引用、adapter 版本
pending batch 的调用顺序与每项已确认状态
已消费/预留预算、步骤计数、终止或中断原因
```

调用前先记录 pending action 和预算预留；调用完成后，先记录 observation，再推进状态。中断发生时：

- **调用尚未派发**：从该动作前继续。
- **调用已派发且结果可查询**：获取原结果，核验 action ID、版本与输入 hash 后继续。
- **调用已派发但结果未知**：记录 unknown；只对可安全重放的动作重试。计费 API 可能重复执行，保留请求和用量记录，不能宣称零额外成本。
- **结果已记录、状态推进未完成**：消费已记录 observation，避免重新执行工具。
- **LLM 流式输出不完整**：保存片段用于诊断，从最近完整消息状态重新请求；半段代码/JSON 不作为完成的工具动作或成功产物。
- **最终结果已提交**：外层跳过该逻辑 job，不重复合成。仅有草稿文件或 library 孤立条目不算提交成功。

恢复时检查输入、配置、profile 与依赖版本。环境已改变时，新测试记录须绑定新版本，不能把旧验证结果当成当前结果。

`llm_call` 的恢复同时遵守 [公共前缀协议](HARNESS_LLM_PROTOCOL.md)：按原消息重建只追加历史，不重新总结或改写过去的上下文。同一逻辑模型请求的传输重试使用相同请求内容；完整但非法的输出追加诊断后，由新的逻辑决策修复。独立工具批次未全部得到结果或明确终态前，不发下一次 controller 请求。请求重试不重复追加消息，自动提交也必须完成相同验证和结果 commit。

## 5. 与 checklist 部分结果规则分开

作者已确认：成功执行项等权平均，全部项执行失败则总分为空、整条 failed，所有项正常执行但为 0 分则是有效 0 分。

这是生成 reward 的评分规则。合成过程中，`test_reward` 返回的部分项代码错误仍可作为 agent 的修订依据；是否继续修订取决于剩余预算和质量验证，而不是看到 partial 就自动结束。服务掉线导致部分项失败，则先走环境重试规则，不能将它解释为该准则设计有缺陷。

## 6. 实现时需验证的最小场景

以下为待实现的验收场景，并未实际运行：

- 无效工具参数 → agent 修正 → 合成继续，非法动作没有执行。
- reward 测试出错 → 保留版本诊断 → 修订重测，不重置 episode。
- 服务超时后恢复 → harness 重试同一动作，agent 没有被要求重写 reward。
- 连续同类故障/无进展 → 有限退出，而不是循环至整个数据集卡死。
- 在工具请求前、派发后、观察落盘后分别中断 → 按对应恢复点继续。
- 合成结果已提交后重启 → 不重复合成，不重复提交。
- 某条 query 合成失败 → 写明确 outcome，下一条仍可处理。
- 用户取消/预算耗尽/环境故障 → 不误记成功，也不丢掉可恢复的草稿与计数。
- LLM 请求或工具 batch 中途中断 → 恢复同一前缀与已确认消息顺序，不多发一次总结调用，不重复执行已有结果的动作。

首版只需要这些明确规则、有限预算、checkpoint 与 trace。具体重试次数和 deadline 在实验配置中固定，不在方法稿里编造为已验证参数。
