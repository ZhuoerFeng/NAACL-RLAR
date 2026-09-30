# Reward Harness 版本统一与代码删减执行文档

日期：2026-09-30  
状态：已获用户批准并完成实施（2026-09-30）。交付与验证见 [UNIFICATION_DELIVERY.md](codebase/reward_harness/UNIFICATION_DELIVERY.md)。下文保留原方案的盘点基线和决策，描述“现状”时均指执行前工作区。  
盘点基线：当前工作区，Git 分支 `main`，HEAD `00f1626`，包含已有未提交修改及未跟踪文件。  
主要范围：`codebase/reward_harness/`；关联范围：项目根目录中的 harness 需求、设计与交付文档。

## 1. 目标和本次决策

将框架收敛为一套当前实现：**双合成角色 + 冻结测例 + 结构化评分 + harness-verifier + 模型自行实现评分逻辑**。新运行统一使用 `self_contained_v1` 评分策略，Python 类、函数和日常入口使用职责名称，移除旧流程与新流程并行维护的分支。

“最新”以以下要求共同确定，而不是仅以文件名含 `v2` 判断：

1. [PRD_REWARD_HARNESS_UPDATE.md](PRD_REWARD_HARNESS_UPDATE.md)：双合成、能力级验收、四角色调用与定向导出。
2. [PRD_REWARD_LOGIC_OWNERSHIP.md](PRD_REWARD_LOGIC_OWNERSHIP.md)：2026-09-30 修订，评分核心由生成源码实现，评分侧和验收侧均移除任务级原生 checker。
3. [AGENTS.md](AGENTS.md)：运行目录、只读训练源、真实调用留档和隔离能力声明约束。

本方案采用的收敛边界：

- **新建、恢复当前运行及重新执行评分**：主框架仅维护最新流程。
- **旧运行的报告、原始请求导出和观察回放**：保留最小读取兼容，保持原始字节、hash、签名和版本标识。
- **旧流程重新构造 reward、重新执行旧 checker 或 scalar ABI**：退出当前执行入口；需要复现实验时使用冻结的原始代码和依赖环境，不在新框架内保留第二套执行器。
- **历史训练视图**：继续默认排除旧 reward；显式 `--include-legacy` 仅控制已有轨迹的导出，不开启旧评分能力。
- **代码命名与存储协议分开处理**：移除业务代码的 `V2/_v2` 命名，不将所有协议字符串机械替换成相同版本号。

方案编写阶段只交付静态盘点，未改代码。用户随后明确同意执行合并；实施阶段已完成 P0–P5，最终 164 项离线工程测试通过，当前 demo 与恢复示例通过，未调用真实模型。完整文件迁移及测试退休依据见交付记录。

## 2. 已确认的现状

静态盘点覆盖 50 个 Python 源文件及 19 份 JSON Schema。主代码中有 **49 处 `.is_v2` 引用**，集中在 `config.py`（10）、`driver.py`（14）、`episode.py`（10）、`tools/dispatcher.py`（13）、`cli.py`（2）。此外还有根据 `schema_version`、ABI 和评分策略分流的代码。

实际上有三种语义状态混在两套版本命名中：

| 状态 | 当前代表入口 | 主要行为 | 目标处理 |
|---|---|---|---|
| 初始流程 | `configs/offline_demo.yaml`、`scripts/offline_demo.py` | 单 controller、预置 DevSuite、scalar ABI、FAR/FRR 等全局门槛 | 退出当前执行入口，保留必要的历史读取样本 |
| 早期双合成流程 | `configs/offline_v2_legacy.yaml`、`examples/v2/` | 双合成和结构化 ABI，但仍使用原生 checker | 删除可执行旧路径，保留必要历史证据样本 |
| 当前流程 | `configs/offline_v2.yaml`、`configs/aihub_v2.yaml`、`examples/self_contained/` | 双合成，源码自行实现评分，测例仅做结构与来源准入 | 成为唯一主流程，入口改为职责命名 |

### 2.1 文件和符号级处理清单

下表路径均相对于 `codebase/reward_harness/`。行号是本次盘点位置，执行时以符号为准。

| 位置 | 已确认的问题或重复职责 | 执行动作 |
|---|---|---|
| `src/rlar_harness/config.py:207` | `V2Config`、`.v2`、`.is_v2`；单 `model` 与 `models + roles`、`rm` 并存 | 当前配置收敛到一个模型目录和角色映射；内部命名改为 `SynthesisConfig` / `.synthesis`；历史解析移至兼容边界 |
| `src/rlar_harness/schemas.py:27`、`:284` | 多个 schema 常量仍默认 v1，`RuntimeContract.scoring_abi` 默认 v1；同一模型同时接受新旧对象 | 当前模型明确限定当前 reward/task pack/ABI；历史对象使用独立读取模型或原始数据视图，避免修改默认值后重解释旧对象 |
| `scripts/export_schemas.py` | 从混合模型导出后，只将顶层 `schema_version` 改为 v2 | 改为从当前权威模型直接导出，核对嵌套约束；例如当前 `rlar.reward.v2.json` 中 ABI 仍默认 v1，不能仅检查文件名和顶层 const |
| `src/rlar_harness/driver.py` | 14 处分流；新流程仍先创建旧 `ScoringBroker`；fixture 逻辑夹在 adapter 工厂内 | 只组装当前角色和 `JudgeUtility`；清除旧 controller/reuse 分支；fixture 选择逻辑移至明确的离线 fixture 模块 |
| `src/rlar_harness/episode.py` | 合成循环含两套预算、错误、调用 ID 和终止语义；测例合成中仍定义 objective checker 回调 | 保留当前有限重试与双角色状态机；删除旧条件和 objective checker 回调，保持 checkpoint、提交和预算语义 |
| `src/rlar_harness/evaluation/evaluator.py:176,240` | `validate()` 分流到 `validate_v2()`；旧指标门槛与当前 verifier 路径共存 | 当前实现成为唯一 `validate()`；删除旧 `_compute_metrics/_decide` 等执行依赖，但先确认无当前引用；保留公共评分、持久化、签名校验和 assurance |
| `src/rlar_harness/evaluation/suite.py` | 结构/来源准入与 objective checker 准入同文件并存 | 只保留结构、来源绑定、引用、覆盖和关系一致性检查；不得把冻结标签当作已证明真值 |
| `src/rlar_harness/evaluation/standalone.py:61` | 独立 audit 也定义了 objective checker 回调，与合成端承担重复旧职责 | 删除该回调；score/audit 复用当前评分和 verifier 路径，保留独立证据运行及 audit 不回流训练的规则 |
| `src/rlar_harness/tools/dispatcher.py` | 13 处分流涉及准入、执行计费、报告复用、finalizer | 保留唯一当前准入和 finalizer；完整保留 suite、reward、verifier 配置和证据绑定；去除旧静态通过/未验证提交路径 |
| `src/rlar_harness/tools/resources.py` | 资源先按 scalar ABI 构造，再追加当前结构化 ABI 资源；默认参数仍为 v1 | 从当前 ABI 和依赖契约一次生成资源；移除旧 checker 模板，保持模型自行实现评分的提示 |
| `src/rlar_harness/runtime/worker.py` | `ScoringContext` 同时容纳 checker、旧 RM 和原始 LLM utility；当前受限 context 仍包装它 | 当前 context 直接提供所需能力，verifiable 无 API、rubric 仅 judge spec 和 raw utility；删除 checker 实例化及旧 RM 方法 |
| `src/rlar_harness/runtime/policy.py:11` | 当前依赖检查仍从 `compat/legacy_checkers.py` 导入 `ForbiddenAPI` | 将公共异常移至中立模块，再删 checker 模块；保持异常身份、错误码和捕获后仍判违规的行为 |
| `src/rlar_harness/runtime/aggregate.py` | scalar、bool opt-in 和结构化返回共用分支 | 当前仅接收结构化返回；保留有限数值、唯一归一化、有效零、partial mask、全失败 null 和等权聚合 |
| `src/rlar_harness/runtime/broker.py`、`compat/legacy_checkers.py` | 旧 RM 评分和任务 checker 的执行实现 | 完成依赖拆除与测试迁移后删除；不迁入另一套可运行 legacy 引擎 |
| `src/rlar_harness/cli.py:56,93` | `score`、`audit` 均有新旧执行实现 | 只保留当前 `evaluate_frozen()`；旧可执行输入在产生请求前给出明确不支持信息；旧读取操作单独解析 |
| `src/rlar_harness/trace/export.py:128,225` | 旧单 controller SFT 导出和 `_export_sft_v2()` 并存 | 当前入口使用 `export_sft()`，必要内部函数按职责命名；历史解码单独处理，JSONL 写入、截断、来源字段等公共逻辑合并 |
| `src/rlar_harness/trace/report.py` | 按版本形成报告，缺 actor 时默认 legacy controller | 保留历史显示语义；当前报告按四角色统计，版本判断集中在读取边界 |
| `tests/conftest.py`、`tests/test_v2.py` | 全局 fixture 默认初始流程；`test_v2.py` 的 `v2` fixture 实际使用 `offline_v2_legacy.yaml` | 当前测试默认最新配置；将 UAT 功能测试迁移至无 checker fixtures；少量历史读取/hash 检查移至 `tests/compat/` |
| `examples/make_self_contained_fixtures.py` | 最新生成器读取 `examples/v2/task_packs/math_v2.json` 和 `configs/offline_v2_legacy.yaml`；还会写入真实服务配置 | 先消除旧输入依赖；合并为一个当前 fixture 生成器，只写显式指定的 fixture 输出，不隐式改写真实服务配置 |
| `examples/make_fixtures.py`、`make_v2_fixtures.py`、`make_self_contained_fixtures.py` | 三代生成链叠加 | 当前 task pack、配置和 fixture 从独立当前定义生成；移除旧生成链，保留最少静态历史样本 |
| `pyproject.toml`、`src/rlar_harness/__init__.py`、`driver.py` | 软件版本分别写为 `0.1.0`，manifest 另有硬编码；描述仍为 offline P0 | 使用一个软件版本来源，manifest 读取它；软件发行号与 schema/ABI 版本分别管理，本整理不以名称为由随意升到 2.0 |

上述分支、默认值和依赖来自源码静态检查，不代表已复现运行故障。删减前仍需按阶段核对调用方。

### 2.2 不能当作重复代码删除的内容

- Chat Completions 与 Responses 是两种仍在使用的 HTTP 协议。当前 Responses 继承 Chat adapter 的传输设施；可按职责提取共享 HTTP 基类，但必须保留两套请求和响应转换。
- `semantic_verifier` 与 `rule_baseline` 是当前实验的显式后端选择；保留两者的行为和配置绑定，不将其当成新旧版本二选一。
- Verifiable 与 rubric、pointwise 与 ranking、required 与 diagnostic 均是当前功能，不做合并消失处理。
- 双合成角色的独立历史和成功筛选条件必须保留，不能为了共用导出代码而拼接会话。
- `evaluation/audit.py` 还提供主 driver 使用的 audit 信息隔离检查；即使删除旧 `Auditor`，也要保留或迁移这些有效公共能力。
- 基础存储、预算、持久化 LLM、runner、聚合器和 finalizer 已有共用实现，优先在现有模块收敛，不新建一个平行框架。

## 3. 统一后的命名与配置约定

### 3.1 Python 和日常入口

| 现有名称 | 目标名称 / 动作 |
|---|---|
| `V2Config` | `SynthesisConfig` |
| 业务代码 `config.v2` | `config.synthesis` |
| `config.is_v2` | 当前执行链删除；输入兼容边界显式判定可读/可执行契约 |
| `TrustedEvaluator.validate_v2()` | 合并为 `TrustedEvaluator.validate()` |
| `_export_sft_v2()` | 收敛到当前 `export_sft()`；必要内部实现用角色、格式等职责命名 |
| `llm/http_chat_json_v1.py` / `HttpChatJsonV1` | `llm/http_chat_json.py` / `HttpChatJsonAdapter` |
| `llm/http_responses_json_v1.py` / `HttpResponsesJsonV1` | `llm/http_responses_json.py` / `HttpResponsesJsonAdapter` |
| `configs/offline_v2.yaml` | `configs/offline.yaml` |
| `configs/aihub_v2.yaml` | `configs/aihub.yaml`，指定模型及路由原样保留 |
| `configs/real_service.template.yaml` | 改为当前四角色配置模板 |
| `scripts/offline_v2_demo.py` | 替换旧 demo 后成为唯一 `scripts/offline_demo.py` |
| `examples/self_contained/` | 作为当前唯一离线示例保留；语义名称无需强制改动 |
| 三个 fixture 生成器 | 合并成唯一当前 `examples/make_fixtures.py` |
| `tests/test_v2.py` | 按行为拆入合成、suite、verifier 等测试；避免形成另一份全流程测试副本 |

重命名同时更新 imports、测试引用、脚本命令、README、生成器和锁文件中确有变化的项目元数据。每个入口只保留一个实现，不长期维护旧名包装函数。旧命令在迁移说明中提供映射。

### 3.2 持久化标识保持真实含义

| 标识类别 | 处理原则 |
|---|---|
| `rlar.reward.v2`、`rlar.taskpack.v2`、`rlar.suite.v2` 等 | 当前契约仍使用其真实版本；不可因为去掉函数后缀就删掉协议版本 |
| `self_contained_v1` | 是当前评分策略的第一版，继续保留；不改成 `v2` |
| `agg.v1`、`canonical.v1`、event/checkpoint/query/tool 等 v1 标识 | 若语义未变则保持原值；它们不等同于旧框架 |
| `http_chat_json_v1`、`http_responses_json_v1` | adapter 的 wire ID 和 trace 身份保持原值，Python 文件与类名可去后缀 |
| `verifier_prompt_version`、模型 revision | 仅在内容确有变化时更新；未知模型 revision 仍为 null |
| 历史 task/profile ID、runtime fingerprint、schema 文件 | 保留历史真实标识；改写会影响引用、hash 或证据绑定 |
| `controller_steps`、`controller_logical_calls` 等账本字段 | 本次不顺带更改已落盘字段含义；当前用户报告展示角色统计，历史解析保持原语义 |
| `status=success` 与展示层 `outcome=done` | 保留既有映射，不新增第二个可写成功终态 |

当前 Python 模型与旧读取模型分开。当前 schema 必须准确约束嵌套 ABI、组件种类、能力引用和评分策略；历史 JSON Schema 保持不覆盖，以继续解释既有对象。仅因收窄新运行准入范围，不重写旧数据为新版本。

若实施中确实需要改变已有 wire 契约，而不仅是内部重命名，必须单独记录字段迁移和协议修订，不能悄悄在相同标识下改变旧对象解释。

### 3.3 `v2` 配置字段的兼容方案

为同时实现内部名称清晰和历史 digest 不漂移，配置入口与 manifest 序列化分别处理：

1. 当前 Python 对象使用 `.synthesis`，当前用户 YAML 使用 `synthesis:`。
2. 配置加载边界仍接受已有 YAML 的 `v2:`，映射为同一个内部字段；同时出现 `v2` 和 `synthesis` 时明确报错，不猜测优先级。
3. 既有 `rlar.config.v2` 的规范化持久化形式暂保留 `v2` wire key。manifest、digest、schema 导出和配置反序列化统一使用同一序列化规则，不能有的按别名、有的按 Python 字段名。
4. README 解释这一个 wire 兼容字段；历史 manifest 使用专用读取路径，不经新配置默认值填充后重写。
5. 新旧配置别名解析后，显式比较规范化结果及 digest；当前用户配置的别名输入规则和 manifest JSON Schema 的规范形态分别验证。

此处保留的是一个字段映射，不是旧运行算法。旧 `rlar.config.v1`、`legacy_checkers_v1` 或历史缺失评分策略的可执行输入，不自动转换成自实现评分；若缺少可信迁移依据，应拒绝执行并给出原环境复现说明。

## 4. 历史兼容和删除边界

建议只保留一个小型 `compat/` 读取层，负责旧 schema/manifest、reward、结果和报告的解释及原始轨迹视图。它可以使用当前共用的存储工具，但不得由当前执行核心反向导入旧 checker、旧 broker 或旧验收逻辑。

| 操作 | 当前运行 | 历史运行 |
|---|---|---|
| `construct` | 仅最新契约 | 拒绝旧可执行配置，指向迁移说明 |
| `resume` | 保留输入、资源、源码、依赖、deadline 和预算校验 | 不隐式升级；执行复现使用原代码与环境 |
| `report`、`export-llm-calls`、观察 `replay` | 支持 | 支持，不发新请求，不改原始记录 |
| `export-sft` | 保持分角色和各自成功筛选 | 默认排除旧 reward；显式导出保留旧身份和来源，不标为当前训练数据 |
| `score`、`audit` | 使用当前冻结产物，新建证据运行 | 不重新执行旧 checker/scalar reward；原环境负责历史复现 |
| 旧 reward 库检索和提交 | 当前策略严格过滤 | 不通过改 schema 或补字段伪装为最新 reward |

特别注意：`driver.resource_fingerprints()` 当前把源码文件路径和内容纳入指纹，**普通重命名也会使之前的运行无法直接在新代码下恢复**。这属于既有保护，应保持；不得去掉指纹校验、修改 manifest 或覆盖旧输入来让恢复“通过”。新代码上的恢复验收，应使用重构完成后新建的运行。

现有工作区已有较多未提交修改。后续实施不得 reset、覆盖或擅自提交这些修改。P0 先保存源文件清单、diff、文件摘要，以及明确排除凭据、数据和历史运行的必要源码快照；仅 Git HEAD 不足以重现当前基线。

## 5. 执行顺序与阶段出口

依赖顺序：**基线固定 → 配置/数据边界 → 当前执行链收敛 → 历史读取与导出 → 示例/测试迁移 → 旧文件删除及文档收口**。每阶段有独立可检查差异；出现失败先修复本阶段，不依靠跳过测试完成删减。

### P0：固定当前基线和影响范围

- 记录已有工作区变更，确认当前最新版文件与未跟踪实现均纳入基线。
- 建立符号和引用清单，包括 `is_v2`、`validate_v2`、旧 broker/checker、fixture 生成器、旧脚本和配置路径。
- 运行一次当前全量工程回归，记录真实结果；[V2_DELIVERY.md](codebase/reward_harness/V2_DELIVERY.md) 记载的 158 passed 仅为之前的证据，不当成本次实测。
- 选取最小历史样本，固定旧 reward key、配置 digest、原始导出、签名验证及观察回放的期望值。不要复制整套历史 runs。
- 对会被删除的测试建立映射：当前能力 → 迁移后测试；纯旧执行语义 → 历史读取检查或退休说明。

**出口**：基线来源明确，已有失败与重构失败可区分；需要保持的历史证据已列明。

### P1：收敛配置、模型和公共依赖

- 引入当前配置/数据模型及历史读取边界，统一 `SynthesisConfig` 命名和序列化入口。
- 当前 `models + roles` 成为唯一模型配置；清理 `model`、`rm.catalogue`、旧 acceptance override 的当前执行依赖。
- 审核 `candidate_selector/selector_metric` 等旧字段在当前 `on_pass` 模式下的实际调用；先迁移有效选择策略再删除无效配置，不能因默认提交场景没走到就判定死代码。
- 将 `ForbiddenAPI` 等被当前代码使用的异常移出 checker 模块，保持结构化错误契约。
- 当前模型默认值与 JSON Schema 的嵌套约束对齐；旧 schema 文件冻结。

**出口**：最新配置有效，旧执行配置明确拒绝；别名、持久化、digest 与嵌套 schema 验证通过。

### P2：收敛执行链

- 简化 driver、episode、dispatcher 和 resources，消除主执行链的版本分流。
- `validate_v2()` 合并为唯一 `validate()`；保留语义和规则基线后端选择。
- 移除合成和 audit 两端的 objective checker 回调。
- 当前 worker 直接建立受限 context，删除旧评分 API；聚合只处理结构化 ABI。
- 保持单一 durable LLM、runner、聚合器和 finalizer，保留故障分类、unknown 用量、重试 owner 与预算持久化。
- HTTP adapter 文件和类改为职责名称；共享传输代码只在存在实际耦合时提取，保持 wire ID、请求体与原始响应留档。

**出口**：最新离线双合成全流程、rubric、verifier、score/audit 均工作；无 checker 执行，无重复预算扣费或漏记。

### P3：收敛历史读取、报告和导出

- 旧对象解析集中至兼容边界，避免当前 `RewardDefinition` 默认值影响旧 hash。
- 当前 SFT 导出按角色和格式复用公共代码；历史轨迹仅做数据解码，不经过旧执行引擎。
- 保持 test case 角色按 suite 准入筛选，reward 角色按开发成功筛选，工具模型 targets 为 0。
- score/audit CLI 只调用当前持久化评估路径；不修改原 construction trace。

**出口**：历史报告、请求导出和回放可读；新旧导出身份清楚；回放新增请求为 0。

### P4：先迁移示例与测试，再删除旧实现

- 先将当前生成器改为不依赖旧 YAML/task pack，消除生成器对真实服务配置的隐式写入。
- 更新离线和真实服务配置、demo、recovery 脚本及测试路径。真实配置继续使用 `gpt-6-sol / deepseek-flash / gpt-4.1` 及既定路由。
- `test_v2.py` 的当前能力覆盖迁至无 checker fixtures；保留通用存储、聚合、预算、恢复等测试，不能整文件删除旧文件名对应的有效覆盖。
- 删除不再被当前功能或历史读取使用的 broker、checker、旧 controller/scalar 执行逻辑和三代重复生成代码。
- 删除旧 demo 和旧可执行配置；保留的历史样本集中到 `tests/compat/fixtures/`，明确不可作为新构造入口。
- 对已迁移功能运行相关测试；最终执行一次完整工程回归。

**出口**：旧可执行路径真实减少；当前测试独立运行不依赖旧 checker/config；残留的版本引用均可解释。

### P5：文档与交付收口

- README 只提供当前安装、运行、评分、audit、回放和导出入口。
- 将最新双合成需求与评分逻辑修订整合成一份当前需求主文档，更新设计、LLM/trace/reliability 文档中的有效约定和入口引用。
- 旧 PRD、`DELIVERY.md`、`V2_DELIVERY.md` 标为历史基线或证据记录，保留原日期、结果和证据链接；不把旧失败改写为成功。
- 提供旧→新符号/路径映射、删除清单、历史兼容矩阵、测试覆盖映射及最终验证报告。

**出口**：文档没有两个默认框架，代码没有两套可执行版本；已知真实服务和模型质量限制仍如实记录。

## 6. 验收标准

| 编号 | 验收内容 | 通过依据 |
|---|---|---|
| U-01 | 主代码命名统一 | 当前执行链没有 `V2Config`、`validate_v2`、`_export_sft_v2` 或 `.is_v2`；协议版本残留有明确归属 |
| U-02 | 单一配置和 schema 权威 | 当前配置别名产生同一规范化结果；冲突字段被拒绝；嵌套 ABI/组件/能力/策略与模型一致；旧 schema 不被覆盖 |
| U-03 | 当前流程独立 | 删除旧 checker/broker 和旧生成器后，当前配置、示例生成、construct/score/audit 不存在导入或文件依赖 |
| U-04 | 评分逻辑归属 | GSM8K 格式和数值行为由源码完成；直接调用、别名、内部导入、反射等违规路径均返回明确错误；不能捕获后伪装有效 0 |
| U-05 | 聚合与验收正确 | 正常 0、partial、全失败 null、唯一归一化、等权均值保持；required false/缺证据阻止提交，diagnostic 不可用按原规则留档 |
| U-06 | Rubric 和 HTTP 协议保持 | raw utility 返回原始文本，源码负责解析；两种 HTTP 协议、错误/截断/拒绝、token 映射和物理重试留档通过已有适用测试 |
| U-07 | 预算与恢复保持 | 冻结、模型响应、verifier 决策和结果提交等故障点恢复不重复提交、不重置预算；完整多轮历史和 unknown attempt 保留 |
| U-08 | 历史证据保持 | 固定旧样本的 reward key、配置 digest、签名验证和原始请求导出不漂移；回放 0 新请求；原始历史文件不改写 |
| U-09 | 训练导出保持 | 两个合成角色分别导出；工具 targets 为 0；suite 成功/reward 失败的筛选正确；旧 reward 默认排除 |
| U-10 | 独立 audit 保持 | 使用独立输入和新证据运行，不反馈开发修订、不改变 SFT 筛选；subprocess 不被标记为正式隔离 |
| U-11 | 测试和删除可核对 | 旧 AT/UAT/SC 检查有迁移或退休依据；不能用减少用例、skip 或只看总 passed 数掩盖覆盖丢失 |
| U-12 | 入口和文档一致 | 唯一 demo、唯一当前 fixture 生成链；新配置模型标识不变；迁移映射、删除清单和验证报告齐全 |

采用现有测试并补齐本次重构特有的必要行为检查：历史序列化、配置别名冲突、当前 schema 嵌套约束、旧代码删除后的依赖闭合。纯文件改名不逐项新增测试。

真实服务联调与工程重构验收分开记录。本次整理不自动启动付费模型实验，也不把 scripted fixtures 记为真实服务或模型质量通过。此前自实现评分真实 GSM8K 完整链路仍受服务故障影响；整理代码不会自动解决该项，也不能据此宣布通过。

## 7. 运行产物、风险和回退

所有后续基线、测试和调试产物放入项目规定的 `codebase/reward_harness/runs/YYYY_MM_DD_HH_MM_{TaskName}/`，时间使用 Asia/Shanghai。每个独立运行新建目录，同分钟重名时更换符合规则的 PascalCase 任务名；不覆盖现有目录。

以下保留方案中的基线命令示例；实际已执行的命令、基线与最终结果见交付证据。命令在 `codebase/reward_harness/` 下运行；目录必须全新，已存在则停止并另取任务名：

```bash
run_dir="runs/$(TZ=Asia/Shanghai date +%Y_%m_%d_%H_%M)_HarnessUnificationBaseline"
mkdir "$run_dir" || exit 1
.venv/bin/python -m pytest tests --basetemp="$run_dir/pytest" \
  --acceptance-output "$run_dir/acceptance.json" > "$run_dir/pytest.log" 2>&1
```

重构完成后的检查使用新的 `HarnessUnificationVerification` 运行目录。拥有独立 manifest、预算或恢复状态的 demo/score/audit 子运行也遵循时间和 PascalCase 命名规则；`exports/`、`pytest/` 等普通产物子目录不算独立运行。

| 风险 | 处理与回退原则 |
|---|---|
| 已有未提交修改被覆盖 | 先固定工作区基线，按文件和差异核对，只撤销本次引入的修改；不使用破坏性 reset |
| 改默认值/别名破坏 hash 或 HMAC | 当前和历史解码分开；固定样本比对失败即暂停该阶段，不改旧产物适配新代码 |
| 删除旧文件切断最新生成器/测试依赖 | 先迁移生成器与 fixtures，再按引用清单删除；当前套件必须在不加载旧执行模块时通过 |
| 改名导致旧 run 无法恢复 | 保留源码指纹保护；旧运行在原代码/依赖环境复现；新环境新建运行 |
| 测试改写掩盖功能删除 | 按能力映射审核，尤其预算、SIGKILL 恢复、finalizer、Responses 和数据导出；退休理由单独记录 |
| 为消除版本号误删当前能力 | 协议 ID、策略 ID、模型 revision 与 Python 名称分开盘点，按第 3.2 节保留 |

最终交付应包含：当前单一实现、最小历史读取兼容层、统一配置/示例/文档、删除与重命名清单，以及存放在规范运行目录中的可复核验证证据。

## 8. 执行结果（2026-09-30）

P0–P5 均已完成。统一后的需求入口为 [当前 PRD](PRD_REWARD_HARNESS_CURRENT.md)，实现、历史边界、重命名与删除、覆盖迁移、验证证据见 [交付记录](codebase/reward_harness/UNIFICATION_DELIVERY.md)。基线 158 passed，最终 164 passed；U-01–U-12 的证据映射见 [验证摘要](codebase/reward_harness/runs/2026_09_30_16_49_HarnessUnificationVerification/summary.json)。未执行真实服务或正式隔离验收，未提交 Git。
