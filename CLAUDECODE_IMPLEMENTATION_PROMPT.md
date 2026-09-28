# 交给 Claude Code 的实现任务

> 本文件是 v1 历史启动任务单。2026-09-28 之后的本次增量开发先阅读 [v2 更新需求](PRD_REWARD_HARNESS_UPDATE.md)，按其 D1–D7、M1–M5 和 UAT-01–UAT-28 执行；不得重建已有项目，也不得把下文“单 controller／不引入 judge”等已被替代的限制用于阻止 v2 明确要求的功能。未冲突的基础约束继续适用。

请在当前 RLAR 仓库实现 reward construction harness。先完整阅读根目录的 `PRD_REWARD_HARNESS.md`，以及它引用的 `HARNESS_DESIGN.md`、`HARNESS_RELIABILITY.md`、`HARNESS_LLM_PROTOCOL.md`、`HARNESS_TRACE_SPEC.md`。以 PRD 中的 INV 约束、M1–M5 和 AT-01–AT-36 为首轮交付标准。

先检查实际工作区和适用的 AGENTS.md/CLAUDE.md；不得重置或覆盖现有修改。将新代码放在 `codebase/reward_harness/`，保持论文、`codebase/data/` 和 `codebase/analysis/` 不变。使用独立 Python package/环境。当前文档是待实现设计，不能假设已经有可运行 harness。

先列出简短实现计划和验收映射，然后按 M1–M5 持续完成 P0，不要停在脚手架或只返回方案。优先打通离线端到端切片，再补真实故障恢复测试、报告与导出。采用显式状态机、普通 Python tools 和单 controller，不引入额外 planner/judge/摘要 LLM 或分布式服务。

必须保留：

1. query 数据流输入、只读源数据、sidecar 输出；Python 函数字符串或 hash library 引用。
2. single/checklist 可选，checklist 对成功执行项等权平均；全部失败返回 null，有效 0 不算失败。
3. 固定公共前缀与只追加历史，同一 episode 的 controller 串行，独立工具 batch 按原顺序返回。
4. 一次生成计划和全部代码，完整验证通过后自动 finalization；兼容复用/首次通过/一次修复的测试路径分别为 0/1/2 次 controller 决策。
5. 区分 agent 修复与 harness 重试，处理 unknown outcome，恢复不重置预算，不重复消息或结果 commit。
6. 客观开发验证、隔离的 audit 接口、完整 trace、single-completion baseline 和 SFT 导出；不得将自动提交事件伪装成 assistant 训练目标。
7. 在 adapter 最终组装后、请求发送前保存每次完整 messages/其他模型输入字段，关联原始可观察响应和重试状态；`export-llm-calls` 可直接展开完整输入，默认 `per_call` 蒸馏只监督当前 assistant 输出，避免重复监督历史消息。

没有真实服务配置时，用 ScriptedLLM、mock HTTP/RM 服务和受控 subprocess fixture 完成验收；测试必须实际执行 fixture reward 和可信 evaluator，不能全部 mock 成 PASS。不得下载或部署大型模型，不需要 Docker，不自动调用付费 API。subprocess 只称受控原型，不能声称提供文件/网络安全隔离。

真实 LLM/RM endpoint、model revision、密钥环境变量、隔离 runner、正式 task packs/阈值若缺失，列为未联调项，并继续完成不依赖它们的 P0。不要猜测服务配置或修改已确认语义。真正影响产品语义且无法从 PRD 决定的问题，请集中提问；普通工程实现细节自行决定并写入 README。

验收时运行全部 P0 测试，尤其是进程级中断、commit 顺序、公共前缀与调用计数。提供安装/运行命令、AT-ID 到测试的映射、离线 demo 输出、恢复证据以及明确的已完成/未完成清单。不要将未运行或跳过的测试写成通过，不自动创建 Git commit、PR 或部署。
