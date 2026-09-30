# 完整调用留档、观察回放与定向训练导出

当前协议，2026-09-30；需求见 [统一 PRD](PRD_REWARD_HARNESS_CURRENT.md)，操作命令见 [README](codebase/reward_harness/README.md)。

## 请求与响应权威

在 adapter 完成组装后、transport 派发前，保存完整模型可见输入：messages 或 Responses input、system、工具/schema、输出格式、模型与采样配置。不得只保存摘要/hash/最终代码。请求使用 wire 序列化与 wire digest，保留原字符串空白/换行/Unicode；reward/config 的 canonical hash 是另一种契约，不混用。

每个 logical_call 和 physical_attempt 记录 run/query/episode/role_episode_id、actor_role、training_target_eligible、model/revision/config_ref、adapter、prefix/history/request digest、cursor、派发状态和时间。响应关联实际请求，保存完整可观察原文、finish_reason、provider request ID、token/cache usage、耗时、错误及 unknown。凭据只传输时注入，不放入请求快照或日志。

`llm_dispatched` 后无确认响应是 unknown；完整、schema_valid、committed_to_history 和 actions_dispatched 分别记录。传输重试可有多个物理 attempt，一个逻辑调用只能接受一个响应。工具观察、verifier 证据和完整角色历史都落 blob/journal。

## 读取与回放

`export-llm-calls` 展开所有可观测请求/响应，包括失败、重试和 unknown，不按训练成功筛选。`replay --mode observations` 校验 blob、消息/请求 hash、只追加历史及角色顺序，不执行组件或发送模型请求，`new_requests=0`。

历史记录由 `compat/reading.py` 读取，不用当前模型填默认字段；保持原始 reward/config hash、报告标签和导出身份。旧数据缺少完整快照时计入 `legacy_trace_unavailable`，不补造上下文。派生输出不得覆盖输入、manifest、trace/results/library、blobs 或 checkpoint。

执行回放是新执行，需当前契约、新证据目录及所需环境，不是观察回放的可读性承诺。旧 checker/scalar reward 只能在冻结原环境重现，当前代码不再执行。

## 训练视图

| actor | 筛选条件 | 监督范围 |
|---|---|---|
| test_case_synthesizer | train split 且自身 suite 已成功准入，即使 reward 随后失败 | 本角色合成消息 |
| reward_synthesizer | train split、开发成功，非零调用复用结果 | 本角色合法尝试/修复与产物 |
| harness_verifier / rubric_judge | 从不作为默认目标 | targets=0，必要观察可进入 prompt |

选择基于 actor，不能基于模型名、HTTP role 或工具文本中出现的 assistant 字样。Audit 不回流修订、不改变筛选。Split 在任务家族/run 层预先分配，同 episode 不跨 split，不随机按 call 切分。

唯一 `export_sft()` 支持三种格式：

- `per_call`：原请求为 prompt，仅当前被接受且 schema 有效的 assistant 为 target；历史 assistant、system、用户和工具消息不重复计算 loss。未来反馈不进入此前 prompt。
- `full_trace`：每个角色独立完整历史，仅选中的 assistant 各监督一次；不拼接两角色会话。
- `final_program_only`：仅 reward 角色，明确标为已验证 artifact 的派生视图，不能冒充实际模型原话。

不完整、非法、未提交响应及重试重复项不会成为 target。完整因果历史仍可保留失败观察。超过配置容量的样本整条排除并计因，不切断工具块或静默修改过去的 prompt。`--max-tokens` 历史参数目前按保守 UTF-8 字节上界筛选，尚未执行 Qwen tokenizer 级 mask 验收或训练。

`export-feedback` 单独输出语言反馈视图，不能改变原始 trace。旧 reward 默认排除，只有显式 `--include-legacy` 才导出，并在 manifest 保留历史策略/身份；该开关不恢复旧执行能力。当前输出保持当前 schema，历史单 controller 视图保持 v1 schema。

## 报告和版本

报告按全输入分母计已提交、失败、未处理、中断与复用，分别展示角色请求、token、unknown、部分失败 mask、测例/关系/能力分布、修订数及保证等级。费用不可获得时为 null/unknown，不能因离线 fixture 成功推断真实服务可用。

本次生成报告的字段 `v2_statistics` 改名为 `synthesis_statistics`；消费该展示字段的脚本需迁移。历史报告文件不回写。协议/ABI/wire 标识保留真实版本，源码业务命名统一为职责名称。
