# 离线交付记录

2026-09-26；Python 3.12.14，macOS 26.6 / arm64（runner 报告 Darwin 25.6.0）。实现范围为 `codebase/reward_harness/`；未执行 Git commit、PR 或部署。

## 实际验证

| 命令 | 本次结果 |
|---|---|
| `uv sync --locked --extra dev` | 独立环境与锁文件一致 |
| `.venv/bin/python -m pytest tests --acceptance-output acceptance-results.json` | **80 passed in 34.47s；0 failed，0 skipped** |
| `scripts/offline_demo.py --run-dir runs/demo` | 3 条 success；controller 次数为 2 / 0 / 1 |
| `scripts/recovery_demo.py --output-dir runs/recovery` | 6 个真实 SIGKILL 点均恢复；每次 3 个唯一 result，重复数 0 |
| `validate-config --config configs/real_service.template.yaml` | 按预期拒绝，并一次列出 17 个 REQUIRED 字段 |
| `uv build` | wheel 和 source distribution 构建成功 |

全部 AT-01–AT-36 的测试名称与真实结果见 [acceptance-results.json](acceptance-results.json)。映射由 pytest hook 生成，`all_p0_passed=true`，无跳过项冒充通过。

## 已完成

- M1：独立包、锁文件、CLI、版本化 JSON schemas、canonical golden hash、只读 JSONL/sidecar、输入位置关联及 inline/reference。
- M2：真实 Python worker、single/checklist、partial mean、客观数学/代码 suite、签名且绑定 artifact/suite/policy/runtime 的 evaluator/finalizer、RM broker 和 mock HTTP 验证。
- M3：单 controller 状态机、固定前缀与追加历史、并发读工具有序 barrier、自动 finalization、兼容复用、single-completion 对照。
- M4：持久化共享预算与 unknown 预留、有限物理重试、绝对 deadline、运行中 watchdog、进程取消与清理、journal/checkpoint/result commit 恢复、单写者锁与损坏检测。
- M5：离线 demo、完整请求/响应导出、observation replay、全量成本/失败报告、三种 SFT 视图、机器可读验收矩阵和恢复证据。

## 产物索引

| 产物 | 路径 |
|---|---|
| 安装、配置、所有 CLI 与边界说明 | [README.md](README.md) |
| 完整 demo | `runs/demo/` |
| 查询结果/定义库 | [results.jsonl](runs/demo/results.jsonl)、[reward_library.jsonl](runs/demo/reward_library.jsonl) |
| 报告/候选评分 | [report.json](runs/demo/report.json)、[scores.jsonl](runs/demo/scores.jsonl) |
| 完整真实 LLM message list | [llm_calls.jsonl](runs/demo/exports/llm_calls.jsonl) |
| 逐调用 SFT | [sft_per_call.jsonl](runs/demo/exports/sft_per_call.jsonl) |
| 独立 prototype fixture audit | [prototype.json](runs/demo/audit/prototype.json) |
| 恢复证据与对应原始 run | [recovery_evidence.json](runs/recovery/recovery_evidence.json)、`runs/recovery/*/` |
| single-completion 对照 | [report.json](runs/single_completion/report.json) |
| 构建包 | `dist/rlar_harness-0.1.0-py3-none-any.whl`、`dist/rlar_harness-0.1.0.tar.gz` |

数学 fixture 第一版总返回 1，真实开发测试指出错奖负例；第二版使用固定 checker 后通过。下一条兼容数学 query 零调用复用。代码 fixture 真实执行固定函数接口和边界测试。single-completion 相同初始任务/验证下两条数学 query 各只生成一次并失败，代码任务一次通过；这只说明 fixture 路径，不代表真实质量实验。

恢复的六个证据点为：请求派发前、派发意图后、响应落盘后、工具结果落盘后、library 写后/result 前、result 写后/checkpoint 前。派发后无响应的 run 保留 1 个 unknown attempt，总物理调用为 4；其他 run 为 3。所有 run 的 controller 决策仍为 2 / 0 / 1，SFT 目标为 3，再次 resume 的结果文件逐字不变。

## 未完成与外部依赖

真实 LLM/RM 服务、版本/密钥环境变量、正式隔离 runner、正式 task packs/阈值、Qwen3 tokenizer/template 未配置，**未联调**。没有下载大模型、付费请求、Qwen3 训练或正式审计；token-level loss mask 尚待真实 tokenizer 验证。

subprocess 是受控原型，不能隔离宿主文件/网络/同用户标签。macOS 的内存和进程数量限制不宣称可强制执行。正式 audit 会被拒绝；fixture audit 明确标注 prototype。

当前 heldout 使用固定空初始库，continual 使用本 run 已提交的库；不支持跨 run 的库导入。P1 的生产服务/隔离 runner adapter、文件/Bash、Parquet、大库检索、原生 tool calling、扩展消融均未实现。完整 trace 不作为下一个 query 的运行时记忆。

`runs/` 和 `dist/` 是本地生成产物（已被子项目 `.gitignore` 忽略），可用 README 命令重现；没有提交真实数据或服务凭据。
