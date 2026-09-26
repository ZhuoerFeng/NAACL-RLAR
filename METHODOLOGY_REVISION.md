# RLAR methodology 改版说明

本文件记录 2026-09-23 的方法设计修订；新增协议是拟实施方案，不代表原实验已经采用，也不包含新实验结果。

## 方法主线

把研究对象从“为回答挑选 reward model”推进为“基于任务测试反馈构造可执行 reward function”。一次构造 episode 的产物是可复用程序及其接口、适用范围、依赖、校准参数、测试报告和完整交互记录。最终比较的是程序在未见样本上的评价质量，以及它对下游 RL 的作用。

单个 controller 负责行动选择，无需多 agent 协作。agentic 性来自可观察的工具调用和测试反馈驱动的修订，而不是增加角色数量、拉长推理文本或在线搜索模型。

## 流式接口、两种 reward 模式与执行后端

完整输入输出、termination 和恢复规则见 [HARNESS_DESIGN.md](HARNESS_DESIGN.md)。核心是 `construct_stream(records, config, library, runner)`，逐条 query 产生构造结果；JSONL/Parquet 是适配层。结果保存函数源码字符串组成的定义，或保存指向可选函数库的 hash key，不覆盖原始数据文件。

- `single`：一个函数。
- `checklist`：有界的计划与子函数列表，固定 runtime 对成功执行项计算等权算术平均，不提供权重设计；全部项执行失败时整条 failed、总分为空。
- key 覆盖源码、模式、组件、归一化与运行依赖；验证记录与 query 输入另存。hash 去重与任务适用性检索分开。
- 文件编辑和 Bash 变为可选；`test_reward` 直接接收序列化定义，`submit_reward` 选择通过验证的 key，持久化由外层 driver 负责。
- Docker 是可选 runner，不是方法前提。可接远程执行服务、Linux 进程沙盒，或在明确限制下用本地 subprocess 做原型。已确认保留自由 Python 函数生成能力，受限 checker/DSL 不是当前路线。
- 每条 query 的所有组件和重试共享预算；外层持久化结果、恢复断点并处理系统性故障。失败、缺测试依据的未验证产物与成功分开。

当前尚未部署任何 runner；尚未将源码字符串用于实际执行。

## 公共前缀与 LLM 调用效率

`llm_call` 使用固定运行前缀、冻结的 episode 任务前缀和只追加的交互历史。后续请求保留旧消息及工具调用顺序；预算等变化追加到末尾。不同 query 独立建会话，累进函数库通过快照复用，不累积整个数据流的对话。恢复保留原前缀、history cursor 与请求 hash，细节见 [HARNESS_LLM_PROTOCOL.md](HARNESS_LLM_PROTOCOL.md)。

一次模型输出可同时包含 checklist 计划与全部源码。哈希、工具调度、批量测试、有限重试和结果提交由程序处理，不增加 planner、judge 或摘要模型。`test_reward(on_pass=submit)` 在完整准入通过后直接提交，失败才向 controller 返回反馈修订；显式 `submit_reward` 仍可用于候选比较。目标路径是兼容复用 0 次 controller 调用、新定义首次通过 1 次调用；这不是已经测得的效率结果。

独立工具可成批返回，但同一 episode 的 controller 决策保持串行。固定前缀可利用服务支持的缓存；缓存节省计算、batch 减少往返、并发减少等待，分别计量。报告逻辑决策数、物理 API 请求/重试、评分请求、token/缓存 token、费用和耗时。短 episode 不做隐式压缩或 LLM 摘要；上下文不足时明确终止并保留候选与 trace。

## 可验证性的要求

- 测试规范、标签来源与数据划分先于 reward 构造确定。agent 可以增加诊断样例，但不能自行创造并认证测试真值。
- 开发测试允许反复调用，用于诊断和修订。独立审计测试仅在最终程序冻结后执行一次，不向 agent 返回反馈，也不用于重选程序。
- 编译/运行成功只证明可执行性。必须另报好坏回答的排序、错误答案被高奖的比例、正确答案被低奖的比例以及针对任务的扰动一致性。
- 内容正确性与格式遵循分别测量。只有任务明确要求的格式才能成为硬性约束；不能将 parser 不兼容直接等同于答案错误。
- 同义改写、数值等价或无关格式变化只有在任务规范明确允许时才作为不变性测试；删去关键信息、篡改答案或引入代码错误作为敏感性测试。有效性须由任务验证器或独立标签确认。
- agent 可使用的评分资源与审计 oracle 分离。若任务 oracle 本来就可以在训练时直接调用，加入直接 oracle reward 基线；不通过隐藏廉价 oracle 制造 agent 优势。

## 比较与消融

核心 baseline 是单次 chat completion 输出完整可执行 reward 定义，而不是单次直接输出分数。single/checklist 表示模式与单轮/agentic 构造策略作为两个独立因素比较。它与 agent 使用相同 backbone、任务说明、初始库快照、model cards、评分 API、开发样例和输出接口。每次最终提交都由同一外部评测器评分。

建议依次比较：单轮程序生成；相同总预算下的独立多次生成及固定开发集择优；多轮自修订但无执行反馈；完整测试反馈循环。完整方法再分别关闭执行反馈、跨任务工具复用、代码构造和 reward-model API。仅移除 `test_reward` 仍允许 Bash 自测，因此另列为“标准化验证工具”消融；“无执行反馈”须同时关闭构造期 Bash 与交互式评分。保留失败程序并计入失败率，不能只比较成功案例。

预算同时记录 controller 输入/输出 token、工具调用、测试样本数、模型评分次数、执行时间和费用。baseline 的独立采样也计入全部消耗；固定模型目录与 model cards 对所有方法一致。先在固定预算上比较，再画质量与成本的关系，避免只比较调用轮数。

## Trace 与可复现性

每个 episode 使用追加写入的事件日志。每条事件保存 `episode_id`、`task_id`、`split`、`step`、`actor_model_version`、`library_snapshot`、`tool_name`、完整参数、返回结果引用及哈希、`artifact_before/after`、`job_key`、`attempt_id`、`mode`、`component_ids`、`test_suite_version`、`status`、`latency` 和 token/费用统计。

同时保存实际可观察的 assistant 消息、简短的方案说明、程序内容或补丁、测试失败与修复、最终提交或放弃的原因。记录不依赖服务商提供隐藏推理内容。大型观测采用内容寻址文件保存，使工具调用可以回放。

审计结果单独存储；不得拼接回 agent 的上下文。失败 episode 也保留用于失败分析。短期工作区在 episode 结束后清空，完整 trace 是训练/审计数据，不自动作为后续任务的运行时记忆。

## 作者已确认

1. 任务规范/子类级构造，query 级选择，同一 GRPO group 固定函数版本与参数。
2. 本轮只以有客观验证器的任务作为新方法的主验证对象，沿用已有数学/代码方向；开放任务暂缓。
3. 写明 Qwen3-8B trace 蒸馏/SFT 与闭环评测，定位为待验证研究问题，不纳入 student RL。
4. 已确认保留 Python 函数构造，checklist 始终等权，不开放权重设计；允许部分执行结果，全部项执行失败时整条 failed。记录有效项与覆盖率。
5. 本轮进一步要求：数据流输入、按 query 输出函数字符串或库 key；single/checklist 可选；明确内外层 termination 与异常处理；Docker 不作为必需架构。
6. `llm_call` 保持公共前缀和时间序列一致性，尽量减少不必要的 LLM call。

## 设计与实验配置待确认


1. 固定模型目录的具体成员与 revision：当前稿件已出现两个 Skywork 8B 模型及 `ByteDance-Seed/Seed-X-RM-7B`，可作为候选；原稿 SeedX 的 7B/8B 命名不一致，不能未经核查锁定部署版本。
2. 开发/审计样本数、构造预算、阈值和客观验证器版本：应在实验 manifest 中预注册，本文不编造具体数值。

## 与旧稿的边界

原 RewardBench 结果只测试固定模型池的选择，不能证明 reward 构造、测试修订或 Qwen3-8B 可学性。旧 WrapLLM 的网页检索、下载部署统计也不对应新方法。原 RL 表格、成本和 ablation 需要重跑或核对实现后才能归属于修订后的方法。

## 文件位置与使用方式

- `Chapters/4_method.tex`：新版方法正文，包含构造工具、验证、最小记忆、GRPO 一致性、trace 学习与对照。
- `Appendix/H_construction_protocol.tex`：流式数据、两种 reward 表示、工具 I/O、终止恢复、可替换执行后端、数据边界、测试构建、消融、trace schema 和 Qwen3-8B SFT 细节。
- `HARNESS_RELIABILITY.md`：合成过程的异常分类、处理归属、有限重试与中断续跑原则。
- `HARNESS_LLM_PROTOCOL.md`：公共前缀、只追加历史、llm_call I/O、工具 batch、减少模型调用和相关恢复/计量约定。
- `HARNESS_DESIGN.md`：数据流与函数字符串、hash library、checklist 聚合、内外循环 termination、恢复协议和无 Docker 后端选择。
- `Appendix/I_legacy_routing.tex`：从原方法节移出的历史 RewardBench 结果，数字保留，明确其只能支持 selection 分析。
- 原实验表格和旧 prompt/工具统计保留并标注版本边界。新方法实验完成后再统一摘要中的效果主张和实验叙述，不将旧结果当作新协议证据。

## 检查结果

- 已通过主稿与独立方法预览的 LaTeX 编译；新方法的交叉引用和公式正常。
- 独立预览包括方法正文、构造与蒸馏协议、历史选择实验；图表和分页随本轮 harness 接口修订更新检查。
- 新方法尚未实现或运行实验，未生成训练 trace，也未训练 Qwen3-8B。预算、阈值与模型版本需要在实际实验前固定。
