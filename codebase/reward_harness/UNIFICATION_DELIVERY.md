# Reward Harness 版本统一交付

2026-09-30。已按用户批准的 [执行文档](../../REWARD_HARNESS_UNIFICATION_PLAN.md) 完成合并。当前唯一需求入口为 [PRD_REWARD_HARNESS_CURRENT.md](../../PRD_REWARD_HARNESS_CURRENT.md)，唯一操作入口为 [README](README.md)。

## 结果与范围

框架现在只执行 **双合成 → 结构/来源准入与冻结 suite → 自实现结构化评分 → harness-verifier → 同一 finalizer**。旧单 controller、scalar ABI、全局 FAR/FRR 提交门槛、原生 checker、RM broker 和未验证发布路径已移除。通用预算、durable LLM、runner、聚合、签名与提交保持共用。

相对开始前的工作区快照，主源码从 **9,657 行降至 8,078 行，减少 1,579 行（约 16%）**；源码文件从 50 个变为 51 个，新增文件用于中立异常、离线 fixtures 和最小历史读取层。不是把旧引擎搬入兼容目录。完整按文件/hash 的 [差异清单](runs/2026_09_30_16_49_HarnessUnificationVerification/change_inventory.md) 以工作区快照为基线，避免把用户原有改动算成本次成果。

保留当前功能：single/checklist、verifiable/rubric、pointwise/ranking、required/diagnostic、semantic/rule backend、Chat/Responses、独立 score/audit、分角色导出，以及原有预算和故障恢复能力。鉴权失败在两个合成阶段均立即停止外层请求；不再作为普通基础设施失败继续遍历。

## 名称与入口迁移

| 原名称/路径 | 当前名称/处理 |
|---|---|
| `V2Config`、`config.v2` | `SynthesisConfig`、`config.synthesis` |
| 用户 YAML `v2:` | 推荐 `synthesis:`；旧别名可读，同时出现则拒绝 |
| `.is_v2`、`validate_v2()` | 删除分流，唯一 `TrustedEvaluator.validate()` |
| `_export_sft_v2()` | 唯一 `export_sft()`，按角色/格式生成视图 |
| `HttpChatJsonV1` / `http_chat_json_v1.py` | `HttpChatJsonAdapter` / `http_chat_json.py` |
| `HttpResponsesJsonV1` / `http_responses_json_v1.py` | `HttpResponsesJsonAdapter` / `http_responses_json.py` |
| `configs/offline_v2.yaml` | `configs/offline.yaml` |
| `configs/aihub_v2.yaml` | `configs/aihub.yaml`，指定模型与路由保留 |
| `configs/real_service.template.yaml` | 当前四角色模板 |
| `scripts/offline_v2_demo.py` 和旧 offline demo | 唯一当前 `scripts/offline_demo.py` |
| 三代 fixture 生成器 | 唯一 `examples/make_fixtures.py --output-dir ...`，目录必须全新，不改真实服务配置 |
| `tests/test_v2.py` | `tests/test_synthesis.py`；公共 fixtures 改为自实现评分 |
| 报告字段 `v2_statistics` | 新生成视图使用 `synthesis_statistics`，消费者需更新 |

仅重命名 Python 实现，不重命名协议身份：`rlar.*.v2`、ABI `v2`、`self_contained_v1`、`agg.v1`、`canonical.v1`、HTTP wire ID 和历史 profile ID 保留。Canonical 配置、manifest、digest 与 JSON Schema 继续用 `v2` wire key；用户别名映射只发生在解析边界。

软件版本由 `src/rlar_harness/__init__.py` 单点提供，Hatch 构建和 manifest 读取相同值，仍为 `0.1.0`。锁文件仅移除本项目的重复固定版本声明，第三方包版本没有升级。验收输出改为 `rlar.acceptance.v2`，字段为 `historical_matrix`、`synthesis_matrix`、`self_contained_matrix`、`superseded_requirements` 和 `all_current_requirements_passed`；不再使用易误解的 `all_p0_passed`。

## 删除与兼容边界

移除旧执行模块 `runtime/broker.py`、`compat/legacy_checkers.py`，旧 DevCase/DevSuite/Store、旧 evaluator 指标门槛、worker checker/RM API、scalar/bool opt-in 聚合及重复的 library 提交判断。旧 `evaluation/checkers.py` 在开始前的用户工作区已移除，本次没有恢复它。

移除旧配置 `offline_demo.yaml`、`offline_v2_legacy.yaml`；删除旧 `examples/v2/`、`dev_suites/`、`audit_suites/`、`task_packs/` 及旧根级输入/脚本回复 fixtures。旧 HTTP 文件、配置、demo 和测试路径由上表新路径接替，三代生成器合并。逐文件删除/改名清单见差异清单；历史测试证据仅保留在 `tests/compat/fixtures/`。

| 操作 | 当前契约 | 历史契约 |
|---|---|---|
| construct / 重新执行 | 支持当前自实现结构化评分 | 明确拒绝旧配置、checker 和 scalar ABI |
| resume | 输入、资源、源码、依赖与原 deadline 一致时支持 | 使用原始源码和环境复现，不隐式升级 |
| report / 原始请求导出 / 观察 replay | 支持 | 原始序列化数据读取，不注入新默认值，不发送请求 |
| SFT | 按角色与各自成功标准筛选 | 旧 reward 默认排除；显式 include-legacy 保留历史身份 |
| score / audit | 冻结 reward，写新证据运行 | 不重新执行旧产物 |

历史 v1 schema 共 9 份保持原字节；当前 schema 从权威模型直接生成，嵌套 ABI、能力、组件种类和策略准确，不再只替换顶层版本。历史 hash、实际旧报告签名与原始导出已核验。`compat/reading.py` 不包含旧执行器。

源码路径/内容参与恢复指纹，**即便之前运行已采用 self_contained 策略，也不能在本次重构后强行 resume**。保留该保护；新代码上的恢复测试使用新建运行。原有历史运行、训练源和原始交付证据未被改写。

## 测试覆盖迁移

基线实测 **158 passed**；最终实测 **164 passed，0 failed，0 skipped（46.69s）**。数量变化不是覆盖结论，具体迁移如下。

| 原测试/能力 | 当前覆盖或退休原因 |
|---|---|
| 输入、canonical hash、聚合、mask、0/null、签名 | `test_core.py` 迁移到结构化自实现 reward，历史原始 hash/签名另见 `tests/compat/test_reading.py` |
| Controller、工具、自动/显式提交、候选排序、预算 | `test_controller.py` 保留行为检查，按角色计数、冻结 suite 和唯一 finalizer 判断 |
| 八个 SIGKILL 边界、断尾/损坏、取消/孤儿 worker | `test_recovery.py` 保留实际进程故障检查；源码不再获得文件/进程能力 |
| `test_v2.py` 当前能力 | 整体迁到 `test_synthesis.py`，使用自实现 reward 和显式工程 verifier fixtures |
| HTTP Chat/Responses、重试、未知费用、脱敏、鉴权 | `test_services.py`、`test_responses.py`，全部使用本地 transport/fixture |
| `test_rm_broker_physical_retry_shared_budget_and_worker_key_boundary`、`test_rm_actual_worker_ipc` | 旧数值 RM 接口退休；原始 rubric utility/IPC、共享预算、请求记录由 UAT-04/26、SC-02 及 HTTP 用例覆盖，不声称旧 broker 仍受支持 |
| `test_gsm8k_bundle_is_available_only_when_declared` | Checker bundle 开放接口退休；SC-02 检查该接口及别名/内部导入均不可用 |
| GSM8K 数字/格式边界 | `test_gsm8k.py` 与 SC 测试直接运行生成源码 fixture；不再用被删除 checker 判结果 |
| `test_legacy_hash_and_feedback_remain_unchanged`、`test_legacy_exports_require_explicit_opt_in` | 合并迁到 `tests/compat/test_reading.py`，检查原始 hash/default、报告/签名、请求导出、回放和显式历史 SFT |
| Boolean opt-in 测试名称 | 改为旧 ABI 拒绝与 worker 状态重置，当前协议不再允许 scalar/bool opt-in |
| 本次配置/schema/删减边界 | `test_unification.py` 检查别名/digest/冲突、旧契约拒绝、嵌套 schema、删除后导入闭合、生成器独立且不改真实配置 |

旧标准的替代映射保持显式：AT-09 → UAT-06/09/10；AT-10 → UAT-11/16；AT-11 → UAT-01/16；AT-12 → UAT-16/27；AT-28 → UAT-04/26、SC-02；AT-30 → UAT-20/21/22；AT-35 → UAT-21/22/23。未替代的 AT、全部 UAT-01–28 与 SC-01–05 均有通过记录。AT-28 原接口用例为空是已退休能力，不将其伪装成通过。完整映射和每个参数化用例结果保存在验收 JSON。

测试的人工数值 verifier 明确只是工程模拟，不将 model_inferred 标签伪称为可信真值；rule backend 的测试先绑定外部人工标签。删除旧原生评分工具后仍能通过，证明当前代码依赖闭合，不证明模型自主生成或语义判断质量。

## 验证证据

- [基线 pytest](runs/2026_09_30_16_35_HarnessUnificationBaseline/pytest.log)、[源文件指纹](runs/2026_09_30_16_35_HarnessUnificationBaseline/files.json) 与同目录 `source_snapshot/`、`worktree.patch` 固定原工作区，保留已有未提交改动。
- [最终 pytest 日志](runs/2026_09_30_16_49_HarnessUnificationVerification/delivery_pytest.log)：164 passed；[完整验收矩阵](runs/2026_09_30_16_49_HarnessUnificationVerification/delivery_acceptance.json)：`all_current_requirements_passed=true`。
- [离线 demo](runs/2026_09_30_17_05_HarnessDemo/demo_summary.json)：done；test/reward 各 1、rubric 6、verifier 9，共 17 次 fixture 请求，实际执行 Python 源码。两个合成角色的 per_call/full_trace 视图均导出。
- [恢复示例](runs/2026_09_30_17_05_HarnessRecovery/recovery_evidence.json)：6 个实际 SIGKILL 点，均无重复结果；再次 resume 的结果字节与模型请求不变，观察 replay 新请求为 0。
- [产物核验](runs/2026_09_30_16_49_HarnessUnificationVerification/artifact_verification.json)：9 份历史 schema 原字节、schema 重生成确定性、真实旧报告签名、软件/安装/manifest 版本一致、demo resume/replay。
- [离线 preflight](runs/2026_09_30_16_49_HarnessUnificationVerification/offline_preflight.json) 通过；[AIHub 无凭据 preflight](runs/2026_09_30_16_49_HarnessUnificationVerification/aihub_preflight.json) 仅报预期凭据缺项，没有发送请求。
- `uv lock --check`、锁定依赖安装、schema 重生成、源码语法/残留引用检查和 `git diff --check` 通过；[验证摘要](runs/2026_09_30_16_49_HarnessUnificationVerification/summary.json) 统一索引 U-01–U-12。

此前调试失败日志保留在各自证据目录；不将旧运行/旧源码与新安装混用造成的失败当作兼容通过。当前通过结论以上述最终日志为准。

本次没有真实服务调用、付费模型实验或训练。已有真实服务受故障影响的未完成项继续保留在历史交付记录；Subprocess 仍为 behavioral_prototype，正式隔离与模型质量未验收。所有修改留在工作区，未提交 Git，未重置或覆盖用户已有修改。
