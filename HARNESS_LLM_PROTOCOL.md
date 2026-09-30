# 模型调用、角色历史与工具因果协议

当前协议，2026-09-30。配合 [统一需求](PRD_REWARD_HARNESS_CURRENT.md)、[Trace](HARNESS_TRACE_SPEC.md) 和 [可靠性](HARNESS_RELIABILITY.md)。

## 角色和唯一调用边界

`models` 定义模型配置，`roles` 引用 test_case_synthesizer、reward_synthesizer、harness_verifier、rubric_judge。两个合成者可共享模型，历史和 role_episode_id 始终独立。Rule backend 不派发 verifier 模型；所有实际模型请求均经过同一 durable 边界。

Chat Completions 与 Responses 共享 HTTP 传输基础，分别使用 `HttpChatJsonAdapter`、`HttpResponsesJsonAdapter`；wire ID 仍为 `http_chat_json_v1`、`http_responses_json_v1`。Responses 保存原始 input/output 及统一 token 映射，不借远端存储替代本地历史。不可观测 revision/usage 保留 unknown。用户指定模型不可用就记录真实失败，不静默替换。

## 合成历史

每个角色的逻辑输入为固定 `P_run + P_episode + H_t`。运行指令、输出 schema、工具顺序及 ABI 固定；episode 冻结任务、query、许可参考、模式和库快照，reward 角色另绑定冻结 suite。后续只追加完整 assistant 输出、按动作顺序排列的 observations 和状态更新。

不改写旧消息、角色、内容、ID 或其当时可见的截断版本；跨 query 不累积聊天。新状态追加尾部，不能回填前缀。一次 observation 首次进入历史时按固定上限截断，完整内容保存在 blob/资源中。上下文预算不足时有限退出，不静默滑窗或让额外模型总结。

一个逻辑请求只接受一个响应。请求派发前检查 cursor、预算和 deadline，记录最终消息、模型可见 schema 等字段和 wire digest。传输重试沿用同一个请求体，物理 attempt 独立记录与计费。完整但格式错误的输出及诊断进入新的修复决策，半段 JSON 不执行动作；不得伪造 assistant 消息填补超时。

## 工具批次与提交

完整 batch 在任何动作派发前校验。独立只读资源可并发，测试顺序执行；模型可见返回严格按原动作顺序，实际开始/结束时间单独留档。依赖前项 hash/报告的操作不能伪装为独立动作。全部动作取得结果或明确终态后才过 barrier、继续合成。

Reward 一次可输出准则和完整源码并调用 `test_reward(on_pass=submit)`，完整验证后由同一 finalizer 自动提交。`on_pass=return` 保留显式选择。排序规则在配置中预先固定，不按完成速度选择，也不增加额外完成确认 LLM。

Test case 角色仅生成 suite；准入/冻结使用确定性程序。首次新构造通常至少一次 test 合成与一次 reward 合成，rubric/verifier 调用另计。复用仍必须取得并绑定当前 suite，不能把旧单 controller 的“全流程零调用”约定套用到当前流程。

## Rubric 与 verifier

Rubric context 暴露唯一 judge_spec 和 `call_llm_api(message, model_name)`，只允许冻结模型和每组件/候选一次逻辑请求。Utility 返回原始文本，生成源码解析并校验 raw_score；服务错误与代码解析错误分别记录，不把错误填为 0。

Verifier 收到原任务、许可参考、冻结意图和实际分项证据。有效 false 不重抽；格式错误在固定上限内修复。Verifier 和 rubric 的回复可影响后续合成输入，但它们的 actor 不是学生训练 target。反馈消融同时过滤工具观察与 read_resource 报告，不更改 verifier 原证据。

## 限额与可观测性

合成、格式修复、传输重试各有有限上限，所有物理请求共享 run/episode 预算和原 deadline。Adapter 一次派发一次，禁止隐藏重试；外部 utility 需要关闭重试或逐次暴露请求/响应。Auth、存储和内部故障停止外层；unknown 请求和未知费用不记成 0。

按四角色分别报告 logical_calls、physical_attempts、token、unknown 和延时。Prefix caching 是否命中只依服务 usage 判断，不能承诺缓存收益。当前 context/export 容量检查是保守 UTF-8 字节上界，历史 `*_tokens` 命名不代表已接入目标 tokenizer。Token mask、真实费用与模型质量需要独立实测。
