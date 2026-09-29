# RLAR reward harness v2

实现 [PRD_REWARD_HARNESS_UPDATE.md](../../PRD_REWARD_HARNESS_UPDATE.md) 的增量链路：test case synthesizer 生成测例 → 确定性准入/冻结 → reward synthesizer 生成组件 → 同一 Python runner/聚合器 → harness-verifier → 同一 finalizer。默认采用语义验收；v1 的总分 FAR/FRR 门槛不参与 v2 提交判断。

## 运行

Python 3.11+；在本子项目目录执行：

```bash
uv sync --locked --extra dev
.venv/bin/python scripts/offline_v2_demo.py
```

Demo 自动在 `runs/YYYY_MM_DD_HH_MM_HarnessV2Demo/` 创建独立目录（Asia/Shanghai 时区），实际执行 verifiable/rubric 混合 checklist。**四个模型角色均为明确标注的 scripted fixtures；这不是模型质量或真实服务验收。** 默认 1 条任务、2 个候选、3 条案例/关系判定；2 个合成调用、2 个 rubric 调用、3 个 verifier 调用。客观标签也通过有界 Python runner 独立校验。

```bash
run_dir="runs/$(TZ=Asia/Shanghai date +%Y_%m_%d_%H_%M)_RewardConstruction"
.venv/bin/python -m rlar_harness validate-config --config configs/offline_v2.yaml
.venv/bin/python -m rlar_harness construct --config configs/offline_v2.yaml --run-dir "$run_dir"
.venv/bin/python -m rlar_harness resume --run-dir "$run_dir"
.venv/bin/python -m rlar_harness report --run-dir "$run_dir" --output "$run_dir/report.json"
.venv/bin/python -m rlar_harness replay --run-dir "$run_dir" --output "$run_dir/replay.json"
.venv/bin/python -m rlar_harness export-llm-calls --run-dir "$run_dir" --output "$run_dir/exports/calls.jsonl"
.venv/bin/python -m rlar_harness export-sft --run-dir "$run_dir" --actor-role test_case_synthesizer --output "$run_dir/exports/tests.jsonl"
.venv/bin/python -m rlar_harness export-sft --run-dir "$run_dir" --actor-role reward_synthesizer --output "$run_dir/exports/rewards.jsonl"
.venv/bin/python -m rlar_harness export-feedback --run-dir "$run_dir" --output "$run_dir/exports/feedback.jsonl"
```

配置中的相对路径相对于配置文件；CLI 路径相对于当前目录。所有运行产物放在本项目 `runs/`，不覆盖历史。`resume` 保留 suite、预算、角色历史和已落盘判断；输入、实现或依赖变化时须新建运行。结果 `status=success` 在报告中映射为 `outcome=done`，没有第二个可写终态。

## 模型配置与真实服务

只使用一个 `models` 目录，`roles` 引用 `test_case_synthesizer/reward_synthesizer/harness_verifier/rubric_judge`。两个合成者可以引用同一模型，历史仍独立。`configs/aihub_v2.yaml` 固定本次指定的 `gpt-6-sol / deepseek-flash / gpt-4.1`；前两者使用 AIHub standard Chat Completions，GPT-4.1 使用 `/openai/v1/responses` 和 `auth_provider: azure`。路由后缀仅在请求时附加到 Authorization，密钥只从 `RLAR_AIHUB_API_KEY` 运行时读取。未获得不可变 revision 时保留 null，不编造版本。

Responses adapter 与 Chat adapter 共享传输、预算和持久化底座；原始 `input/output` 完整留档，token 用量映射到统一账本。当前文本评分配置采用 JSON 输出、`stream: false`、`responses_store: false`，不启用远端工具。所有历史由 harness 提供；Responses 的截断、拒绝、错误和未知费用分别记录。

```bash
.venv/bin/python -m rlar_harness validate-config --config configs/aihub_v2.yaml
```

该配置仍使用小型数学工程任务；不是正式任务质量基准。凭据未注入时 preflight 报告所有缺项，不发送请求，不替换模型。所有角色通过 `llm/durable.py` 和同一 adapter 边界留档，传输只允许一个重试 owner。已有 utility 可实现 `LLMAdapter.send_prepared`，每次只派发一次并返回可观察原始响应；带隐藏重试或不能提供请求/响应的 utility 不符合完整证据契约。

## 数据、执行与验收

- `rlar.reward.v2` 沿用 single/checklist：组件具有 `kind`、`capability_ids`、`criterion`、源码、固定 normalization，rubric 另有 `judge_spec`。模板只有 `context.judge_spec` 一份权威内容；`context.call_llm_api(message, model_name)` 返回原始文本，组件 Python 解析器负责解析。
- ABI `v2` 返回 `{raw_score, feedback, evidence}`；有限数值只归一化一次。正常 0 参与等权均值；执行异常保留 error/mask；全失败为 null。每个 rubric 组件/候选最多一次逻辑 judge 调用；物理重试仍扣全局预算。
- GSM8K 可显式使用 `gsm8k_numeric_v1` checker：取候选最后一个 `####` 后的完整数值，与参考解答最后的数值精确比较；支持整数、小数及规范的千分位逗号。无有效候选答案为 0，缺失/无效参考答案是执行错误。该 checker 只验证最终数值，不证明中间推理；原有 boxed checker 的语义保持不变。
- `SuiteDraft` 使用唯一候选表、能力级 pointwise 标签和显式关系。`SuitePolicy` 固定来源等级、类别、数量及 pairwise/triplet。Triplet 必须包含三个候选的三条关系；等价类合并后不能出现严格偏好环。Objective 标签实际校验；human 标签需要外部固定的证据内容绑定；未证明的模型依据只能标记 `model_inferred`。
- 冻结 suite、依据、required/diagnostic 范围及 digest 后才启动 reward 角色。开发依据可以进入合成上下文；执行函数只收到许可的 query/response/reference/metadata。源码的明显样例/标签查表会被拒绝；原型静态检查不构成安全证明。
- Verifier 只收固定意图、候选和实际分项证据；校验 reward/suite/evidence/model/prompt 绑定及引用。有效 false 不会重抽；格式错误最多修复 5 次。缺所需评分证据为非 completed，不能充当“成功拦截”。无关项 partial 不自动阻断能力级验收。
- 所有判断落盘后由唯一 finalizer 校验签名和全部 required 决策。语言描述不能取代评分。默认语义提示检查真实分项行为；其判断质量仍需要独立标注集评估，HMAC 只证明完整性。

## 预算、消融与导出

`v2.test_synthesis_attempts/reward_synthesis_attempts/model_transport_attempts/verification_format_attempts` 默认均为 5（含首次）；`max_reward_decisions` 约束连续读工具。所有角色共享有限 run/episode 请求、token、工具、组件、测例和 deadline 预算。历史字段 `controller_steps` 在 v2 账本中承担合成决策总上限；报告单独列四种角色，不改写历史 `controller_logical_calls` 的含义。每次派发先持久化预留；未知费用保留 unknown；恢复不免费重置额度。

独立实验开关：`v2.backend=semantic_verifier|rule_baseline`、`v2.feedback=full|scores_only`、task pack `suite_policy.categories` 与 `ranking_layout`。一次只激活一个 verifier backend。Rule baseline 仅用于有独立标签依据的能力级数值规则。反馈消融同步过滤 observation 和 report 资源，verifier 原始证据不受影响。报告记录案例/关系数、组件能力分布、修订次数、各角色调用/token/耗时及 unknown。

`export-sft` 一次显式选择一个合成角色，默认 reward 角色；`full_trace` 不跨角色拼接历史。Test case 角色按 suite 自身准入成功筛选，即使后续 reward 失败也可导出；reward 角色按开发 done 筛选，保留合法失败尝试及修复。工具模型 targets 始终为 0，工具观察进入 prompt 但不计 loss。`final_program_only` 仅适用 reward；语言反馈是独立导出视图。Split 由任务家族预先分配到 run，不按 call 随机切分。未执行 tokenizer 级 mask 验证或训练。

## 冻结评分与独立 audit

原有 `score`、`audit` CLI 继续使用。v2 的模型调用写入新的、符合命名规范的证据运行，默认自动生成，也可通过 `--execution-run-dir` 指定；不改变原 construction trace。

`score --input` 接收 JSONL `{query_id, candidate_id, response}`。`audit --suite` 接收 JSON 对象 `{query_id: SuiteDraft}`，该文件由外部提供并独立留存。Audit 强制恢复 fail/pass/ranking 全类别，检查独立依据，不反馈修订、不筛选 SFT。Subprocess 后端的正式 audit 会被拒绝；`--allow-prototype-audit` 仅记录原型保证。没有独立标注的 verifier 判断集时，不宣称验收模型质量已合格。

## 验证与兼容

```bash
verify_dir="runs/$(TZ=Asia/Shanghai date +%Y_%m_%d_%H_%M)_HarnessVerification"
mkdir -p "$verify_dir"
.venv/bin/python -m pytest tests --basetemp="$verify_dir/pytest" --acceptance-output "$verify_dir/acceptance.json"
.venv/bin/python scripts/export_schemas.py
```

验收 hook 分别输出 AT 和 UAT 矩阵；[V2_DELIVERY.md](V2_DELIVERY.md) 记录本次结果。`schemas/` 保留历史 v1 文件并增加 v2 文件。v1 显式配置和旧 fixture 仅用于历史回归；旧 reward hash、原始 blob、scalar ABI、报告签名及无反馈语义保持兼容，不原地升级旧 run。旧协议在变更范围内 superseded，映射见交付说明。

Subprocess 仍是同用户的受控原型，具有 wall/CPU/output 与进程组清理，**不提供文件、网络、标签或密钥的正式隔离**。未捆绑部署、训练器、文件/Bash 工具、Parquet、大库检索或跨 run 库导入。
