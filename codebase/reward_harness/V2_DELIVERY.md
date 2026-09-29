# Reward harness v2 增量交付

实现依据：[PRD_REWARD_HARNESS_UPDATE.md](../../PRD_REWARD_HARNESS_UPDATE.md)。2026-09-29；保留现有执行器、聚合器、finalizer、durable LLM、budget/journal/blob/result commit，没有引入通用多 agent 框架。没有修改训练源文件或执行训练。

## 实现范围

| 目标 | 实现与边界 |
|---|---|
| D1 协议 | v2 capability / component / SuiteDraft / FrozenSuite / ValidationDecision / actor metadata；保留 v1 hash、scalar ABI、历史报告签名和原始 trace 语义；新增 10 份 v2 JSON schema，历史 schema 不覆盖。 |
| D2 两角色 | 测例角色独立只追加历史；准入成功及冻结 checkpoint 后启动 reward 角色。无效 suite 有界修订；缺 suite 终止 failed，不产生 unvalidated。 |
| D3 执行 | 同一 worker/聚合器处理 verifiable 与 rubric；结构化 raw_score/feedback/evidence；正常 0、异常、partial mean 分开。Judge utility 返回原始文本，Python 解析；一次逻辑采样，全部物理重试留档/计费。 |
| D4 验收 | 单一 semantic/rule backend；按能力范围检查实际证据，不叠加旧全局总分门槛。校验引用、配置及版本；required 决定提交，diagnostic 完整保留。相同定义/suite/verifier 配置的判断复用，不能重抽有效 false。 |
| D5 恢复 | suite、角色历史、执行证据、verifier 决策可恢复；冻结、模型响应、判断落盘、result commit 的 SIGKILL 测试通过。未确认的执行重试仍扣组件预算；unknown 模型 attempt 保留请求和预留。 |
| D6 导出 | 两个角色独立 per_call/full_trace；reward-only final_program_only；judge/verifier target 为 0。Suite 成功而 reward 失败仍可导出测例角色。语言反馈单独导出；观察回放不发请求。 |
| D7 实验 | pairwise/triplet、类别移除、语言反馈移除、单一 verifier backend；报告角色成本/unknown、案例关系数及能力覆盖。独立 score/audit 写新运行，audit 保持三类覆盖且不影响原训练筛选。 |

工程验收使用实际 Python 执行和明确标注的模型 fixtures。Semantic backend 的接口/分支测试**不证明真实小模型判断质量**。实际 rubric/verifier 服务、独立标注的 verifier 判断集和任务质量阈值仍需分别验证。

## 初次交付证据

最终结果：**107 passed，0 failed，0 skipped（45.58s）**；UAT-01–UAT-28 均有通过的工程检查。最终 demo `done`，四角色共 7 次 fixture 请求；观察回放新增请求 0，恢复新增结果/模型请求均为 0，原结果字节不变。

- 全量验收：[final_acceptance.json](runs/2026_09_29_01_28_HarnessV2Acceptance/final_acceptance.json)，对应 [pytest 日志](runs/2026_09_29_01_28_HarnessV2Acceptance/final_pytest.log)。包含历史 AT 与独立 UAT-01–UAT-28 的测试映射；scripted 工程测试不计为真实服务通过。
- 最终 demo 位置与摘要写入 [verification_summary.json](runs/2026_09_29_01_28_HarnessV2Acceptance/verification_summary.json)。目录包含完整请求导出、两类合成 SFT 视图、反馈视图、角色成本报告和观察回放。
- 真实服务检查：[real_service_preflight.json](runs/2026_09_29_01_28_HarnessV2Acceptance/real_service_preflight.json)。本次指定配置为 [aihub_v2.yaml](configs/aihub_v2.yaml)：base `gpt-6-sol`、harness-verifier `deepseek-flash`、rubric judge `gpt-4.1`，沿用项目已有 AIHub endpoint。当前环境未注入 `RLAR_AIHUB_API_KEY`，故真实请求未派发；没有替换模型。三个 revision 均如实保留 unknown/null。

运行证据都在 `codebase/reward_harness/runs/`，不覆盖历史。最终 demo manifest 记录 fixture 输入指纹、输入位置、配置、seed、绝对 deadline 和依赖指纹。此次输入为自建工程 fixtures，未抽样训练集。凭据不写入配置、trace 或版本库。

## v1 标准迁移

旧 [PRD 验收矩阵](../../PRD_REWARD_HARNESS.md) 已标记受影响条目 superseded；保留旧测试验证只读历史兼容，不将其通过作为 v2 功能证据。

| v1 历史条目 | v2 标准 |
|---|---|
| AT-09：总分指标验收 | UAT-06/07/08：能力级数值行为及语义判断 |
| AT-10：缺 suite 可 unvalidated | UAT-02/12/13：先冻结、按所需证据判断、缺 suite failed |
| AT-11：仅原 runtime/task 的复用 | UAT-19：额外绑定 suite、verifier/judge 配置 |
| AT-12：测试无模型调用 | UAT-01/20/25：两个合成角色及显式工具模型完整留账 |
| AT-28：直接返回数值的 RM | UAT-04/26：原始 judge 回复 → 组件解析 → 唯一归一化 |
| AT-30/35：单 controller 的 assistant 监督 | UAT-20–23：角色过滤及各自成功标准，工具 target 为 0 |

## 2026-09-29 修复与真实服务复核

- 修复 diagnostic verifier 传输失败错误阻断整个 episode：仅将非 required 案例的 `environment_unavailable` 转为留档的 `operation_status=error / passed=null`，required 失败以及预算、deadline、存储等其他异常继续按原规则处理。
- 新增 4 项回归，覆盖诊断项/必测项的 503 与结果未知，以及诊断项先于必测项、完整导出、恢复不重复请求。全量 **111 passed，0 failed，0 skipped（47.19s）**，见 [pytest 日志](runs/2026_09_29_16_53_DiagnosticVerifierFix/full_pytest.log) 和 [验收矩阵](runs/2026_09_29_16_53_DiagnosticVerifierFix/full_acceptance.json)。
- [真实双合成联调](runs/2026_09_29_16_55_AiHubV2Integration/integration_summary.json)：一条数学工程输入，`gpt-6-sol` 合成 suite 和 verifiable reward，`deepseek-flash` 验收，结果 `success/validated`。9 次真实物理请求，含一次 HTTP 500 后成功重试；失败 attempt 的未知费用如实保留。观察回放和恢复均新增请求 0，结果字节不变，两类合成训练视图成功导出。该次没有生成 rubric 组件，因此不作为 rubric 服务通过证据。
- [真实 rubric 补测](runs/2026_09_29_16_57_AiHubV2RubricIntegration/integration_summary.json)：使用明确标注的固定混合 reward 和实际 Python runner，向 `gpt-4.1` 发出 2 次真实请求。均收到 HTTP 401，响应正文为 `PlatformConfigError / 1030`：该模型下所有厂商均不支持标准协议，需平台检查厂商模型是否开启标准协议。**Rubric 成功联调仍未通过**；没有替换模型，也没有把该补测称为真实合成成功。
- 密钥只在这两次运行的进程环境中注入；凭据扫描未在运行产物发现密钥。模型 revision 继续保留 null。输入为工程 fixtures，不是训练集抽样或独立质量基准。

## 2026-09-29 GPT-4.1 Responses 接入

用户提供了 AIHub 的 GPT-4.1 调用样例：`/openai/v1/responses`，运行时鉴权后缀 `?provider=azure`。此前 standard Chat Completions 的 1030 错误已通过选择正确接口和路由解决，不需要替换模型。

- 新增 `http_responses_json_v1`，复用现有 HTTP 传输与错误分类、durable I/O、预算、重试 owner。支持 Responses 文本输入、文本输出、JSON 输出配置、完成/截断状态和 input/output/cached token 用量；不启用远端工具。协议依据为 [Responses API reference](https://developers.openai.com/api/reference/resources/responses/methods/create/)，AIHub 路由依据为用户提供的示例。
- 持久化和导出支持真实 `input` 字段，不向请求伪造 `messages`；角色训练视图沿用实际文本会话。新增字段默认不进入历史配置序列化，已检查旧运行配置 digest 保持一致。v2 schema 已重新导出，v1 schema 未覆盖。
- 全量 **122 passed，0 failed，0 skipped（48.35s）**，见 [pytest 日志](runs/2026_09_29_17_00_ResponsesAdapterValidation/full_pytest.log) 和 [验收矩阵](runs/2026_09_29_17_00_ResponsesAdapterValidation/full_acceptance.json)。新增 11 项 Responses 检查包含四角色 HTTP fixture 的请求留档、重试预算、角色导出、恢复、拒绝、截断及错误响应。
- [真实 Responses 混合评分联调](runs/2026_09_29_17_05_AiHubV2ResponsesIntegration/integration_summary.json)：固定混合 reward，经实际 Python runner 调用 GPT-4.1 两次，正确/错误回答的 rubric 分数分别为 1/0；deepseek-flash 三次验收全部通过，finalizer 接受。共 5 次真实请求，无重试，全部 usage 可观察，观察回放新增请求 0；密钥扫描无泄漏。该测试验证固定 reward 的真实执行链路，不将其称为模型新合成的混合 reward。

## 2026-09-29 用户指定 GSM8K Natalia 样本实测

- 输入为用户提供的 Natalia 两个月售卖 clips 的原题及完整参考解答（`#### 72`），未读取或修改训练源文件，未声称核验原始数据集行号。新增 `gsm8k_numeric_v1`，从最后一个 `####` 后读取完整数值，支持有符号整数、小数和合法千分位，使用精确数值比较；不认证中间推理。新增 12 项检查，全量 **134 passed，0 failed（49.00s）**，见 [pytest 日志](runs/2026_09_29_19_45_Gsm8kFormatValidation/pytest.log)。
- [真实运行摘要](runs/2026_09_29_19_46_Gsm8kNataliaRealTest/integration_summary.json)：`gpt-6-sol` 合成并冻结 6 个候选、9 个必测案例（3 pass、3 fail、3 ranking），随后生成 verifiable + rubric 两组件。模型自行修订了 capability metadata 和组件 evidence 返回类型错误；没有手改冻结测例或合成 reward。
- **最终结果为 `failed / infrastructure_error`，未产生通过验收的 reward。** 最新执行版本对六个候选的 verifiable 分数为 `1,1,1,0,0,0`，GPT-4.1 rubric 分数为 `1,0,1,0,0,0`；`#### +72.0` 被 rubric 错误扣分，虽然实际发送的 prompt 明确允许等价有符号小数。DeepSeek 对此项仍返回 `passed=true`，同时指出误扣分并要求修复，暴露 verifier 判断一致性问题。
- 最新版本仅完成 4/9 条 verifier 判断；第 5 条 `pointwise_fail_april_only` 的首次请求及四次重试均收到 AIHub `HTTP 500 / PlatformNoAvailableAccount / 1011`（暂无可用供应商账号），必测失败按设计终止。余下 4 条未执行验证，不能写成 9/9 通过。共 28 次真实物理请求：测例合成 2、reward 合成 5、rubric 12、verifier 9（含 5 次失败 attempt）；失败 attempt 的 usage 保留 unknown。
- [按最新 evaluation 绑定的复核结果](runs/2026_09_29_19_46_Gsm8kNataliaRealTest/exports/postrun_analysis.json)与[完整代码、rubric、测例及分数](runs/2026_09_29_19_46_Gsm8kNataliaRealTest/exports/latest_executed_artifacts.md)。本次运行脚本的原始 `generated_reward.json` / `validation.json` 来自上一次完成的错误草稿：最新 evaluation 因外部故障中断，尚未更新 episode current。为保留证据未覆盖原导出，另以 `evaluation_dispatched` / `score_batch_result` 提取 `latest_*` 文件，明确其为已执行但未验收的草稿。
- 观察回放新增请求 0，完成态恢复新增结果/请求均为 0，结果字节不变，输入指纹不变，运行时密钥扫描未发现泄漏。测例角色可导出 1 条成功训练视图，reward 角色成功训练视图为 0。此次只是单样本开发测试，未构成独立 GSM8K 基准或 verifier 质量验收。

## 使用与剩余外部依赖

默认入口和命令见 [README.md](README.md)；`scripts/offline_v2_demo.py` 可一条命令重现。显式 v1 配置仅用于历史兼容。所有新配置默认 v2。

M1–M4 及 D7 离线实验入口已实现。M5 中真实双合成/verifiable 验收链，以及固定混合 reward 的真实 rubric/verifier 执行链均已通过小样本联调；三个指定模型都有成功的真实调用证据。新增 GSM8K 动态混合 reward 测试未通过，仍需解决平台供应商可用性并检查 rubric 误判、verifier 判断一致性。模型质量验收还需要独立标注判断集。Subprocess 保持 `behavioral_prototype`，不声称正式文件/网络/标签隔离。消息级监督边界已验证，tokenizer/template 级 mask 和训练不在此次交付中。
