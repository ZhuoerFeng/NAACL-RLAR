# RLAR Reward Harness

当前唯一执行流程由 [统一需求](../../PRD_REWARD_HARNESS_CURRENT.md) 定义：test case synthesizer 生成测例 → 确定性准入/冻结 → reward synthesizer 生成组件 → 同一 Python runner/聚合器 → harness-verifier → 同一 finalizer。默认采用能力级语义验收；执行、预算、持久化与提交各使用一套实现。迁移及本次验证见 [UNIFICATION_DELIVERY.md](UNIFICATION_DELIVERY.md)。

## 运行

Python 3.11+；在本子项目目录执行：

```bash
uv sync --locked --extra dev
.venv/bin/python scripts/offline_demo.py
```

Demo 自动在 `runs/YYYY_MM_DD_HH_MM_HarnessDemo/` 创建独立目录（Asia/Shanghai 时区），实际执行 verifiable/rubric 混合 checklist。**四个模型角色均为明确标注的 scripted fixtures；这不是模型质量或真实服务验收。** 默认使用 Natalia GSM8K 样本、6 个候选、9 条案例/关系判定；2 个合成调用、6 个 rubric 调用、9 个 verifier 调用。评分源码是明确标注的工程 fixture；测例只做结构与来源检查，不运行原生 checker。

```bash
run_dir="runs/$(TZ=Asia/Shanghai date +%Y_%m_%d_%H_%M)_RewardConstruction"
.venv/bin/python -m rlar_harness validate-config --config configs/offline.yaml
.venv/bin/python -m rlar_harness construct --config configs/offline.yaml --run-dir "$run_dir"
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

只使用一个 `models` 目录，`roles` 引用 `test_case_synthesizer/reward_synthesizer/harness_verifier/rubric_judge`。两个合成者可以引用同一模型，历史仍独立。`configs/aihub.yaml` 固定本次指定的 `gpt-6-sol / deepseek-flash / gpt-4.1`；前两者使用 AIHub standard Chat Completions，GPT-4.1 使用 `/openai/v1/responses` 和 `auth_provider: azure`。路由后缀仅在请求时附加到 Authorization，密钥只从 `RLAR_AIHUB_API_KEY` 运行时读取。未获得不可变 revision 时保留 null，不编造版本。

Responses adapter 与 Chat adapter 共享传输、预算和持久化底座；原始 `input/output` 完整留档，token 用量映射到统一账本。当前文本评分配置采用 JSON 输出、`stream: false`、`responses_store: false`，不启用远端工具。所有历史由 harness 提供；Responses 的截断、拒绝、错误和未知费用分别记录。

```bash
.venv/bin/python -m rlar_harness validate-config --config configs/aihub.yaml
```

该配置使用用户指定的 Natalia GSM8K 样本及原始参考解答；不是独立任务质量基准。凭据未注入时 preflight 报告所有缺项，不发送请求，不替换模型。所有角色通过 `llm/durable.py` 和同一 adapter 边界留档，传输只允许一个重试 owner。已有 utility 可实现 `LLMAdapter.send_prepared`，每次只派发一次并返回可观察原始响应；带隐藏重试或不能提供请求/响应的 utility 不符合完整证据契约。

## 数据、执行与验收

- `rlar.reward.v2` 沿用 single/checklist：组件具有 `kind`、`capability_ids`、`criterion`、源码、固定 normalization，rubric 另有 `judge_spec`。模板只有 `context.judge_spec` 一份权威内容；`context.call_llm_api(message, model_name)` 返回原始文本，组件 Python 解析器负责解析。
- ABI `v2` 返回 `{raw_score, feedback, evidence}`；有限数值只归一化一次。正常 0 参与等权均值；执行异常保留 error/mask；全失败为 null。每个 rubric 组件/候选最多一次逻辑 judge 调用；物理重试仍扣全局预算。
- 当前执行契约为 `self_contained_v1`，同时写入配置、task pack 与 reward runtime contract，参与指纹绑定。格式解析、答案提取、合法性检查、比较及 raw_score 必须由模型在源码中实现，可自行定义辅助函数。Verifiable 无 context API；rubric 仅可使用 `judge_spec` 与原始 `call_llm_api`。
- 源码准入与执行均应用版本化依赖规则：只允许 `re/json/math/decimal/fractions` 中列出的通用成员，禁止内部 checker 导入、动态执行、反射与私有属性访问。违规为 `forbidden_api`，即使组件捕获异常也不能伪装成有效 0。该能力限制仍属 `behavioral_prototype`，不代表正式 OS 隔离。
- `SuiteDraft` 使用唯一候选表、能力级 pointwise 标签和显式关系。`SuitePolicy` 固定来源等级、类别、数量及 pairwise/triplet。Triplet 必须包含三个候选的三条关系；等价类合并后不能出现严格偏好环。当前流程拒绝 objective checker 及 `objective_verified`；human 标签需要外部固定的证据内容绑定；模型依据标记 `model_inferred`。准入报告明确 `structure_and_provenance_only`，不宣称语义真值已证明。
- 冻结 suite、依据、required/diagnostic 范围及 digest 后才启动 reward 角色。开发依据可以进入合成上下文；执行函数只收到许可的 query/response/reference/metadata。源码的明显样例/标签查表会被拒绝；原型静态检查不构成安全证明。
- Verifier 接收原始任务、许可的参考答案、固定意图、候选和实际分项证据，先判断测例意图是否有依据，再判断实际数值行为；校验 reward/suite/evidence/model/prompt 绑定及引用。有效 false 不会重抽；格式错误最多修复 5 次。缺所需评分证据为非 completed，不能充当“成功拦截”。无关项 partial 不自动阻断能力级验收。
- 所有判断落盘后由唯一 finalizer 校验签名和全部 required 决策。语言描述不能取代评分。默认语义提示检查真实分项行为；其判断质量仍需要独立标注集评估，HMAC 只证明完整性。

## 代码执行环境（run_code）

Task pack 可声明 `code_execution`（env_id、固定包版本、超时/输出/次数上限），config 在 `execution.code_envs` 中把 env_id 映射到独立 venv 的绝对解释器路径。声明后组件可在 `required_apis` 写入 `run_code` 并调用 `context.run_code(source, stdin="", timeout_s=None)`，得到 `{status, exit_code, stdout, stderr, ...}`；程序失败是组件自行打分的观测，环境不可用才是 `scoring_service_error`。Reward 进程本身的白名单不变。保证等级仍为 `behavioral_prototype`。详见 [CODE_EXECUTION_PROPOSAL.md](CODE_EXECUTION_PROPOSAL.md)。

## 测例一致性与失败反馈

准入时 pointwise 标签被转为隐含偏序（能力内 pass > fail；overall 下 overall pass > fail 及完整能力标签的 Pareto 支配），与显式关系一起检查严格环，拒绝时列出冲突链。测例可附 `violation_feedback`；`test_reward` 结果的 `unmet_cases`（亦可经 `unmet_cases:<report_id>` 资源分页读取）列出每个未通过测例的意图、关系依据、候选、实际分项得分和 verifier 依据。`scores_only` 消融同样隐藏 `violation_feedback`。该检查只能发现矛盾，不能补出缺失的测例。

## 预算、消融与导出

`synthesis.test_synthesis_attempts/reward_synthesis_attempts/model_transport_attempts/verification_format_attempts` 默认均为 5（含首次）；`max_reward_decisions` 约束连续读工具。所有角色共享有限 run/episode 请求、token、工具、组件、测例和 deadline 预算。历史字段 `controller_steps` 在当前账本中承担合成决策总上限；报告单独列四种角色，不改写历史 `controller_logical_calls` 的含义。每次派发先持久化预留；未知费用保留 unknown；恢复不免费重置额度。

独立实验开关：`synthesis.backend=semantic_verifier|rule_baseline`、`synthesis.feedback=full|scores_only`、task pack `suite_policy.categories` 与 `ranking_layout`。一次只激活一个 verifier backend。Rule baseline 仅用于有独立标签依据的能力级数值规则。反馈消融同步过滤 observation 和 report 资源，verifier 原始证据不受影响。报告记录案例/关系数、组件能力分布、修订次数、各角色调用/token/耗时及 unknown。

`export-sft` 一次显式选择一个合成角色，默认 reward 角色；`full_trace` 不跨角色拼接历史。Test case 角色按 suite 自身准入成功筛选，即使后续 reward 失败也可导出；reward 角色按开发 done 筛选，保留合法失败尝试及修复。工具模型 targets 始终为 0，工具观察进入 prompt 但不计 loss。`final_program_only` 仅适用 reward；语言反馈是独立导出视图。Split 由任务家族预先分配到 run，不按 call 随机切分。未执行 tokenizer 级 mask 验证或训练。

## 冻结评分与独立 audit

原有 `score`、`audit` CLI 继续使用。所有模型调用写入新的、符合命名规范的证据运行，默认自动生成，也可通过 `--execution-run-dir` 指定；不改变原 construction trace。

`score --input` 接收 JSONL `{query_id, candidate_id, response}`。`audit --suite` 接收 JSON 对象 `{query_id: SuiteDraft}`，该文件由外部提供并独立留存。Audit 强制恢复 fail/pass/ranking 全类别，检查独立依据，不反馈修订、不筛选 SFT。Subprocess 后端的正式 audit 会被拒绝；`--allow-prototype-audit` 仅记录原型保证。没有独立标注的 verifier 判断集时，不宣称验收模型质量已合格。

## RL 框架 adapter（verl）

`rlar_harness.adapters.verl.VerlRewardAdapter` 提供独立的接入层，不改变构造流程或生成组件的 `score(example, context)` ABI，也不依赖 verl、torch 或 Ray。适用于已由调用方选定的 reward；`data_source` 不会自动选择 reward，也不代表该 reward 已验证适用于整个数据集。

```python
from rlar_harness.adapters.verl import VerlRewardAdapter

# definition: RewardDefinition；task_pack: TaskPack；runner: 已配置的 Runner。
# runner 的工作目录、judge 服务、预算和调用留档由调用方管理。
adapter = VerlRewardAdapter.from_reward(definition, task_pack, runner)
compute_score = adapter.compute_score

result = compute_score(
    data_source="openai/gsm8k",
    solution_str="48 + 24 = 72.\n#### 72",
    ground_truth="原始参考解答……#### 72",
    extra_info={"rlar": {"query": "原始问题", "metadata": {}}},
)
# {"score": 1.0}，具体值由所选 reward 决定。
```

默认映射：`solution_str → response`、`ground_truth → reference`、`extra_info.rlar.query → query`、`extra_info.rlar.metadata → metadata`。metadata 中的字段也按既有约定展开到 example 顶层，再由 task pack 的 `permitted_inputs` 过滤；禁止 metadata 覆盖 query/response/reference/metadata。原始输入不会被修改，工具配置、rollout 分数及其他 extra_info 字段不自动传入。ground_truth 的内容和类型必须与构建时的 reference 契约一致；本地 verl 默认 GSM8K 预处理的 ground_truth 仅为提取后的数字，不能直接假定它等同于原始解答。

已有其他数据布局时传入 `input_mapper(data_source, solution_str, ground_truth, extra_info) -> dict`。已有带预算、留档或服务调用的评分运行时时，直接使用 `VerlRewardAdapter(scorer)`，其中 `scorer(example) -> ScoreResult`。`from_reward` 复用公开的 `rlar_harness.evaluation.scoring.score_reward`，使用同一 runner/白名单/聚合器；CLI 原 `score_reward` 导入保持兼容。

成功只返回 `{"score": total_score}`，合法 0 分正常返回。当前不提供异常回传协议：scorer 异常原样抛出；partial/failed 或非有限总分在本地抛出 `ValueError`，不返回错误字典，不填 0。adapter 不提供训练跳过、重试、丢组或故障恢复策略。使用本地 verl 新版 `naive` manager；不要用会把异常转成 0 的 manager 来假定上述语义仍成立。

每个 worker 初始化一次 adapter；同一实例串行评分，避免共享 judge 的执行绑定互相覆盖。不同 adapter 不应并发共享同一个有状态 runner。rubric 使用调用方提供的 judge-enabled runner 或 durable scorer，adapter 不选择模型、不注入凭据、不绕过真实调用留档。

[examples/verl_reward.py](examples/verl_reward.py) 是可由 verl 按文件加载的 **verifiable reward** 脚手架。准备已选定的完整 `RewardDefinition` JSON 和对应 `TaskPack` JSON（不是整个 `results.jsonl`），并在所有 worker 安装相同版本的 rlar-harness。路径按部署环境替换：

```yaml
reward:
  reward_manager:
    source: register
    name: naive
  reward_model:
    enable: false
  custom_reward_function:
    path: /path/to/reward_harness/examples/verl_reward.py
    name: compute_score
    reward_kwargs:
      reward_path: /path/to/frozen/reward.json
      task_pack_path: /path/to/frozen/task_pack.json
      execution_run_dir: /project/codebase/reward_harness/runs/YYYY_MM_DD_HH_MM_VerlRewardTraining
```

文件入口按路径缓存，不要在训练中修改 artifact 文件；变更 reward 应新开运行及 worker。示例不自动建立跨 query 索引、导出 bundle 或配置 rubric 服务；rubric 请在自己的入口中使用上面的 Python API。此接入不改变 SubprocessRunner 的原型隔离等级。

## 验证与兼容

```bash
verify_dir="runs/$(TZ=Asia/Shanghai date +%Y_%m_%d_%H_%M)_HarnessVerification"
mkdir "$verify_dir" || exit 1
.venv/bin/python -m pytest tests --basetemp="$verify_dir/pytest" --acceptance-output "$verify_dir/acceptance.json"
.venv/bin/python scripts/export_schemas.py
```

验收 hook 输出历史 AT、当前 UAT、SC 矩阵及被替代要求的映射；只有适用要求全部有通过证据且测试进程成功退出时，`all_current_requirements_passed` 才为 true。`schemas/` 的历史 v1 文件保持原字节；当前模型直接生成准确的嵌套 schema。验收仅证明离线工程行为。

Subprocess 仍是同用户的受控原型，具有 wall/CPU/output 与进程组清理，**不提供文件、网络、标签或密钥的正式隔离**。未捆绑部署、训练器、文件/Bash 工具、Parquet、大库检索或跨 run 库导入。

## 命名、配置与历史兼容

Python 对象使用 `SynthesisConfig` / `config.synthesis`，用户 YAML 使用 `synthesis:`。读取边界接受已有 `v2:` 别名；两者同时出现时拒绝。规范化配置、manifest 和配置 JSON Schema 保留 `rlar.config.v2` 的 `v2` wire key，避免混用两种 digest。禁用的旧 `rm` 空段可读取后丢弃，非空 RM 配置和旧单 `model` 入口拒绝；旧配置不会自动升级成新契约。

软件版本只来自 `src/rlar_harness/__init__.py`，当前仍为 `0.1.0`。`rlar.*.v2`、ABI `v2`、`self_contained_v1`、`agg.v1`、`canonical.v1` 和 HTTP adapter wire ID 各自标识不同契约，不是两个框架，不能机械替换版本号。HTTP 类名分别为 `HttpChatJsonAdapter` 和 `HttpResponsesJsonAdapter`。报告新生成的展示字段为 `synthesis_statistics`，原 `v2_statistics` 的读取方需更新；旧报告文件不改写。

`compat/reading.py` 只读取历史序列化数据，不应用当前模型默认值。历史 `report`、`export-llm-calls`、观察 `replay` 可用；SFT 默认排除旧 reward，显式 `--include-legacy` 才导出并保留历史身份。旧 checker、scalar ABI 和 RM 执行器已删除，旧配置不能 construct/resume/score/audit。历史复现需原始源码与依赖环境。

即使历史运行已经使用结构化 ABI 和自实现评分，源码路径/内容指纹变化也会阻止直接 resume；不要改写 manifest 或绕过保护。当前代码新建的运行可以在输入、资源、配置及依赖均未变化时恢复。

唯一 fixture 生成器只写新指定目录，不修改真实服务配置：

```bash
fixture_dir="runs/$(TZ=Asia/Shanghai date +%Y_%m_%d_%H_%M)_FixtureGeneration"
.venv/bin/python examples/make_fixtures.py --output-dir "$fixture_dir"
.venv/bin/python -m rlar_harness validate-config --config "$fixture_dir/offline.yaml"
.venv/bin/python scripts/recovery_demo.py
```

生成器输出为明确标注的人工工程 fixture，不能作为模型自主合成的证据。旧入口迁移映射及删除清单见 [交付记录](UNIFICATION_DELIVERY.md)。
