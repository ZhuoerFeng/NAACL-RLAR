# Reward Harness 当前统一需求

日期：2026-09-30。状态：当前权威需求，合并双合成修订、评分逻辑归属修订和版本统一决策。工程实现及边界见 [交付记录](codebase/reward_harness/UNIFICATION_DELIVERY.md)，操作入口见 [README](codebase/reward_harness/README.md)。

本文替代早期 PRD 中冲突的单 controller、预置 DevSuite、scalar ABI、原生 checker、总分门槛和 unvalidated 发布路径。旧 PRD 与交付记录保留为需求演进和实验历史。运行目录、训练源只读、模型标识、凭据和真实调用留档仍遵守 [AGENTS.md](AGENTS.md)。

## 1. 目标与职责

每条 query 由 test case synthesizer 生成测例，harness 做结构与来源准入并冻结，再由 reward synthesizer 生成可保存、可重放执行的完整 Python 源码。组件执行后由 harness-verifier 检查测例意图和实际评分行为，唯一 finalizer 决定是否可提交。两类 synthesizer 可以使用同一模型，必须保持独立角色和历史。

| 角色/模块 | 输入、职责与输出 |
|---|---|
| Test case synthesizer | 固定任务、query、许可的原始 reference → 唯一候选表、能力级 pointwise 测例和显式 ranking 关系 |
| Reward synthesizer | 固定任务、冻结 suite、ABI 和开发反馈 → 评价准则、完整源码、依赖和 rubric judge_spec |
| Harness | 结构/来源准入、受限执行、归一化、等权聚合、预算、持久化、报告绑定及提交 |
| Harness-verifier | 原始任务/参考、冻结意图、实际分项数值/反馈 → 绑定证据的二分类决策或无法判断状态 |
| Rubric judge | 只返回原始模型回复；Python 源码负责请求组装、解析、数值校验与 raw_score 提取 |

答案提取、格式校验、比较和评分核心必须包含在生成源码内；可在同一源码中定义辅助函数。不得调用或导入任务级 checker，也不得通过改名或通用代理取得同等能力。原始 response/reference 不由 harness 预先提取正确答案或注入标签。

## 2. 唯一当前数据契约

当前可执行配置为 `rlar.config.v2`，只使用 `models + roles`。用户配置字段 `synthesis` 映射到内部 `SynthesisConfig`；历史 `v2` 别名可读但不可与其并用。规范化持久化保留 `v2` wire key。可执行 reward/task pack 必须声明当前结构化 ABI 和 `self_contained_v1`，不自动升级缺字段的历史输入。

`RewardDefinition` 同时支持 single 与 checklist。组件声明 `kind=verifiable|rubric`、能力引用、criterion、source、normalization、required_apis；rubric 另有唯一权威 `judge_spec`。single 为单组件 identity，checklist 对成功组件等权平均；重复源码不能增加权重。不强制组件与能力一一对应。

ABI 返回 `{raw_score, feedback, evidence}`。数值必须有限，只归一化一次；合法 0 参与均值。异常保留独立 error 和 mask，不填 0；部分成功为 partial，全部失败为 null/failed。Verifiable 无 context API；rubric 仅可用 `judge_spec` 和 `call_llm_api`，每组件每候选至多一次逻辑 judge 请求。

通用依赖由版本化白名单控制，当前为 `re/json/math/decimal/fractions` 的许可成员。准入和 worker 同时检查内部导入、动态执行、反射及私有属性访问；捕获违规异常不能伪装成有效 0。这是行为原型约束，不是正式 OS 隔离证明。

软件版本、schema 版本、ABI、评分策略、聚合版本和 adapter wire ID 分别管理。保留 `self_contained_v1`、`agg.v1`、`canonical.v1`、`http_chat_json_v1`、`http_responses_json_v1`，不因业务函数去版本后缀而改写历史身份。

## 3. 测例、验收与提交

Suite 包含唯一候选表、能力范围、required/diagnostic、pointwise 的 pass/fail 意图及 ranking 关系。Task pack 预先固定来源等级、类别/数量、pairwise/triplet 和能力描述。Pairwise 为两个候选一条关系，triplet 为三个候选三条关系；合并等价类后不得出现严格偏好环。

准入只核对结构、引用、覆盖、来源绑定和关系一致性，明确标为 `structure_and_provenance_only`。模型标签为 `model_inferred`，人工标签需要外部固定证据内容绑定；拒绝 `objective_verified` 和 objective checker。冻结不会证明标签正确。冻结 suite、标签、required 和 digest 后才启动 reward 合成，reward 不能修改它们。

Verifier 先核对测例意图是否由原任务与许可参考支持，再判断分项数值行为。Fail 必须有相关能力的实际惩罚，pass 不能无差别惩罚，ranking 按声明范围比较；只写反馈而数值不变不能自动通过。不使用旧总分 FAR/FRR 阈值代替能力级判断。

每个决策绑定 reward、suite、执行证据、case、模型配置和 prompt 版本，并核验引用。有效 false 进入源码修订，不对同一证据反复抽裁判。缺少 required 所需评分证据是非 completed，不能记为拦截成功；无关组件 partial 不自动阻断能力级验收。格式错误有限修复。

唯一 finalizer 核验报告完整性标签、全部绑定及 required 决策。`on_pass=submit` 自动提交与 `submit_reward` 使用同一检查；`on_pass=return` 保留显式选择。多个合格候选按配置 metric、reward key 稳定排序；选择策略不是额外验收门槛。没有合格候选则失败，不发布 unvalidated reward。`status=success` 仅在展示映射为 `outcome=done`。

## 4. 预算、恢复与复用

Test/reward 合成、模型传输、verifier 格式修复默认各至多 5 次（含首次）；连续读工具还受 `max_reward_decisions` 约束。Run 和 episode 显式固定有限请求/token/工具/组件/测例预算及 deadline，所有角色共用账本。历史 `controller_steps` 字段保留总合成决策计数语义，角色用量另行报告。

每次派发前持久化不可变请求和预算预留，每次响应、工具观察、冻结和决策完成后落盘。恢复保留 suite、独立历史、已确认结果、预算和原 deadline；unknown attempt 保留，不假设请求免费。鉴权、存储或内部故障停止外层，临时服务故障按单一 owner 有限重试，连续故障触发熔断。

Results 是提交权威；library 先落盘的孤立条目不可见，checkpoint 为派生缓存。复用先筛任务/输入/模式/运行时适用性，再由同一 finalizer 核验当前 suite 与 verifier 配置；文本相似或旧 passed 不足以授权复用。代码可候选复用，不复用无效旧证据。

恢复校验输入、源码路径与内容、资源和依赖指纹。任何变化包括本次重命名均可能使既有运行无法直接恢复；旧实验用原代码/环境复现，不绕过校验。详见 [可靠性](HARNESS_RELIABILITY.md)。

## 5. 调用、导出与实验边界

四角色统一使用 durable LLM 边界，分别记录 role、逻辑请求、物理 attempt、完整请求/响应/观察、模型/revision、adapter、hash、token/unknown、失败原因和耗时。Chat Completions 与 Responses 是两种协议，不是两个框架；Python 类名无版本后缀，wire ID 保持不变。凭据只运行时注入。

合成历史只追加，重试不重写请求，独立工具批次按原顺序返回并完成 barrier。反馈消融同时过滤 observation 和报告资源；完整 verifier 证据不受影响。Verifier backend 明确选择 semantic_verifier 或 rule_baseline，后者只用于有独立标签依据的数值规则，不叠加两套裁判。详见 [LLM 协议](HARNESS_LLM_PROTOCOL.md)。

训练导出按角色：test case synthesizer 以 suite 准入成功筛选，reward synthesizer 以开发 done 筛选。工具角色 targets 为 0；同模型也不得混淆角色，full_trace 不拼接两个角色。保留合规失败尝试和修复因果，未来 observation 不能进入先前 prompt；audit 不影响训练筛选。按预先任务家族划分 run/split，不随机拆 call。详见 [Trace 协议](HARNESS_TRACE_SPEC.md)。

独立 score/audit 使用冻结源码与相同评分底座，写全新证据运行，不修改 construction trace。Audit 用外部独立 SuiteDraft、完整类别与来源准入，结果不反馈开发修订。Subprocess 正式 audit 拒绝；显式 `--allow-prototype-audit` 仅产出原型证据。

## 6. 兼容和质量验收

历史 schema、原始记录、hash、报告标签和模型身份保留原样。只读层支持 report、原始请求导出及观察 replay；SFT 默认排除旧 reward，显式 include-legacy 保留历史身份。旧单 controller、scalar、原生 checker 和 RM broker 退出全部执行入口；历史读取不经过当前模型默认值填充。

工程验收沿用 UAT-01–28、SC-01–05 和适用的基础 AT，并增加配置别名、嵌套 schema、历史字节/hash 与依赖闭合检查。被替代要求及测试迁移见 [交付映射](codebase/reward_harness/UNIFICATION_DELIVERY.md)。完整原始 UAT 表保留于 [历史双合成需求第 13 节](PRD_REWARD_HARNESS_UPDATE.md#13-v2-验收矩阵)，当前来源规则以本文为准。

GSM8K 评分源码需要覆盖标记、合法数字、等价形式、错误答案和格式边界，并以不同题目的独立样本检查查表/记忆问题。人工实现和 scripted verifier 只证明工程分支。真实模型自主合成、verifier 质量和任务泛化需单独真实评估；此前服务故障及未通过项不会因整理代码变为通过。Subprocess 仍为同用户原型，未提供正式文件/网络/密钥隔离。本次不启动付费模型实验、训练或部署。
