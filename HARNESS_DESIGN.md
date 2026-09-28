# RLAR 流式 reward construction harness

> 更新入口（2026-09-28）：以下为 v1 设计记录。双合成角色、工具化 harness-verifier、统一组件与测例协议以 [v2 更新需求](PRD_REWARD_HARNESS_UPDATE.md) 为准；未被修改的基础约定继续适用。下文“尚未实现”是历史状态，不是当前代码状态。

状态：接口与方法设计，尚未实现或部署。当前修订将 Docker 从默认架构中移除：核心是数据流、可序列化的 reward 定义、可选哈希库、有限构造循环和 trace。执行后端独立选择。作者已确认保留 agent 生成 Python 函数的能力，checklist 本轮始终等权，不开放权重设计；允许部分执行结果，全部子项执行失败时整条失败。延续任务级函数复用、query 级绑定、同一 GRPO group 固定 reward 的约定。

## 1. 核心结构与不使用 Docker 的方式

```text
输入数据流 → 外层 driver → construct_one(query, context)
                              ├─ 读取当前任务和可选函数库
                              ├─ llm_call：固定前缀 + 只追加历史
                              │           一次生成计划与完整定义
                              ├─ test_reward → 可替换 Runner
                              └─ 提交或终止，产生一条结果
输出结果流 ← 单写入器 ← 构造结果 + trace 引用
                         └─ 可选 reward library：key → 定义
```

推荐先定义纯 Python 的迭代接口 `construct_stream(records, config, library, runner)`，文件只是其适配层。JSONL 便于逐条写入和恢复；现有 Parquet 数据可以逐批读入，最终按稳定 ID 合并 sidecar 结果，不必直接修改原始文件。一次构造返回的是函数定义和验证记录，不是提前给尚未生成的 policy response 打分。

| 执行后端 | 是否需要本地 Docker | 用途与边界 |
|---|---|---|
| 现有远程执行/沙盒 API | 不需要 | 本地只做数据流与调度；隔离和进程管理交给服务。仍需固定服务版本、限额、依赖与结果协议。当前未指定或接入某个服务。 |
| Linux 进程沙盒，如 nsjail / bubblewrap 一类后端 | 不需要 Docker daemon | 支持原生程序和文件/进程隔离；需要操作系统权限与配置，通常不直接适用于 macOS。隔离强度取决于实际配置。 |
| 受限 checker 配置/DSL 的解释器 | 不需要 | agent 只选择预定义 checker 和参数，任意 Python/Bash 不开放。容易部署，但减少了可学习的代码构造能力，不能当作与自由 Python 完全相同的方法。 |
| 本地独立 subprocess + 固定环境 | 不需要 | 最轻的受控原型，可防止异常直接杀死主循环，提供超时与进程清理；本身不能阻止同账号代码读取宿主文件或访问网络。 |
| 容器或虚拟机 runner | 可选 | 更换执行后端时保持数据与工具协议不变，不作为论文方法定义的一部分。 |

函数保存成字符串不等于执行时已有隔离。允许生成 Python 或任意 Bash 时，正式验证仍需满足所声明的文件、网络和标签边界。不能在主 driver 内直接 `exec`/import 未验证代码，再寄希望于 `try/except` 捕获死循环。若先用本地 subprocess，必须明确它是受控原型；同账号的另一个进程或文件不是审计标签的安全边界。可把标签与最终判定放在另一个受控账号/服务，并由有权限约束的 runner 执行候选代码。

当前保留 Python 构造能力，优先接已有隔离执行设施；若暂时没有设施，可以先跑生成、序列化、静态检查与日志链路，结果标为未完成行为验证。CPU 检查无需与 RM GPU 服务放在一起。远程服务也可能内部使用容器，但本地 harness 不需要管理 Docker。

## 2. 输入、输出与最小文件集

输入一条记录的示意结构：

```json
{
  "query_id": "q_001",
  "query": "Return a solution to the supplied task.",
  "task_profile_id": "math_v1",
  "reference": null,
  "metadata": {},
  "reward_mode": "checklist"
}
```

- `query_id` 必须稳定；同一 ID 内容变化不能被当作已完成记录直接跳过。
- `task_profile_id` 指向版本化的目标、允许接口、开发测试和阈值。测试数据可以全局共享或由 profile 创建，无需复制到每条输入里。
- `reference`、允许的代码测试等按任务提供。只有 query 而没有可信测试依据时，可以构造定义，但应标为 `unvalidated`，不能宣称已经验证 reward 质量。
- `reward_mode` 由运行配置指定，或允许输入行显式覆盖；实验前固定该规则。agent 不根据某个 candidate 的表现临时切换模式。

输出每条记录均包含以下字段；坏输入也产生带输入位置的错误记录；它使用位置派生的错误 key，避免覆盖已有同 ID 的成功结果：

```text
schema_version, query_id, input_digest, run_config_digest,
library_snapshot, job_key, attempt_id,
status, stop_reason, reward_key|null, reward_definition|null,
validation_ref|null, fallback_key|null, trace_ref, usage
```

`status` 取 `success / unvalidated / failed / skipped`。`success` 表示所选 artifact 通过当前适用范围的完整开发验证，或存在有效的兼容复用记录；`unvalidated` 包括没有测试依据或仅完成静态检查；`failed` 是本次构造没有产生可用结果；`skipped` 是预先约定的过滤。运行中状态留在事件日志中，不伪装成终态成功。fallback 单独标识，不能把 fallback 的可用性算作 agent 成功。

首版只需：

```text
input.jsonl                  # 原始输入，只读；也可由 Parquet adapter 提供
results.jsonl                # 每条 query 的最终构造结果，追加写入
trace.jsonl                  # 所有工具调用、失败、修订、预算与状态事件
run_manifest.json            # 模式、profile、版本、预算、顺序、后端配置
reward_library.jsonl         # 可选：哈希 key 与完整定义；索引可重建
```

不需要先引入数据库或独立 memory 服务。默认顺序处理、单写入器。以后并发时，结果按 `query_id` 关联而不依赖返回顺序，主写入器统一提交；需要跨主机多写者时再考虑事务存储。

## 3. 函数字符串、hash 与复用

`reward_definition` 是权威的 JSON-compatible 定义。单 reward 保存一项函数源代码字符串；checklist 保存多个子函数源代码字符串和固定等权聚合配置。源码不被替换成进程内 callable、pickle 或只能在当前机器使用的路径。每个子函数遵循相同入口 `score(example, context)`。

定义包括：`schema_version, mode, components, aggregation, runtime_contract`。每项 component 包括稳定 ID、评价准则、`source` 字符串、入口、输出归一化和所需的已批准 API。`runtime_contract` 固定 Python/依赖、scoring SDK、API 模型 revision 和聚合器版本，使用后端无关的版本指纹；容器 digest 只是其中一种可选表达。

定义的编码示例（示例函数尚未经过任务验证）：

```json
{
  "schema_version": "rlar.reward.v1",
  "mode": "single",
  "components": [{
    "id": "answer_match",
    "criterion": "Answer equality under the declared task contract",
    "entrypoint": "score",
    "source": "def score(example, context):\n    return context.check_answer(example)\n",
    "normalization": {"kind": "identity", "range": [0, 1]}
  }],
  "aggregation": {"kind": "identity"},
  "runtime_contract": {"scoring_abi": "v1", "environment_ref": "configured_at_run_start"}
}
```

checklist 使用同一结构，例如下面的等权组合；这只是编码示例，不意味着所有数学任务都应加入格式项：

```json
{
  "schema_version": "rlar.reward.v1",
  "mode": "checklist",
  "components": [
    {
      "id": "answer_match",
      "criterion": "Answer correctness",
      "entrypoint": "score",
      "source": "def score(example, context):\n    return context.check_answer(example)\n",
      "normalization": {
        "kind": "identity",
        "range": [
          0,
          1
        ]
      }
    },
    {
      "id": "required_format",
      "criterion": "Only formatting required by the task contract",
      "entrypoint": "score",
      "source": "def score(example, context):\n    return context.check_declared_format(example)\n",
      "normalization": {
        "kind": "identity",
        "range": [
          0,
          1
        ]
      }
    }
  ],
  "aggregation": {
    "kind": "mean"
  },
  "runtime_contract": {
    "scoring_abi": "v1",
    "environment_ref": "configured_at_run_start"
  }
}
```

`context.check_answer` 在实际运行中须解析到任务允许且版本固定的 checker；它不是默认存在的万能 oracle。若可直接使用该 checker，实验中包含直接调用它的 baseline。

支持两种存储布局：

1. **inline**：每条输出同时保存 `reward_key` 和完整 `reward_definition`，源码在 JSON 字符串里。最易检查和搬运。
2. **reference**：定义只存一次于 library，输出保存 `reward_key`。hash index 启动时可从 library 文件重建。若已有下游接口要求一个总函数源码字符串，可从定义导出调用同版聚合 runtime 的 wrapper；导出内容是派生产物，不另立一份可独立修改的权威定义。

`reward_key = SHA256(canonical_json(reward_definition))`。canonicalization 固定 UTF-8、JSON 键排序、数字表示和源码换行规则，拒绝 NaN/Inf；组件列表顺序显式保留。哈希覆盖源码、模式、准则、归一化、组件列表、聚合与运行依赖。只 hash 函数文本会漏掉组件或模型版本变化。当前 schema 不接受可调 weights 字段。字节相同用于精确去重，不主张语义等价去重。

query ID、当前回答、参考答案和测试标签不嵌入可复用函数；它们在调用时作为输入提供。query 特有、会改变算法的参数若绑定到 artifact 中，则必须进入 hash。验证证据和应用记录单独存储，关联 `(reward_key, task_contract, suite_version, runtime_fingerprint)`；函数内容不因增加一份验证报告而改变 key。

哈希索引只解决精确定位，不能回答“哪个函数适合这个任务”。为复用保留少量 `applicability/task_tags` 和验证覆盖元数据即可；小规模时文本/标签过滤足够。命中 hash 或标签也不自动意味着适用，需核验 task contract 与有效验证记录。改变组件、归一化或依赖后重新验证最终组合。

## 4. 单 reward 与 checklist 的统一协议

模式由外层配置选择：

- `single`：恰好一个函数，返回一个经约定归一化的标量。
- `checklist`：agent 输出完整结构化计划与有限个组件。计划在结构上先于实现，但两者可在同一次 LLM 输出中完成，不强制拆成规划和编码两轮。每项计划给出 ID、评价准则和实现方式，不包含可调权重；计划随 trace 保留。组件数受 `max_components` 限制，不能任意扩张成本。

每个组件返回有限的 `[0,1]` 分数，或显式评分错误；bool 仅在接口显式允许时转换为 0/1。原始模型分数先按冻结的归一化映射转换。聚合由 harness 的确定性 runtime 执行，避免每个 agent 重写一套略有不同的加权逻辑。

对某个回答，若组件结果为 `s_1,...,s_K`，当前聚合唯一采用：

```text
single:     s = s_1                         # 执行成功时
checklist:  s = sum(s_i for i in V) / len(V) # V 为成功执行项，非空时
all_failed: s = null                        # V 为空，整条 failed
```

要求计划组件数 `K > 0`。全部成功时按 `1/K` 平均；部分成功时按有效集合 `V` 中每项 `1/|V|` 平均。此部分结果规则已获作者确认。当前 schema 不提供 weighted sum、weighted mean 或权重搜索；实现收到这些配置时应拒绝，而不是静默解释成另一种模式。所有组件使用声明且冻结的归一化协议，不能通过输出尺度差异替代显式权重设计。

整个 checklist 是一个 artifact。冻结其 ID、组件、准则、归一化和等权聚合后，对同一 query 的所有候选使用完全相同的列表与聚合。不能根据得分高低选择性删除子项，也不能针对某个回答改变计划准则或代码。成功项集合仅由预先固定的执行状态规则产生，并完整记录。

评分返回：`status, total_score|null, component_results[{id,status,score,error_code}], valid_component_ids, coverage, reward_key, aggregation_version, usage`。`status` 取 `ok / partial / failed`，`coverage=|V|/K`；是否通过质量验证另用 validation 字段表示。每项独立调用并捕获异常，某项的语法、导入、入口或运行错误不能阻止其他项执行。同时测试组件行为与最终总分的辨别力；若每项能运行但平均后把错误回答排在正确回答前，整个 reward 仍然有问题。对重复准则、遗漏任务要求、相关项重复计权分别作诊断；纯平均不会自动保证合理性。

已确认采用 **partial mean**：部分项 reward 实现异常或服务失败时，保留其 error 记录，仅对成功项求等权均值；有效项为空时 `total_score=null`、整条 `failed`。所有正常执行项都得 0 分时，结果是有效的 0 分，不是执行失败。候选程序按任务要求没有通过测试，也通常是有效低分；不能把错误答案伪装成异常后从均值中排除。

构造/验证输出允许保留部分结果，但 `partial` 不自动等于通过 reward 质量验收。对最终部分均值仍应用统一开发评测并记录缺失项。评分 policy 固定后，同一 group 也可能出现不同有效集合；当前不擅自要求统一交集或丢弃整组，但明确报告其可比性风险。不同集合的均值混用可能使缺失困难检查的回答获得更高 reward，因此记录 component mask、覆盖率与错误原因，并在 GRPO 实验中单独分析缺失模式和诱发异常的情况。若以后加入 group 交集评分或覆盖率门槛，作为另一个明确的实验选项，而不是暗中改变当前约定。

数值聚合不自动引入格式 gate 或强制零分条件；只有任务 contract 明确要求的硬约束才配置 gate。

## 5. Construction tools 的进一步简化

数据流核心不要求创建本地脚本目录。agent 可以直接在结构化动作中提供源码字符串，临时文件仅是文件编辑工具或 runner 的实现细节。

| 接口 | 输入 | 返回与必要性 |
|---|---|---|
| 当前任务与资源读取 | 当前 record、task pack、小型 library/catalogue 索引由上下文提供；长资源按 ID 读取 | 无需独立 `analyze_task` 或 `inspect_model`；agent 自行分析静态信息。 |
| Read / Write / Edit（可选） | 工作区路径、范围、内容或 diff、旧版本 hash | 返回内容/变更/新 hash。适配已有编码工具时可用；最终由 adapter 转成同一个 reward 定义。 |
| Bash（可选） | 命令、工作目录、执行 profile、超时与输出上限 | 返回 exit code、stdout/stderr、截断与终止原因；在所选执行后端中运行，不因未使用 Docker 就默认开放宿主 shell。 |
| `search_registry`（可选） | query、tags、kind、limit、library snapshot | 只在小索引不能直接提供时使用，返回 key、适用范围、定义和验证引用。 |
| `test_reward` | reward definition、允许的 dev profile、最大展示失败数、`on_pass=submit/return` | 接收字符串定义，批量测试，返回版本绑定的组件/总分报告。`submit` 在完整准入通过后调用同一 finalizer，无需再发一次 LLM 请求。 |
| `submit_reward` | reward key、匹配的完整 validation run ID | 检查准入，返回 accepted/rejected、理由和选定 artifact；持久化由外层 driver 统一负责。 |

每次工具调用返回统一 envelope：`schema_version, call_id, status, result/error, usage, trace_ref`。`status` 是工具操作完成情况，与 reward 分数、质量是否通过分开。错误包含稳定 code、阶段、retryable 和简短诊断。完整原始观测存 trace，给模型的输出有长度上限和可访问的引用。Bash 引起的源码变化也记录，不能只追踪 Write/Edit。

`test_reward` 的 profile 只能是当前任务允许的开发 profile，不接受任意 evaluator 路径或 audit 模式。展示限制不改变实际测试数量与指标分母。报告包含 reward key、环境指纹、profile/suite 版本、完成数、错误分类、组件与总分指标、eligibility 和成本。指标不适用时返回 null 与原因。报告由可信 evaluator 根据真实执行结果和 oracle 标签计算，不接受 agent 自写的 PASS 文件。

提交必须经过可信 finalizer，不能凭自然语言“完成”替代。`submit_reward` 显式选择并关闭 episode；默认效率路径允许 `test_reward(on_pass=submit)` 在完整验证与同一提交检查通过后直接关闭，无需模型补一次“完成”调用。`on_pass=return` 用于需要观察报告再选择的实验。对于单轮、无执行反馈等基线，最终结构化输出由相同 finalizer 在生成结束后验证，反馈不返回 agent 继续修订。没有完整开发验证的定义可以以 unvalidated 状态保存，但不发布成可用于正式评分的已验证函数。

### 5.1 llm_call 与公共前缀

完整协议见 [HARNESS_LLM_PROTOCOL.md](HARNESS_LLM_PROTOCOL.md)。同一 episode 的模型输入固定为 `P_run + P_episode + H_t`：运行指令/工具 schema 在前，冻结的任务信息与库快照其次，后续只追加完整 assistant 动作、按调用顺序排列的工具结果和状态更新。不改写旧消息，不逐工具重建孤立 prompt，不把不同 query 串成一个会话。每个 episode 至多一个 controller 请求在途；独立工具可批量执行，依赖动作顺序执行，结果齐备或终态明确后再请求模型。

一次 controller 调用可以输出 checklist 计划和全部源码，由程序完成哈希、批量测试与条件提交。明确兼容的已验证复用可为 0 次调用；新定义首次通过可为 1 次；可修复失败才增加修订轮次。网络重试不增加逻辑决策数，但物理 API 请求和费用仍计入预算。工具不内置隐含的 planner/judge/摘要 LLM。prefix caching 是可用时的计算优化，不能保证命中，也不能替代减少决策轮次。

checkpoint 保留前缀/历史/请求 hash 和 cursor，恢复原始可见历史；不让模型总结后重新开始。观察首次进入上下文时有固定长度上限，旧历史不再压缩；短 episode 预检上下文容量，超限时明确终止，不静默丢弃前缀。

## 6. 内层 termination：每条 query 的有限 episode

一次合成被中断或工具报错后，按 [HARNESS_RELIABILITY.md](HARNESS_RELIABILITY.md) 的恢复协议处理：区分 agent 可修复错误、harness 可重试的环境错误与需停下的中断；明确 retry owner，从最后确认完成的步骤续跑。

```text
NEW → VALIDATE_INPUT → REUSE_OR_DESIGN → TEST → REVISE → TEST ...
                              └──────────────→ SELECTED
任意非终态 → UNVALIDATED / FAILED / CANCELLED
SELECTED / UNVALIDATED / FAILED → 交给 driver 持久化结果
CANCELLED → 中断事件，保留预算与未完成 job，不进入完成集合
```

每条 query 共享一份预算，包含所有 checklist 子项与它们的重试。子项不能重新得到一份完整预算。至少固定 `max_agent_steps, max_revisions, max_tool_calls, max_test_cases, max_model_requests, token_budget, wall_deadline, max_components`，以及单工具/单候选时限。每次工具调用前检查剩余额度；调用 deadline 不得超过 episode/global 剩余时间。外层 supervisor 用独立 watchdog/可取消 worker 执行 episode，网络请求设置连接、读取和总时限；只在进入工具之前检查计数器，不能终止已经卡住的调用。正常返回或抛异常都消耗实际资源，失败调用不免费。

终止条件：

- 有兼容且已验证的复用 artifact，或当前 artifact 通过完整开发验证并被提交：选择结果，关闭 episode。
- 没有足够测试依据：保存构造定义为 `unvalidated`；不伪造成功。
- 任意硬预算耗尽（包括 `context_budget_exhausted`）：若已有完整验证通过的候选，按预注册规则选择它，否则 `failed`。保留 best candidate 不等于允许提交未通过的草稿。
- 相同定义/同一 profile 反复导致相同失败、无有效动作、反复无效 schema，达到有限 stagnation 阈值：终止，避免无意义循环；总步数与 wall deadline 始终生效。
- 不可恢复的记录/任务契约错误：终止当前 query；调度器故障或取消由外层处理。

缓存只在定义、profile、输入、执行版本及相关随机设置一致且允许复用时使用；缓存命中写入 trace。外层对失败 query 的重试也有总 attempt 上限，不因重开 episode 或进程重启重置所有预算。预算预留/已消费和外部请求 ID 持久记录；无法确认外部请求是否完成时保留 unknown 状态，不能假设它免费或从未发生。

## 7. 外层 termination、异常与恢复

外层 `for record in stream` 拥有数据遍历、输出写入和全局预算，agent 无权自行推进 cursor。首版顺序运行，逐条形成明确结果；不会因为单条 query 长循环而无界等待。

| 情况 | 当前 query | 外层行为 |
|---|---|---|
| 缺字段、重复 ID 内容冲突、非法模式 | 写入 input-error 结果，保留行号/ID与摘要 | 可继续下一条，重复策略预先固定。 |
| 语法/入口/返回类型错误、NaN/Inf、质量不通过 | 返回可修复诊断，在总预算内修订 | 不崩溃整个数据流。 |
| 单个组件/代码运行超时 | 终止相关 worker，按评分或构造阶段分类 | 只做有上限重试/修复；禁止将异常悄悄当作 0 分。 |
| 限流、短暂服务不可用、worker 丢失 | 有上限退避；无法恢复则记录失败 | 连续基础设施失败触发全局熔断/暂时停止，避免烧完整个数据集预算。 |
| API 鉴权失败、运行环境不兼容、必需服务未配置 | 标记系统故障 | 启动 preflight 或首次发现时停止，不让每条 query 重复失败。 |
| 输出/trace 写入失败、磁盘满、结果引用损坏 | 不声明提交成功 | 停止并保留可恢复日志；不能继续消耗任务而丢结果。 |
| EOF、全局预算耗尽、用户取消 | 停止接收新任务，有限期收尾/取消在途请求 | 刷新结果与预算；写明 run stop reason 和未完成记录，已完成结果保留。 |
| 未知 harness 内部 bug | 记录诊断 | 停止以便检查；不要用宽泛 `except BaseException` 吞掉故障和取消。 |

结果文件中的每条成功/失败终态，以 `job_key` 去重。`job_key` 包含输入内容/ID、配置指纹和任务开始时的库快照；attempt 单独编号。恢复时沿原先提交顺序重建库状态，保持任务顺序、初始库和随机种子；不能把重试当作全新实验又重复收录结果。

单写入器的最小提交顺序：

1. 将选定定义追加到 library（若使用引用模式），完整写入并持久化。
2. 追加包含 key、状态、验证和 trace 引用的完整结果行并持久化。结果行是该 query 的 commit 记录。
3. 更新可重建的内存索引/可选 checkpoint。后续 query 只看到已提交且验证通过的可复用条目。

若在第 1 与第 2 步之间崩溃，最多留下未被发布的孤立定义；恢复时不将其自动加入可检索库。若结果已落盘而 checkpoint 未更新，扫描结果即可恢复，不重新构造该条。末尾未写完整的 JSONL 行按未提交处理；只清理输出日志中确认不完整的尾记录，不修改原始输入。多个进程并发写不能直接复用这套无锁假设。

系统保证一个逻辑 job 的最终结果幂等，而不是承诺外部 API 恰好调用一次。崩溃后某次 API 可能已被服务端执行，重试会产生重复费用；如服务支持 request idempotency 则使用它，否则通过请求 ID、usage/unknown 记录说明。

## 8. 验证、隔离、消融与 trace 的一致性

最终 evaluator 在执行进程之外持有 oracle 标签，只向 reward 传递允许的评分输入。定义只接受 JSON-compatible 数据和受限的 scoring client；序列化返回同样有大小限制。运行环境不能让生成程序读取整个输入/输出数据集或隐藏测试。对于本地同账号 subprocess，这一权限边界并不自动成立，需使用具有相应隔离能力的后端才能主张审计信息不可访问。

单 reward 与 checklist 是表示模式，不等同于单轮与 agentic 构造。一个 Python 函数本来也能实现复杂聚合；显式 checklist 的价值在于计划结构、分项诊断和可验证性，不能单凭模式名称宣称更强表达能力。实验应区分这两个因素：两种构造策略都可以输出相同模式，使用相同 checker/RM 资源、开发测试、总模型调用和计算预算。checklist 多调用了多少组件必须计入成本，不能只按外层函数调用次数算账。

“禁用标准化 test_reward、允许 Bash 自测”与“无执行反馈”是不同消融。后者同时关闭构造期 Bash/code execution 与交互式评分，但最终程序仍由相同外部 evaluator 验证。memory 消融需移除索引、检索和上下文中的旧函数，而非只删一个工具名称。hash cache 的加速与跨任务语义复用也分别报告。

trace 保存 query/job/attempt、配置与库快照、计划/模式、component IDs、定义 hash、实际工具参数/结果、每次修改、异常类别、重试、预算和终止原因；另存固定前缀、实际消息、history cursor、请求 hash 和 batch 顺序。分别报告 controller 决策、物理 API 请求/重试、评分模型请求、缓存 token 与耗时。原始观测留存，模型可见版本首次进入上下文时限长后冻结。Qwen3-8B 可以学习选择/构造等权 checklist 与处理测试失败；工具返回仍作为上下文，不纳入 assistant 输出 loss，自动 finalization 也不伪装成模型输出。独立 audit 不进入 agent 历史。

每次 `llm_call` 还必须采集最终实际发送的完整 message list、messages 之外的工具/schema 信息及对应可观察响应；请求发出前落盘，不只留摘要或 hash。支持直接展开逐请求 JSONL 和 `(本轮完整输入, 本轮 assistant 输出)` 蒸馏样本，默认只对当前输出计算 loss。具体协议见 [HARNESS_TRACE_SPEC.md](HARNESS_TRACE_SPEC.md)。

## 作者确认与待定配置

已确认：保留 agent 生成 Python 函数；checklist 始终等权，当前不设计或学习权重。受限 DSL 仅作为不采用的替代路线记录，不替换当前 Python 构造能力。

已要求：`llm_call` 保持公共前缀与时序一致性，并减少不必要的模型请求。对应设计采用只追加历史、一次输出计划与代码、批量验证和条件自动提交；实际效率尚待实现测量。

已确认：允许部分执行结果，对成功项等权平均；所有项执行失败时整条 failed，返回空总分，保留完整错误与覆盖率。具体 runner 服务/权限、RM API、profile、预算阈值、最大组件数和默认存储布局在实施前固定。JSONL sidecar 与可重建 hash library 是当前最小实现建议，不要求迁移用户原始数据。
