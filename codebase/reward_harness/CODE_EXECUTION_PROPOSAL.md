# 增量需求：轻量 code 执行环境

状态：已实现（2026-10-02，未提交）。与草案的差异见第 7 节。

## 1. 背景

DS-1000 pandas 样本（`runs/2026_10_02_14_42_Ds1000PandasRewardSynthesis`、`runs/2026_10_02_14_53_Ds1000PandasProtocolFixRerun`）暴露：`self_contained_v1` 下 reward 源码只能用 `re/json/math/decimal/fractions`，禁止 `exec/eval/import pandas`。代码题的核心标准（"插入 `exec_context` 后 `result` 是否等于期望"）因此无法确定性检查，只能用正则静态检查 + LLM judge 代替。模型在两次运行中都把"功能正确性"交给了 rubric。

## 2. 目标与非目标

目标
- reward 组件可以**请求**在一个独立的、预先声明的 Python 环境（含 pandas/numpy 等）中运行一段程序，并拿到结构化结果，再由组件自己判分。
- 判分逻辑仍归 reward 模型所有：组件自己拼程序（例如 `code_context` + 候选代码 + 调用 `test_execution`），自己解析结果。harness 不提供内置 checker，不知道"正确"是什么。
- 调用与 `call_llm_api` 同级：经 trusted parent 代理、计预算、全量留档、可回放。

非目标
- 不提供正式安全隔离；保证等级仍为 `behavioral_prototype`，不得标记为生产隔离验收通过。
- reward 进程本身仍不能 `import pandas` 或 `exec`；不放宽现有白名单。
- 不支持任意语言、长驻服务、网络访问或跨调用状态。

## 3. 方案

### 3.1 组件侧 API

```python
out = context.run_code(source: str, *, stdin: str = "", timeout_s: float | None = None)
# -> {"status": "ok"|"error"|"timeout"|"output_limit",
#     "exit_code": int|None, "stdout": str, "stderr": str,
#     "stdout_truncated": bool, "wall_seconds": float}
```

- 仅当 task pack 声明 `permitted_apis` 含 `run_code` 且组件 `required_apis` 含 `run_code` 时可用；`verifiable` 与 `rubric` 均可声明。
- 程序非零退出或抛异常是**正常观测**（`status="error"`），由组件决定得分；只有执行环境本身不可用才是基础设施错误（`scoring_service_error`），不得伪装为 0 分。
- 每个组件/候选的 `run_code` 次数有上限（默认 1，task pack 可调）。

### 3.2 执行环境（code env）

- 在 task pack 中声明，例如：

```json
"code_execution": {
  "env_id": "py312-pandas2.2-numpy2-v1",
  "interpreter": "envs/ds1000/.venv/bin/python",
  "lock_digest": "<uv.lock sha256>",
  "timeout_s": 10, "memory_mb": 1024, "max_output_bytes": 65536,
  "max_calls_per_component_example": 1
}
```

- 独立 venv（由 uv 锁定），与 harness 自身 venv 分离，harness 不依赖 pandas。
- 每次调用：新进程、临时空工作目录、`python -I`、清空环境变量（凭据不可见）、固定 `PYTHONHASHSEED`、wall/CPU/输出上限、进程组清理；Linux 上额外 RLIMIT_AS 与（可用时）无网络 namespace。macOS 上网络与文件不隔离，如实写入 runner capabilities。
- `env_id` 与 `lock_digest` 进入 runtime fingerprint / reward key，环境变化即新运行，不可 resume 混用。

### 3.3 证据与回放

- 新 journal 事件 `code_exec_dispatched` / `code_exec_result`，保存源码、stdin、完整输出（blob）及 digest。
- `replay` 使用已记录结果，不重新执行；`resume` 不重放已落盘调用。
- 非确定性程序（如 `np.random.permutation`）的结果按实际记录；是否要求固定随机种子由 task contract 说明，harness 不改写候选代码。

## 4. 牵涉改动

| 模块 | 改动 |
|---|---|
| `schemas.py` | TaskPack 增加 `code_execution` 段；`permitted_apis` 允许 `run_code`；RuntimeContract 记录 code env 与 API 版本 |
| `runtime/policy.py` | 版本化策略：`validate_pack`/`validate_component` 允许声明了的 `run_code`；`reward_context` 暴露 `run_code`；白名单不变 |
| `runtime/worker.py` | `JudgeChannel` 增加 `run_code`，复用现有 proxy 管道 |
| `runtime/runner.py` | `_ScoringServicer` 分派 `run_code` 请求；capabilities 报告 code env 隔离能力 |
| 新增 `runtime/code_exec.py` | `CodeExecutor`：启动受限子进程、限额、截断、清理、结果结构化 |
| `budget.py` | 新计数 `code_executions`（run/episode），先预留后结算 |
| `config.py` | preflight：解释器存在、锁文件 digest 匹配、可 import 声明的包；不满足时报告并不发请求 |
| `tools/resources.py`、`episode.py` | `scoring_abi` 资源与 reward 提示词说明 `run_code` 的签名、限额与"非零退出不是错误" |
| `trace/export.py`、`trace/report.py` | 导出/回放 code exec 事件；报告统计调用数、超时、耗时 |
| `adapters/verl.py` | 无接口变化；文档说明训练 worker 需安装同一 code env |
| `examples/`、`tests/` | DS-1000 task pack 示例；单测：超时、输出截断、非零退出、凭据不可见、预算耗尽、回放不重执行、未声明时 `forbidden_api` |

预计规模：新增约 300–400 行（含测试），既有模块各为小幅修改。

## 5. 相关但独立的问题（本次测试发现，建议单独修）

- Rubric 配置 `response_format=json_object` 时，OpenAI Responses 要求输入中出现 "json"；judge 提示词由模型编写，未包含即 HTTP 400（重跑中 5 次全部失败）。
- `runtime_contract.dependencies=['re','json']` 与 `judge_spec.model_ref='gpt-4.1'` 被拒时，错误信息未说明正确取值（应为空列表、目录键 `rubric`）。
- checklist 等权均值使"兼容但错误"的答案得 0.5（见交付说明）；与执行环境正交，需在聚合或 task pack 层面另行决策。

## 6. 待确认

1. 首批 code env 的包与版本（建议：DS-1000 所需 pandas/numpy，其余按需追加）。
2. `run_code` 默认每组件/候选 1 次是否足够。
3. 是否需要 Linux 上的网络隔离作为该能力的启用前提，还是 macOS 原型即可先行。

## 7. 实现记录与差异

- 实现：`runtime/code_exec.py`（`CodeExecutor`、`ScoringRouter`、`probe_environment`），`schemas.CodeExecutionSpec` / `TaskPack.code_execution`，`execution.code_envs`（env_id → 绝对解释器路径），policy/worker/resources/提示词/driver 接线。未声明 `code_execution` 的 pack、config 与提示词字节不变，历史 digest 不变。
- 预算：未新增 `code_executions` 计数（会改变配置 digest）。上限由 `max_calls_per_example`（默认 1）× 已计预算的 component execution 约束；每次调用写入 `code_exec_result` 事件（源码/输出 blob）。
- 环境固定：task pack 写明包版本；preflight 和首次调用均探测解释器实际版本，不一致即 `scoring_service_error`，不静默运行。另检查 `timeout_s × max_calls` 小于 worker `wall_timeout_s`。
- 程序目录与输出日志分离，候选代码看不到自己的日志；Python 忽略 SIGXFSZ，超限写入记为 `output_limit`。
- 首个环境：`examples/code_envs/ds1000/`（pyproject + uv.lock，pandas 2.2.3 / numpy 2.1.3），构建到 `.code_envs/ds1000`（已 gitignore）：
  `UV_PROJECT_ENVIRONMENT="$PWD/.code_envs/ds1000" uv sync --locked --project examples/code_envs/ds1000`
- 待确认第 6 节问题均按默认处理：首批包为 DS-1000 所需；默认 1 次；macOS 原型先行，不提供网络隔离。
- 真实联调：`runs/2026_10_02_15_29_Ds1000PandasCodeExecSynthesis/`（success，16 次真实执行）。
- verl 文件入口（`examples/verl_reward.py`）尚未支持 code env；需要时在 runner 上设置 `ScoringRouter(code=CodeExecutor(...))`。
