# Reward Construction Harness：Claude Code 实现 PRD

> 历史 v1 基线。新建运行默认使用 [v2 更新需求](PRD_REWARD_HARNESS_UPDATE.md)；双合成角色、结构化 ABI、能力级验收与角色导出的实现/验收见 [v2 交付说明](codebase/reward_harness/V2_DELIVERY.md)。下表标记 superseded 的条目仅保留作 v1 回归标准。

> 更新入口（2026-09-28）：以下保留为 v1 历史基线。当前增量开发以 [v2 更新需求](PRD_REWARD_HARNESS_UPDATE.md) 为准，尤其是双 synthesizer、harness-verifier、语义验收、角色训练边界和终止条件；v2 明确替代的条款不与本文件重复执行。实际已有实现见子项目 README。

版本：v1.1，2026-09-23。状态：待实现、可交付开发。v1.1 明确完整 LLM 请求快照及逐调用蒸馏导出。本文定义实现与验收要求，不代表代码、故障测试或效率实验已经完成。

## 1. 交付目标与依据

构建一个 Python harness：顺序读取 query 数据流，复用或通过单个 LLM controller 合成任务级 Python reward function，用任务提供的客观开发测试验证并按需修订，逐 query 输出函数字符串或函数库 key，保留可恢复状态与完整可观察 trace。

实现依据：

- [HARNESS_DESIGN.md](HARNESS_DESIGN.md)：数据、reward 表示、工具、执行后端与外层循环。
- [HARNESS_RELIABILITY.md](HARNESS_RELIABILITY.md)：合成过程的异常处理与中断恢复。
- [HARNESS_LLM_PROTOCOL.md](HARNESS_LLM_PROTOCOL.md)：公共前缀、只追加历史、调用效率。
- [HARNESS_TRACE_SPEC.md](HARNESS_TRACE_SPEC.md)：实际 message list/request/response 留档及 Qwen3-8B 数据导出。
- [METHODOLOGY_REVISION.md](METHODOLOGY_REVISION.md)：实验边界与比较要求。

本文将上述设计细化为工程默认值和验收项。不得用实现便利性改变已确认语义；发现实质冲突时，记录冲突并询问作者，独立模块可继续开发。具体模型、服务地址、实验数据与正式阈值仍由配置提供，不编造为已确认选择。

### 1.1 不可变更的产品约束

| ID | 约束 |
|---|---|
| INV-01 | 任务规范/子类级构造与复用，query 级绑定；选择不依赖待评分 candidate 的表现。 |
| INV-02 | 保留 agent 生成 Python 函数的能力。权威产物是 JSON-compatible 定义和源码字符串，不能只存 pickle、callable 或本地脚本路径。 |
| INV-03 | `single` 与 `checklist` 可选。checklist 始终对成功执行项等权平均；不开放权重设计。 |
| INV-04 | 某项异常不阻断其他项；全部执行失败时 `total_score=null`；全部正常得 0 分时总分是有效 0。 |
| INV-05 | 同一 query/rollout group 固定 artifact、组件、归一化和聚合版本；不同有效 mask 如实记录，不擅自强制公共交集。 |
| INV-06 | 同一 episode 的模型历史只追加；固定前缀、旧消息、角色、调用 ID 和顺序不被重写。 |
| INV-07 | 只在需要新决策时调用 controller；没有隐含的 planner、router、judge 或摘要 LLM。 |
| INV-08 | 网络故障由 harness 有限重试；代码/工具协议错误反馈 agent 修复。恢复与重试不重置预算。 |
| INV-09 | 开发反馈可用于修订；最终 audit 不进入 controller，也不触发替换已冻结产物。 |
| INV-10 | Docker 不是前置依赖；subprocess 不被宣称为文件/网络安全沙盒。 |
| INV-11 | 函数库是简单累进式记忆；完整对话 trace 不自动作为下一 query 的运行时记忆。 |
| INV-12 | 所有失败、fallback、模型评分请求、重试与未知用量都计入报告；不能只报告成功 episode。 |
| INV-13 | 每次 LLM 请求保留最终实际发送的完整 messages 及其他模型输入字段，并关联真实响应；摘要/hash/最终程序不能替代完整输入记录。 |

## 2. 范围、优先级与完成边界

### 2.1 P0：首轮必须完成

1. 可安装的独立 Python package、CLI 和类型化协议；JSONL 流式输入/sidecar 输出。
2. inline/reference 两种保存方式、hash library、可信适用性规则与已验证函数复用。
3. single/checklist 的真实 Python 执行、等权 partial mean 与结构化分项错误。
4. 单 controller 的 agentic 修订循环，以及 single-completion baseline。
5. 公共前缀、只追加消息、`llm_call` 统一入口、批次结果顺序、调用/预算统计。
6. `test_reward`、`submit_reward`、`read_resource`；通过后自动 finalization。
7. 独立的 `Runner` 接口、受控 subprocess 原型和能力声明；不在 driver 内 import/exec 生成代码。
8. 客观任务 pack 协议、可离线运行的数学和代码 fixture、独立可信 evaluator、冻结后 audit 的入口。
9. 固定 RM catalogue/model card、scoring API 接口、mock 评分服务与 broker 的预算/日志链路。
10. 明确状态机、有限重试、预算、取消、checkpoint、崩溃恢复和幂等 commit。
11. 完整事件 trace、每次 LLM 请求/响应快照、逐请求完整消息导出、统计报告、回放工具，以及不训练模型的 SFT 消息导出。
12. `ScriptedLLM` 和一个可配置的 HTTP chat-completion adapter；mock HTTP server 契约测试无需真实 key。
13. 下文 P0 验收矩阵全部通过；提供一条命令可运行的离线 demo。

P0 的离线 demo 必须实际运行 fixture 中的 Python reward 与开发测试，不能只让 mock runner 永远返回 PASS。mock 仅替代外部模型/服务；恢复与质量判定仍走真实 harness 路径。

### 2.2 P1：在 P0 后实现，单独列出进度

| 功能 | 边界与验收 |
|---|---|
| 真实 LLM/RM/隔离服务接入 | 使用作者提供的 endpoint、model revision、密钥环境变量与 runner 能力；没有配置时明确未联调。 |
| 文件与 Bash adapters | Read/Write/Edit 带内容 hash；Bash 仅在配置的执行能力内运行，记录源码改动，不能绕过 audit/预算边界。 |
| Parquet adapter | 分批读取，列映射显式配置；不假设现有数据包含可用 verifier/reference，不修改原始数据。 |
| 较大函数库的 `search_registry` | 版本化检索和适用性证据；小库仍直接提供，不引入向量数据库作为必需依赖。 |
| 扩展对照 | independent drafts、无执行反馈 self-revision、无 library、无 RM、禁用自定义代码、直接 oracle、固定组合。 |
| 原生 tool-call provider adapter | 保留 provider 原始 call ID/消息结构，通过相同历史与恢复验收。 |
| 真实 Qwen3 tokenizer 导出检查 | 按提供的 tokenizer/revision 检查窗口和 assistant loss mask，不下载或训练模型作为 P0 前提。 |

### 2.3 本次不建设

分布式调度、多写者数据库、Kubernetes/Docker 管理平台、在线模型搜索/下载/部署、长程 memory 服务、多 agent 协作、权重优化、开放式任务的 LLM judge、GRPO 训练器、Qwen3-8B SFT 训练任务及网页 UI。

## 3. 仓库落点与实现方式

当前仓库以论文文件为主，`codebase/data/` 与 `codebase/analysis/` 已有数据和分析工作。本 PRD 不要求改动它们。新实现放在独立目录：

```text
codebase/reward_harness/
  pyproject.toml
  README.md
  configs/                 # offline demo、real-service template
  schemas/                 # 从类型定义导出的版本化 JSON Schema
  examples/                # 自包含 query、task packs、mock responses
  src/rlar_harness/
    schemas.py             # 数据与状态协议
    config.py              # 配置解析、preflight、manifest
    driver.py              # 数据遍历、复用、commit
    episode.py             # controller 状态机
    llm/                   # 请求、历史、scripted/HTTP adapter
    tools/                 # test、submit、resource；P1 file/shell
    runtime/               # runner、worker、聚合、scoring broker
    evaluation/            # task pack、dev/audit、准入
    storage/               # canonical hash、library、journal、checkpoint
    trace/                 # replay、report、SFT export
    cli.py
  tests/                   # 单元、集成、故障注入、HTTP 契约
```

目录可合并以减少样板代码，但上述责任边界和测试必须保留。使用 Python 3.11+、Pydantic v2、httpx、pytest；CLI 可用 argparse，YAML loader 只用安全解析。Parquet 依赖作为 extra 安装。为新子项目生成依赖锁文件，使用独立虚拟环境，不改现有 analysis requirements。无需 LangChain/LangGraph 等 agent 框架；采用显式状态机和普通 Python 接口即可。

不得重置工作区、覆盖现有未提交修改、自动批量重写论文、提交真实数据/密钥或自动部署外部服务。本文只授权后续编码任务的实现范围，不要求 Claude Code 自动提交 Git commit 或创建 PR。

## 4. 用户流程与接口

### 4.1 使用流程

1. 用户准备只读 query 数据、task packs、catalogue 和运行配置。
2. `validate-config` 检查 schema、profile、依赖、预算与 runner 能力。
3. `construct` 建立 manifest，逐条复用或合成，写 sidecar、library 与 trace。
4. 中断后，用户显式执行 `resume`；恢复相同 run，不重开预算。
5. `score` 用已选 reward 给 candidate 数据评分，保留 component mask。
6. `audit` 对冻结产物做独立测量，结果单独保存。
7. `report`、`replay`、`export-sft` 消费已有日志，不隐含启动新模型调用。

建议 CLI（相对路径均以新子项目根目录为基准）：

```bash
python -m rlar_harness validate-config --config configs/offline_demo.yaml
python -m rlar_harness construct --config configs/offline_demo.yaml --input examples/queries.jsonl --run-dir runs/demo
python -m rlar_harness resume --run-dir runs/demo
python -m rlar_harness score --run-dir runs/demo --input examples/candidates.jsonl --output runs/demo/scores.jsonl
python -m rlar_harness audit --run-dir runs/demo --suite examples/audit_suite.json --output runs/demo/audit.jsonl
python -m rlar_harness report --run-dir runs/demo
python -m rlar_harness replay --run-dir runs/demo --mode observations
python -m rlar_harness export-llm-calls --run-dir runs/demo --output runs/demo/llm_calls.jsonl
python -m rlar_harness export-sft --run-dir runs/demo --format per_call --output runs/demo/sft.jsonl
```

`replay --mode observations` 只重建/核验历史，不执行服务或代码；重执行必须使用显式 `--mode execute`、独立输出目录和新的成本记录。正式 audit 模式要求隔离能力；offline demo 的 audit 是标明 `prototype` 的合成 fixture 测试，不是受保护审计的证明。

### 4.2 Python 接口

```python
construct_stream(records, config, library, runner, llm_client) -> Iterator[ConstructionResult]
construct_one(record, episode_context) -> ConstructionResult | InterruptedState
score_reward(definition, example, scoring_context, runner) -> ScoreResult
test_reward(definition, dev_profile, on_pass, display_limit) -> ToolResult
llm_call(request: LLMRequest) -> LLMResult
```

大数据不整表载入；首版外层单 query、单写入器。可在同一工具 batch 内并发独立只读动作或隔离测试；并发上限受配置约束。不同 episode 并发不是 P0 要求。

## 5. 数据契约

全部持久化对象有 `schema_version`，使用严格校验、拒绝未知可执行配置字段。路径/资源引用在 run manifest 中解析，核心接口不依赖当前工作目录。

### 5.1 输入与输出

QueryRecord 必需：`query_id: str`、`query: str`、`task_profile_id: str`。可选：`reference`、`metadata`、`reward_mode`。默认模式及是否允许行级覆盖在配置中固定。`metadata` 只接受 JSON 数据；实际进入 reward 的字段由 profile 白名单确定。

```json
{
  "query_id": "math_001",
  "query": "Compute 1/2 + 1/3.",
  "task_profile_id": "rational_arithmetic_fixture_v1",
  "reference": "5/6",
  "metadata": {},
  "reward_mode": "checklist"
}
```

ConstructionResult 字段：

```text
schema_version, query_id, input_location, input_digest,
run_config_digest, library_snapshot, job_key, episode_id, attempt_id,
status, stop_reason, reward_key|null, reward_definition|null,
validation_ref|null, fallback_key|null, trace_ref, usage
```

| status | 含义 |
|---|---|
| `success` | 选定产物通过适用范围内的完整开发准入，或证据兼容的验证复用。 |
| `unvalidated` | 有完整定义，但缺少可信测试依据或只完成静态检查；不能发布为验证通过条目。 |
| `failed` | 未得到可用结果或构造/预算终止；若保留草稿，用诊断引用保存，不伪装成合格 reward。 |
| `skipped` | 预定义过滤的结果，带理由。 |

`validation_ref` 指向的报告另含 `assurance = static / behavioral_prototype / isolated`。demo 的 `success` 仅表示该 fixture 的开发准入通过，并带 prototype 标识；不能用于声称正式隔离或真实任务质量。

坏 JSON/缺字段/重复 ID 产生带输入位置的错误结果并继续。工程默认：同一输入流内重复 `query_id`（即使内容相同）均作为后续位置的 `duplicate_query_id`，使用位置派生的错误 key，不覆盖首条结果；resume 识别已处理的同一位置不属于重复输入。输入文件内容变化导致原 run 不可直接 resume，要求新 run 并显式记录来源。

### 5.2 RewardDefinition 与 hash

```text
RewardDefinition:
  schema_version: rlar.reward.v1
  mode: single | checklist
  components: nonempty list[Component]
  aggregation: {kind: identity | mean}
  runtime_contract: pinned Python/dependencies/SDK/API/aggregator fingerprint

Component:
  id: unique str
  criterion: nonempty str
  source: Python source str
  entrypoint: score
  normalization: declared frozen mapping into [0,1]
  required_apis: list[approved API IDs], default []
```

single 恰好一项且用 identity；checklist 用 mean。拒绝 weights、weighted sum、空列表和重复 component ID。schema 合法性与 Python 语法合法性分开：某项源码语法错误在测试中作为该项错误，其他项仍运行。

入口为 `score(example, context) -> float`。`example` 包含 query、response、允许的 reference/metadata；`context` 只暴露 profile 允许的 checker/评分能力，不暴露文件系统、完整数据集或 audit 标签。返回 bool 默认拒绝，除非 ABI 显式允许；拒绝 NaN、Inf、归一化后超出 `[0,1]` 的结果与异常对象。候选答案本身错误应通常返回有效低分，不能通过抛异常排除困难样本。

P0 支持 identity 和 catalogue/profile 提供的固定归一化映射；agent 只能引用已声明映射，不能通过任意尺度隐式设计权重。输出 raw score、normalized score 及 mapping ID，只有 normalized score 进入聚合。

query、当前参考答案和测试标签作为运行时输入，不能复制到源码中形成特定样例查表。共享静态筛查可拒绝明确的答案/样本 ID lookup，但不能宣称静态检查能证明泛化。额外的格式 gate/强制零分条件仅在 task contract 明确要求时允许，不由 harness 擅自添加。

`reward_key = SHA256(canonical_json(validated_definition))`。先补全 schema 默认值和固定数值类型，源码 CRLF/CR 转 LF，不 strip 或重格式化代码；使用 UTF-8、排序对象键、紧凑 JSON、保留列表顺序、拒绝非有限数字。锁定实现版本并提供 golden hash fixtures。hash 覆盖源码、准则、归一化、组件顺序、聚合与 runtime contract，不包括 query ID、当前参考答案或报告。验证报告单独绑定 key、contract、suite 和 runtime。

### 5.3 ScoreResult

```text
status: ok | partial | failed
total_score: finite float | null
component_results: [{id, status, raw_score|null, score|null, error|null}]
valid_component_ids: [id]
coverage: successful_count / planned_count
reward_key, aggregation_version, usage
```

成功项集合 V 非空时 `total_score = sum(score_i for i in V) / len(V)`；single 等价于唯一项。错误项不是 0 分项。组件独立载入/执行/捕获异常，不能把所有源码放在同一个 import 中导致一项语法错拖垮全体。聚合必须在可信 runtime 中进行，不让生成代码自行报告最终成功状态。

### 5.4 TaskPack 与验证报告

TaskPack 固定 `profile_id/version, task_contract, applicability_rule, permitted_inputs, permitted_apis, mode_constraints, dev_suite_ref, verifier_version, acceptance_policy, report_policy`。audit 的路径/标签由 evaluator 配置持有，不加入 controller 的资源索引或 worker 输入。

开发 suite 至少能表达有独立标签的正确/错误回答对、允许的不变性变换和应改变分数的错误变换。P0 fixture：数学使用精确有理数与可信答案；代码使用固定接口、可信测试及边界用例。不要用生成 reward 的 parser 反过来认证测试标签；检查器覆盖有限测试的局限必须写进报告。

ValidationReport 必需包含：报告 ID、reward/contract/suite/runtime 指纹、样本 ID、计划/完成/失败数量、分项与总分结果、错误类别、排序/不变性指标、临界约束、准入布尔值及失败理由、assurance、usage。未完成测试不能返回 eligible。`partial` 是否满足执行门槛由预声明 policy 决定，不能自动通过，也不能暗中改成必须所有项成功。

缺 dev suite 时，完整定义可经静态检查后以 `unvalidated` 结束，不为追求 eligible 无限调用模型。服务故障导致测试无法完成时走环境重试/中断规则，不能把缺失指标解释为语义质量不合格并要求改 reward；最终评分中的 partial mean 规则仍独立成立。

指标分母保留所有相关计划样本；缺分不静默剔除。错误答案分数达到阈值的比例、正确答案低于阈值的比例及执行错误率分别报告；缺分不是正确拒绝或接受。排序比较的缺分 pair 计入失败/缺失统计，不只在可评分子集上给出一个高准确率。阈值、tie tolerance、coverage/执行门槛全部来自 policy。

### 5.5 Library 与复用

library 保存定义、可信的适用性绑定、验证引用与来源。源码 hash 只定位内容，不证明适用性。零 LLM 复用要求 trusted profile 的规则确认 contract/允许输入/模式/runtime 兼容且证据仍有效；agent 自称适用或文本标签相同不够。需要重新测试但无需判断时可确定性重测；未确认的匹配进入统一 controller。

每个 episode 冻结初始 snapshot。仅已 commit 且通过准入的条目向后续 query 发布；失败草稿和孤立 library 记录不可被检索当成成功工具。主要 held-out 评测固定初始库且任务间不互相贡献；continual 模式才按固定输入顺序累进，并记录该实验模式。

## 6. 模块责任与工具契约

| 模块 | 输入/输出 | 不承担的职责 |
|---|---|---|
| Driver | record → result；控制 cursor、复用、commit、run budget | 不解析模型自由文本来猜测成功，不执行生成 Python。 |
| EpisodeController | 前缀/历史/状态 → 下一轮 llm_call 或终止 | 不自行推进数据文件，不管理外部模型部署。 |
| LLMClient/adapter | 不可变请求 → 完整输出、usage、transport status | 不执行工具，不用隐藏模型修 JSON。 |
| ToolDispatcher | 结构化动作 → 已确认 observation batch | 不篡改历史，不把基础设施错误变成代码错误。 |
| Evaluator/finalizer | 定义+可信 suite → 报告/选定 artifact | 不信任 agent 的 PASS 文件，不把 audit 反馈回 agent。 |
| Runner/Broker | 有界执行/受控 API 请求 → 原始结果与费用 | 不自行变更阈值、调用模型目录外服务。 |
| Storage/Trace | 事件、定义、消息 → 持久化与恢复 | 不用“文件存在”代替逻辑 commit。 |

P0 construction tools：

| Tool | 输入 | 返回/副作用 |
|---|---|---|
| `read_resource` | 当前 episode 允许的 `resource_id, offset, limit` | 固定版本内容、hash、截断标记；用于长 model card、代码或允许的详细诊断。不能读取任意宿主路径/audit。 |
| `test_reward` | 完整 `definition, dev_profile_id, display_limit, on_pass` | snapshot key、真实测试报告、eligibility、成本；`on_pass=submit` 时完整准入后触发同一 finalizer。 |
| `submit_reward` | `reward_key, validation_run_id` | 校验 ownership/适用性/版本/准入，accepted/rejected；选定后交给 driver commit。 |

共同返回 envelope：`schema_version, call_id, status, result|error, usage, trace_ref`。错误有 `category, code, phase, retry_owner, action_id, action_outcome, reward_key, remaining_budget, message`。工具状态表示操作是否完成；发现 reward 质量差是有效 observation，不是 transport failure。

无须独立的 `analyze_task`、`inspect_model`、`plan` 或“检查是否完成”LLM tool。模型一次输出计划与完整定义即可申请测试；计数不能按 checklist 项数强制拆出多次 controller 调用。

## 7. llm_call、公共前缀和效率

### 7.1 请求与历史

`LLMRequest` 至少包含：`episode_id, expected_history_cursor, prefix_ref, history_ref, model_config_ref, logical_call_id, remaining_budget, deadline`。

`LLMResult` 至少包含：`status=complete/incomplete/failed/unknown, assistant_message_ref, proposed_actions, request_digest, provider_request_id, finish_reason, usage, error, trace_ref`。

```text
C_t = P_run + P_episode + H_t
H_(t+1) = H_t + 完整 assistant 动作 + 已确认工具结果 + 状态更新
```

`P_run` 固定指令、工具 schema/顺序、输出格式和静态 catalogue；`P_episode` 冻结 query/task、模式、初始预算、库快照与初始资源。模型/adapter/序列化版本固定。可见预算更新追加在尾部；日志时间戳、request ID 不插到共享前缀开头。不同 query 不共享可变 history。

每次请求前断言既有可见消息保持字节内容、角色、顺序和调用 ID 不变。此断言作用于规范化消息序列，不宣称所有 provider HTTP 字节或 chat template 有相同前缀。adapter 同时保存实际请求 body 引用和摘要；移除 Authorization/API key 等凭据，不将密钥写入 trace。

完整请求采集点必须在 adapter 最后一次加工之后、transport 发送之前；冻结完整 `messages` 和其他模型输入（如顶层 system/tools/response schema），持久化后才发请求，SDK 重试也不能绕过物理 attempt 记录。保存已接收的原始可观察 response 和其完整性/历史提交状态。导出须能直接展开所有内容；具体字段与因果约束见 [HARNESS_TRACE_SPEC.md](HARNESS_TRACE_SPEC.md)。

同一 episode 最多一个 controller 请求在途。接收结果时校验 history cursor；旧 cursor 的迟到结果仅记录，不能执行动作。完整但 schema 非法的输出与诊断按协议追加；流式截断内容只留诊断，不提前执行其中看似完整的子动作。P0 可默认非流式，仍须处理输出截断/连接丢失。

### 7.2 P0 provider 适配方式

提供 `ScriptedLLM` 和通用 HTTP chat-completion adapter。HTTP endpoint 是完整配置地址，不由 harness 猜测；模型名、revision、context limit 和 token counter 配置独立。P0 采用明确的 JSON action envelope，形如 `{"actions":[{"id":"a1","tool":"test_reward","arguments":{...}}]}`，无需依赖原生 tool calling。assistant 原文原样入历史，工具结果通过固定的 harness observation 消息格式返回；消息角色符合配置 API。不得假造 `tool` role 与不存在的 provider call ID。

P0 将具体 wire dialect 固定为 `http_chat_json_v1`：请求 body 含 `model, messages[{role,content}], temperature, max_tokens, stream=false`；读取 `choices[0].message.content`、`choices[0].finish_reason`，可选读取顶层 `id` 和 `usage.prompt_tokens/completion_tokens`。工具 schema 放在固定运行前缀，assistant content 中承载上述 action JSON。`finish_reason=length` 为 incomplete；拒绝/空 choices/不支持的返回形态分别报告，不猜测为成功；usage 缺失记 unknown。对这一 wire contract 编写 mock fixture；真实服务不支持它时添加独立 adapter，不能宣称所有服务零配置兼容。鉴权 header 由 secret env 引用生成，不入请求 body/hash/trace。

### 7.3 批次和自动提交

一个 assistant 输出可含有界的独立动作 batch。按声明顺序入历史，按真实时间另记 trace；结果全部确认或具有明确终态后才继续 controller。依赖动作不误并发，不允许引用尚未获得的 hash。P0 禁止同一 batch 将显式 `submit_reward` 与其他动作混合；多个 `test_reward(on_pass=submit)` 则先收齐相关报告，再按预声明 selector 选定。

默认 `on_pass=submit`，全部准入检查通过后无需模型回复“完成”。`return` 保留显式候选比较路径。多个 eligible 候选默认按 profile 的固定指标与 deterministic tie-break 选择，不能按网络返回先后选择。提交策略和 selector 进入 config digest。

### 7.4 验收的调用效率路径

| 场景 | controller 逻辑调用 | 要求 |
|---|---:|---|
| 明确兼容的已验证复用 | 0 | 仍校验证据与 runtime；必要重测属于工具执行。 |
| 一次构造通过 | 1 | 一次输出计划/代码/test，真实验证后自动提交。 |
| 一次失败、一次修复成功 | 2 | 首次测试反馈真实进入第二次上下文，无隐藏总结调用。 |
| 网络重试后成功 | 不增加逻辑决策 | 增加物理请求计数与实际/未知费用。 |

这些是固定测试路径，不是所有真实任务的平均次数承诺。RM 调用是独立的 scoring 成本，不能藏进“1 次调用”宣传中。使用 provider cache 时只记录服务确认的 cached tokens；无支持/无返回时记 unknown，不推断必然命中。缓存工具结果需匹配定义、输入、suite、runtime 和随机条件；不能缓存本应独立采样的 controller 答案。

上下文预检使用匹配 tokenizer 或 adapter 提供的保守计数，并为最大输出与有界 observation 预留空间。首次加入时按固定规则限长，旧 observation 不再重压缩。不能满足容量时 `context_budget_exhausted`，选择已有合格候选或失败；不静默滑窗，不额外调用摘要 LLM。

## 8. 运行时、模型组件与验证边界

Runner 提供 `capabilities()`、`execute(request)`、`cancel(request_id)`；可查询远端结果时另提供 `lookup(request_id)`。ExecuteRequest 包含不可变定义、白名单评分输入、runtime fingerprint、API capabilities、资源限额与 action ID；输出分项结果、执行状态、usage、服务请求 ID。依赖不满足在执行前明确失败。

subprocess runner：生成代码只在独立 worker 载入，stdout/stderr 与结构化 IPC 分离，限制输出/返回大小、wall time，并在超时/取消时清理子进程组。每个 example 的执行状态重置；组件隔离以免 import/运行错误互相污染。平台不能实施的内存/进程限制必须在 capabilities 中如实声明，不能假报成功。不得只用线程超时实现不可终止的代码执行。

worker 使用显式白名单环境变量，不继承主进程的 API key；工作目录与 IPC 资源独立。这些约束减少意外暴露，但不把同用户进程变成安全隔离边界。原型验收只要求清理已受控的进程树；正式防逃逸依赖隔离 runner 能力。

本地同用户 subprocess 只能作为受控原型。默认离线测试执行项目自带可信 fixture；运行任意模型生成代码需显式 prototype 配置或已配置隔离 runner。正式模式在 preflight 检查文件、网络、label/secret 隔离；能力不足即拒绝正式 audit。远端 runner 可内部使用 Docker，但本地不依赖 Docker daemon。

RM catalogue 项包含 `model_id, revision, model_card_ref, supported_inputs, score_semantics, normalization_id, endpoint_ref, timeout/limits`。目录在运行前固定。reward 通过 `context.score_model(model_id, query, response, allowed_metadata)` 使用 broker；broker 在可信侧持有凭据、执行预算/超时/重试、校验 model ID 并记 usage，worker 不接收主 API key。P0 用 fake 服务验证链路；真实模型名单与版本待作者提供，不运行 Hugging Face 下载/自动部署。

可信 evaluator 持有标签、计算指标与准入，runner 只获允许的评分输入。代码 candidate 自身不通过任务测试通常是有效低分，reward 代码/服务无法评分才是评分错误。对同一 checklist 保留所有分项结果、mask 与 coverage；静态可执行性和语义质量必须分别报告。

## 9. 状态机、异常、预算与恢复

### 9.1 状态层次

```text
Run: NEW → RUNNING → COMPLETED | INTERRUPTED | FAILED
Episode: NEW → INPUT_VALIDATED → REUSE_CHECK
         → LLM_PENDING → ACTIONS_PENDING → OBSERVATIONS_READY → LLM_PENDING ...
         → SELECTED | UNVALIDATED | FAILED
任意运行态 → INTERRUPTED（可恢复，不是完成结果）
SELECTED/UNVALIDATED/FAILED → driver commit
```

`stop_reason` 与 result/score/tool status 分开。用户取消和未决系统中断只记录事件/checkpoint，不伪造 `success` 或加入 completed set。EOF 停止取新数据并有限期收尾；单 query 的确定性失败落盘后处理下一条。共享鉴权、输出存储、版本环境或未知 driver 故障停止外层；连续服务不可用达到配置阈值时暂停整个 run。

### 9.2 错误处理矩阵

| 类型 | retry_owner | 行为 |
|---|---|---|
| 非法 action/schema、未知 tool、源码/入口/返回值错误 | agent | 不执行非法动作；反馈版本绑定诊断，在预算内修订。 |
| 质量指标不达标 | agent | 有效测试 observation，反馈真实失败，不伪装成 service error。 |
| 429、临时 5xx、短暂 worker 不可用 | harness | 同一逻辑动作/不可变输入有限退避重试。 |
| timeout | 按原因 | reward 资源超限反馈修复；基础设施有限重试；无法判断记录 unknown。 |
| 请求派发后结果不明 | harness/none | 优先 lookup/reconcile；仅对安全重放的动作重试，保留重复费用与 unknown usage。 |
| 鉴权/固定依赖/环境不兼容 | none | 保存状态，停止外层，等待显式修复/恢复。 |
| 磁盘写入、trace 损坏、未知 driver 异常 | none | 不报告 commit，停止；不能吞异常继续全量。 |
| 预算、context、无进展上限 | none | 按规则选已有 eligible 版本，否则失败。 |
| 用户取消/进程被杀 | none | best-effort 清理；由已持久化 journal 恢复，不自动后台重启。 |

`failed` 表示确定失败，`unknown` 表示无法确认服务端是否已执行。必须分别处理。内部重试没有新的 assistant 消息；达到限额后不再让 agent 重复发同一个不可用服务动作。未知副作用的 Bash 默认不可自动重放。

### 9.3 预算

run/episode 共享账本记录已消费与预留：controller steps、revisions、tool calls、test cases、component executions、全部模型物理请求、输入/输出 tokens、wall time、最大组件数。所有 checklist 项、controller 重试及 RM 子请求都扣同一上层预算；另保留分项统计。

派发前持久化预算预留，返回后结算。未知用量保留预留并标 unknown，不能归零；若已有硬上界可采用保守扣减。重试另作 admission；没有剩余额度则不发请求。请求的 max output tokens/采样参数在该逻辑调用重试间不改变。

deadline 语义在配置中固定：绝对 UTC wall deadline 持久化，运行期使用 monotonic watchdog 执行超时；重启/停机时间不把同一 deadline 顺延。若已过期，resume 执行预算终止规则。网络同时有连接/读取/总时限；取消不能等到被卡住的请求自然返回。

### 9.4 持久化与恢复

最小 run 目录：

```text
run_manifest.json          # 初始配置、输入指纹、版本、split、seed、deadline
results.jsonl              # 终态结果；逻辑 commit 来源
trace.jsonl                # 追加事件 journal，含 action/预算/消息提交事件
reward_library.jsonl       # reference 模式需要，inline 可选
blobs/                     # 内容寻址的源码、报告、完整观测、消息/请求 body
checkpoints/               # 原子替换快照，可由 journal/结果重建
```

单写入器并对 run-dir 加进程锁；第二个 writer fail-fast。Blob 先写临时文件、flush/fsync、原子 rename；随后写引用事件并持久化。记录 action intent/参数/预留预算 → 派发 → 记录原结果 → 提交 observation/history cursor → 推进状态。checkpoint 必须包含当前 artifact、eligible 候选、prefix/history/request digests、cursor、pending batch 顺序/状态、请求 IDs、预算与版本。

最终提交：先持久化定义/验证/trace 引用，再写完整 result 行并持久化，最后更新可重建索引/checkpoint。result 是 commit；孤立定义不发布。崩溃后重建已提交结果和库，重用原 episode 的 snapshot/job key，不拿当前增长后的库重新计算旧 job。已完成结果跳过；已记录工具结果直接消费；pending unknown 先核对；取消记录不自动启动。

每条事件有唯一 ID 和递增 seq；恢复去重。只允许隔离/截断确认未完成的尾记录；中间行损坏、hash 不匹配或缺失引用停止并报告，不“尽力跳过”。写入 snapshot 失败不能成为继续消耗 LLM 的理由。所有原始输入保持只读。

## 10. 实验控制、trace 与导出

P0 `construction_strategy=single_completion|agentic` 与 `reward_mode=single|checklist` 是独立配置。两种策略初始 task 信息、目录、profile、预算口径与最终 evaluator 相同。single-completion 仅一次 controller 生成，结束后由同一 finalizer 测试；失败反馈不返回 agent，非法输出也不再调模型修复。固定复用策略对两个条件一致；新构造子集另报，避免 0-call 复用掩盖比较。

`trace` 同时保存原始事件和实际模型可见历史：身份/split、配置/库快照、计划、artifact 版本、assistant 原文、tool 参数/结果引用、batch 顺序、调用逻辑/物理 IDs、错误归属、预算、usage、latency、stop reason。审计输出在独立命名空间，不可进入 `read_resource` 或 prompt。

`report` 至少输出：输入/成功/失败/未验证/跳过/中断数、复用率、controller 逻辑次数、物理请求和重试、评分请求、输入/输出/缓存 token、unknown usage、实际或 unavailable 费用、耗时、验证指标、partial/全失败/mask 分布。费用缺价格或用量时用 null/unknown，不写 0。原始总数和按 episode 分布均保留。

`export-llm-calls` 默认逐物理请求输出完整 request（包括直接展开的 messages/工具/schema）、可观察 response、逻辑/物理 ID 与状态；包含失败、重试和无响应请求。它是既有 journal/blob 的派生视图，原始请求须在运行过程中采集，不能在运行结束后靠模板或新 LLM 调用补造。blob 丢失/hash 不匹配明确失败，不退化成摘要。

`export-sft` 支持 `per_call`（默认）、`full_trace` 和 `final_program_only`。默认样本是本轮完整 prompt_messages + 本轮 target_message + tools/schema + provenance，只有本轮 assistant target 计算 loss，历史 assistant 也是条件输入；full_trace 每个 episode 保存一次并对选中的 assistant 各监督一次，两种视图不默认混合重复训练。工具、driver 自动 finalization 不是目标，不导出隐藏推理。默认选择 training split 中开发验证成功的 episode，保留其中真实失败和修复顺序；合法但测试失败的动作默认保留监督，非法 schema/截断/未提交的重试返回不做默认正向目标，但既有历史不删除。全部原始失败 trace 仍归档。

各导出模式使用相同任务划分和选中 episode；同一 episode 的 calls 不随机分散到不同 split。audit 不参与训练筛选，也不能把未来工具结果放入较早请求的 prompt。目标按 logical call/已提交返回去重，零调用复用不伪造训练样本。超窗口 trace 明确排除并计数；Qwen3 model/tokenizer/chat template、工具编码和 thinking 配置显式固定，正式 token-level loss mask 验证在提供 tokenizer 后完成。完整字段及示例见 [蒸馏数据协议](HARNESS_TRACE_SPEC.md)。

P1 的“无执行反馈”必须关闭构造期 test、Bash/code execution 和交互式评分；仅移除 `test_reward` 不是该消融。无库时同时移除旧函数上下文/索引/检索；无 RM 时 broker 也拒绝调用。不能只隐藏工具名却留下等价通道。

## 11. 配置与尚待提供的外部信息

所有预算/阈值必须显式固定并进入 manifest。以下只列配置字段，不指定未经实验验证的正式数值：

| 配置组 | 必需字段 |
|---|---|
| 数据 | 输入 adapter/列映射、任务 pack 根目录、split、输入指纹、输出布局。 |
| 构造 | strategy、reward mode、行级覆盖规则、on-pass、selector、seed。 |
| 模型 | provider adapter、endpoint、model/revision、secret env var 名、生成参数、context/token counter、timeout。 |
| 执行 | runner type/profile、能力要求、runtime fingerprint、CPU/wall/memory/process/output 限额。 |
| RM 目录 | 固定 catalogue/model cards、revision、归一化、评分 endpoint/secret 引用、单次限额。 |
| 预算 | run 与 episode 总量、请求重试上限/退避、无进展阈值、绝对 deadline、每工具/样本限制。 |
| 验证 | dev suite、oracle 版本、准入指标/阈值/覆盖要求、audit 单独配置。 |
| 日志 | blob/trace 限额、单写者锁、截断策略、消息格式/adapter 版本。 |

开发用 `offline_demo.yaml` 必须给出有限、可快速完成的测试数值，并标明仅为 demo。真实配置模板使用 required placeholder；preflight 一次性列出缺失项，不能默默用 demo 阈值跑正式实验。

当前外部依赖待定：真实 controller 服务/model revision、RM 成员与版本、已有隔离 runner、正式 task packs 与准入阈值。它们不阻塞离线核心实现；在用户提供前不声称真实联调完成。实装某一服务时核对其实际 API，不假设可以直接复用另一个 SDK。

## 12. 实现里程碑

| 阶段 | 交付 | 退出条件 |
|---|---|---|
| M1 协议与存储 | package/CLI、schemas、canonical hash、JSONL adapter、manifest、library、事件与预算类型 | schema/golden hash/输入只读/输出关联测试通过。 |
| M2 执行与验证 | worker、聚合、客观 fixtures、evaluator、RM broker/mock、test/submit | 真实执行得到 ok/partial/failed；伪 PASS 无效；版本变更废止旧报告。 |
| M3 LLM 与循环 | scripted/HTTP adapter、固定前缀/history、batch、自动提交、single/agentic | 0/1/2 次调用场景通过；多 query 独立；baseline 无反馈修订。 |
| M4 预算与恢复 | watchdog、重试归属、journal/checkpoint、取消/unknown/commit 恢复 | 进程级故障注入通过，不重复消息/提交，不刷新预算。 |
| M5 交付与分析 | demo、report/replay/export-llm-calls/export-sft、安装文档、P0 验收矩阵 | 全部 P0 自动测试通过；真实服务未测项单独列明。 |
| M6 可选集成 | P1 adapters/消融/Parquet | 逐项验证，不把未配置外部服务报告为通过。 |

每个阶段交付可运行切片；不要先生成大量空接口然后宣布完成。真实外部服务缺失时完成独立工作，明确阻塞的 adapter 联调项即可，不反复请求已确认的产品决策。

## 13. P0 验收矩阵

以下为必须实现并运行的自动验收，不是已经完成的测试。使用 ScriptedLLM、fake HTTP server、真实 fixture worker 和可注入故障点，不消耗真实付费 API。

| ID | 场景 | 必须观察到的结果 |
|---|---|---|
| AT-01 | JSONL 多记录，含坏行和重复 ID | 一条明确结果/错误关联一个输入位置；后续正常记录继续；原文件字节不变。 |
| AT-02 | inline 与 reference 同一定义 | key 相同，均可解析回完整源代码/runtime；索引删除后可重建。 |
| AT-03 | 更改源码、组件、归一化或依赖 | key 改变；旧报告不能提交新定义。仅新增报告不改 key。 |
| AT-04 | `[1,0,0.5]` 全部成功 | checklist 总分 0.5、coverage 1，顺序不改变等权语义。 |
| AT-05 | `[1,error,0]` 与 `[error,error]` | 前者 partial、0.5、coverage 2/3；后者 failed、null。 |
| AT-06 | 全 0、NaN、bool、语法/import 错误 | 全 0 是有效 0；非法项独立失败，其他组件仍执行；bool 按 ABI。 |
| AT-07 | 权重、空 checklist、重复组件 | schema 明确拒绝，不默默重解释配置。 |
| AT-08 | 伪造 PASS 或改报告内容 | 无法获得准入；报告来源/hash/版本被检查。 |
| AT-09（v2 superseded → UAT-06/07/08） | 函数可运行但错奖负例 | 开发质量指标不通过；不会因执行成功直接提交。 |
| AT-10（v2 superseded → UAT-02/12/13） | partial、缺分、缺测试依据 | coverage/错误不被删除；partial 不自动 eligible；缺依据输出 unvalidated。 |
| AT-11（v2 superseded → UAT-19） | 兼容复用/不兼容同标签 | 兼容路径 0 controller call；不兼容不得零调用宣称可用。 |
| AT-12（v2 superseded → UAT-01/20/25） | 首次通过、一次修复通过 | 分别 1/2 次 controller 逻辑调用；测试/自动提交没有隐藏模型调用。 |
| AT-13 | single-completion 测试失败 | 只有一次生成，失败反馈不触发第二轮；与 agentic 共用 evaluator。 |
| AT-14 | 连续三次 controller 请求 | 旧 prefix/message 内容和顺序严格保留；状态只追加；schema 固定。 |
| AT-15 | 两条不同 query | 历史相互独立；第二条只通过许可库看到已提交 artifact。 |
| AT-16 | 并发工具乱序返回 | 完整 barrier 后按原 action 顺序给模型；迟到旧 cursor 输出不执行。 |
| AT-17 | JSON 非法/完整输出截断 | 非法动作未执行；可修错误进入新决策；截断片段不冒充完成 action。 |
| AT-18 | HTTP 429/5xx 后恢复 | 同逻辑请求 digest 不变；物理 attempts/usage 增加，无额外修代码指令。 |
| AT-19 | 派发后断连/结果 unknown | 不假设未执行；lookup 或安全重放；未知预留/重复费用留存。 |
| AT-20 | controller/RM 重试、组件预算竞争 | 均扣同一预算；不足时不发请求；组件/episode 重启不获得新额度。 |
| AT-21 | reward 死循环/大量 stdout/派生子进程 | 超时/输出有界，worker 被清理，driver 可继续；能力不足有明确标识。 |
| AT-22 | context 不足、无进展、deadline 到期 | 有限终止，保留候选/trace，无静默删历史、LLM 摘要或预算重置。 |
| AT-23 | 请求前、派发后、结果落盘后强制杀进程 | 分别恢复到正确状态；已有结果不重跑，消息不重复提交。 |
| AT-24 | library 写后/result 写前崩溃 | 孤立条目不发布；恢复不误判 query 完成。 |
| AT-25 | result 写后/checkpoint 更新前崩溃 | 恢复识别 commit，不重复构造/提交，旧 snapshot 保持一致。 |
| AT-26 | 尾行撕裂、中间损坏、缺失 blob、双 writer | 仅安全处理不完整尾部；中间损坏/引用缺失/双写者明确停止。 |
| AT-27 | 用户取消、鉴权失败、磁盘失败 | 用户取消不自动重启；系统故障暂停外层；未落盘不报告完成。 |
| AT-28（v2 superseded → UAT-04/26） | RM broker 调用未许可模型/耗尽预算 | 拒绝并记因果错误；密钥不进入 worker、prompt、trace；评分请求单独计数。 |
| AT-29 | 正式 audit 使用不足隔离后端 | preflight 拒绝；prototype fixture 结果明确标识，audit 不进入模型资源。 |
| AT-30（v2 superseded → UAT-20/21/22） | observation replay 与 SFT export | 不发新请求；复现消息 hash；仅 assistant trainable，无 audit/自动提交目标。 |
| AT-31 | report 含失败、partial、重试和未知费用 | 全量分母正确，逻辑/物理/评分计数分开，未知值不写成免费。 |
| AT-32 | HTTP adapter/mock wire contract | 完整请求与解析、异常、finish reason、usage 均被断言；无 key 可全套运行。 |
| AT-33 | 最终请求快照与 transport 对照 | fake transport 收到的 messages/顶层工具与 schema 和导出逐项一致；包含 adapter 追加字段，实际限长 observation 不被替换为全量原始输出。 |
| AT-34 | 重试、截断、迟到返回的请求导出 | 每个物理 attempt 均有完整输入/状态；响应与请求一一关联，默认正向目标按逻辑调用及历史提交去重。 |
| AT-35（v2 superseded → UAT-21/22/23） | 多轮 trace 的 SFT 因果/监督范围 | per_call 仅监督当前 assistant；旧 assistant 不重复计 loss，未来反馈不进旧 prompt，同 episode 不跨 split，无 audit/自动提交/零调用伪目标。 |
| AT-36 | 消息引用损坏及无损展开 | 缺 blob/hash 错误时导出失败；共享/压缩存储可还原完整消息；导出不新增 LLM 请求，不生成近似 prompt。 |

此外至少一条集成测试串起“首个 query 构造失败→修复→自动提交→下一 query 复用→进程重启→不重复结果→导出 trace”。崩溃测试必须包含真实子进程终止；只在同一进程抛 Python exception 不足以覆盖落盘时序。

## 14. Definition of Done 与交付报告

- P0 功能具有真实实现，`pytest` 全部通过；CLI demo 无外网连接、无 Docker、无真实 API key 可完成，HTTP 契约测试可使用 loopback fake server。
- README 提供安装、construct/resume/score/audit/report/replay/export-llm-calls/export-sft 命令及 fixture 结果解释。
- 生成一份机器可读验收映射：AT-ID → 测试名称/结果；不得将跳过的真实服务测试算作通过。
- 至少附带完整 demo run 的结果、库、报告、trace、逐次完整 LLM message list、SFT 样本和恢复记录，可从干净环境重现；不提交现有真实数据或带密钥日志。
- 已实现/待实现/P1/外部服务未配置分别列明。报告实际命令、测试数、失败/跳过项、平台与 runner 能力。
- 不声称已改善真实 reward 质量、已实现安全沙盒、已取得缓存加速或已训练 Qwen3-8B，除非另有真实证据。
- 若真实服务尚未给定，离线 P0 可以验收；产品的真实服务联调状态必须保持未完成。

可直接交给 Claude Code 的启动说明见 [CLAUDECODE_IMPLEMENTATION_PROMPT.md](CLAUDECODE_IMPLEMENTATION_PROMPT.md)。
