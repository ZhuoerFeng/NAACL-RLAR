# 当前流程的异常、预算与恢复

2026-09-30。与 [统一需求](PRD_REWARD_HARNESS_CURRENT.md)、[调用协议](HARNESS_LLM_PROTOCOL.md)、[Trace](HARNESS_TRACE_SPEC.md) 共同生效。工程证据见 [交付记录](codebase/reward_harness/UNIFICATION_DELIVERY.md)。

## 终止与错误归属

先生成/准入/冻结 suite，再生成、执行、验收和修订 reward。没有合格候选则 failed；成功提交保存 status=success，展示为 done。用户取消或存储/鉴权/内部故障记录中断，不伪造成提交结果。当前执行不发布 unvalidated reward。

| 来源 | 处理 |
|---|---|
| 非法 JSON、未知工具、参数不符合 schema | 不执行非法动作，返回有界诊断，让对应 synthesizer 在剩余限额内修复 |
| 代码语法/入口/返回值错误、实际行为不通过 | 诊断绑定当前源码 hash，允许 reward 修订；合格旧候选仍需通过同一 finalizer |
| 临时网络/限流/5xx | Durable LLM 按不可变请求、唯一 retry owner 有限重试；每个物理 attempt 消耗预算 |
| Reward 死循环/资源超限 | 结束相关 worker，明确组件错误；不能悄悄记成 0 |
| Required 评分服务无法完成 | 环境失败；不诱导模型修改正确的评分逻辑，连续失败触发外层熔断 |
| 鉴权失败、固定依赖/环境不兼容 | Preflight 或首次发现时停止；不更换模型，不逐 query 重复请求 |
| 用户中断/进程被杀 | 取消与清理，保存可得状态；用户未发起恢复时不自动后台重启 |
| Trace/结果写入失败、损坏、内部异常 | 停止外层，不能吞掉异常继续消耗任务 |
| 预算/deadline/无进展上限 | 选已有且证据仍有效的合格候选，否则失败；不新建 episode 清零 |

结构化错误区分 category、code、phase、retry_owner、action_id/outcome、reward_key、message 和恢复信息。`failed` 与 `unknown` 不同：未收到响应不等于服务端未执行。Tool dispatcher 不另包一套评分传输重试，避免重试乘积。

## 预算和持久化

Run/episode 共享各角色请求、token、工具、测例和组件执行账本，子组件不获得独立免费预算。配置明确有限上限与原 deadline；工具、网络和 worker 的时限受剩余时间约束。派发前持久化预算预留，已消费和未知用量在恢复后继续保留。更换 worker 或重启进程不能重置。

Checkpoint 至少保留 query/job/attempt、输入/配置/library 快照、两个角色历史及 cursor、suite_ref/digest、当前源码/报告/合格候选、pending action/请求 digest、batch 顺序、已预留/消费预算、状态和原因。调用响应/工具结果先落盘，再推进状态；verifier 结果也有独立耐久边界。

| 中断位置 | 恢复动作 |
|---|---|
| 尚未派发 | 沿原动作继续，幂等预算 admission |
| 已派发但未知 | 保留 unknown 与可能费用；只安全重放允许重试的动作 |
| 响应/工具观察已落盘，状态未推进 | 消费已有结果，不重发已确认请求 |
| Suite/判断已冻结落盘 | 复用原冻结依据及决策，不重抽有效 false |
| Library 已追加、结果未提交 | 孤立条目不可见；恢复后完成同一提交 |
| Results 已提交、checkpoint 未推进 | 结果为权威，跳过已提交 job，不重复合成或提交 |

单写入器提交顺序为：落盘所选定义/library → 持久化完整 results 行 → 发布 library → 更新派生 checkpoint。仅完整换行提交的记录可见；末尾截断可恢复，内部损坏拒绝继续。Blob hash、事件连续性及结果引用受检查。

## 恢复边界与验收

输入路径/内容、配置、task pack、suite 资源、源码路径/内容、Python 与依赖指纹必须一致。当前版本重命名也改变指纹，既有运行不能直接强制恢复；原环境负责历史复现。当前版本新建运行可正常 resume，已完成运行的再次 resume 不追加结果或模型请求。

观察 replay 不执行任何工具或模型。独立 score/audit 产生新证据运行，不改变原 construction trace，也不作为构造的恢复入口。运行证据都在 `codebase/reward_harness/runs/YYYY_MM_DD_HH_MM_TaskName/`，时区 Asia/Shanghai；有独立 manifest 的子运行同样遵循命名规范。

测试覆盖模型派发、响应、suite 冻结、verifier 落盘、工具结果、library/results/checkpoint 边界的真实 SIGKILL，以及预算、unknown attempt、断尾、损坏、取消和孤儿 worker 清理。Scripted 模型用于可重现工程检查；不据此宣称真实服务稳定或正式隔离通过。
