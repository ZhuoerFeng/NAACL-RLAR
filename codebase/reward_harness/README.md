# RLAR reward construction harness

独立 Python 子项目，实现根目录 PRD 的离线 P0（M1–M5，AT-01–AT-36）。源 query 文件只读；定义、结果、库、预算、完整 LLM 输入输出和恢复状态写入独立 run 目录。数学、代码 fixture 实际启动 Python worker，并由可信 evaluator 计算开发指标。

## 安装与一条命令 demo

在本目录执行（Python 3.11+，当前验证平台为 macOS / Python 3.12）：

```bash
uv sync --locked --extra dev
.venv/bin/python scripts/offline_demo.py --run-dir runs/demo
```

`uv.lock` 固定依赖；不安装 `parquet` extra，不下载模型。若不用 uv：`python3 -m venv .venv` 后执行 `.venv/bin/pip install -e '.[dev]'`；这条备选安装不保证锁定所有间接依赖。

demo 不访问外网、不调用付费 API、不需要 API key 或 Docker。ScriptedLLM 只替代教师服务，reward 和 evaluator 均真实运行。预期：

| query | 实际行为 | controller 决策 |
|---|---|---:|
| math_001 | 错奖负例 → 开发反馈 → 修复 → 自动提交 | 2 |
| math_002 | 验证证据、contract、runtime 兼容后复用 | 0 |
| code_001 | 固定接口和边界用例首次通过 | 1 |

输出包括 `results.jsonl`、`reward_library.jsonl`、`trace.jsonl`、`blobs/`、`run_manifest.json`、`checkpoints/`、`scores.jsonl`、`report.json`、`replay.json`、`audit/prototype.json`、`exports/llm_calls.jsonl` 和三种 SFT 视图。默认 per-call 导出有 3 个真实 assistant 目标；零调用复用没有目标。`demo_summary.json` 可直接查看结果。已有目录应使用 `resume`，或换一个新目录，不会自动清理。

## CLI

所有配置内相对路径以 **配置文件所在目录** 解析，CLI 文件参数以调用目录解析；manifest 保存绝对路径和内容指纹。

```bash
.venv/bin/python -m rlar_harness validate-config --config configs/offline_demo.yaml
.venv/bin/python -m rlar_harness construct --config configs/offline_demo.yaml --input examples/queries.jsonl --run-dir runs/manual
.venv/bin/python -m rlar_harness resume --run-dir runs/manual
.venv/bin/python -m rlar_harness score --run-dir runs/manual --input examples/candidates.jsonl --output runs/manual/scores.jsonl
.venv/bin/python -m rlar_harness audit --run-dir runs/manual --suite examples/audit_suite.json --output runs/manual/audit/prototype.json --allow-prototype-audit
.venv/bin/python -m rlar_harness report --run-dir runs/manual --output runs/manual/report.json
.venv/bin/python -m rlar_harness replay --run-dir runs/manual --mode observations --output runs/manual/replay.json
.venv/bin/python -m rlar_harness export-llm-calls --run-dir runs/manual --output runs/manual/exports/llm_calls.jsonl
.venv/bin/python -m rlar_harness export-sft --run-dir runs/manual --format per_call --output runs/manual/exports/sft.jsonl
```

`replay --mode observations` 仅核验已有消息与 hash，无服务或代码执行。`--mode execute --execute-run-dir runs/reexecuted` 会建立另一个 run 并重新计算成本，必须明确指定。导出和评分输出不能覆盖输入、定义库、journal、manifest 或 blob。

`audit` 默认要求文件、网络及标签/密钥隔离，subprocess 后端会拒绝。`--allow-prototype-audit` 仅运行合成 fixture，结果显式为 `behavioral_prototype`；不会修改已选产物、construction trace 或训练筛选。

## 工程约定

- `single` 是一个组件和 identity 聚合；`checklist` 对成功执行项等权平均。有效 0 保留；全失败返回 null；每次评分保留 mask 和 coverage。schema 拒绝 weights、重复组件和空 checklist。
- 定义包括 Python 源字符串、criterion、固定 normalization、API/runtime 版本，canonical hash 覆盖全部。只有源代码的 CRLF/CR 会在 **reward hash** 中归一化；请求/响应 blob 保留实际原始内容。
- `test_reward` 执行完整开发 suite，展示条数不改变指标分母。报告 HMAC、artifact、完整 suite digest、policy、profile 和 runtime 全部通过同一 finalizer 校验。报告不是 agent 自写的 PASS 文件。
- 每个 query 一个显式状态机、一个串行 controller。一次生成完整组件计划和代码；自动 finalization 不追加 assistant 消息。独立 `read_resource` batch 可并发，结果在完整 barrier 后按原顺序进入上下文；本地行为测试顺序执行，避免重叠 watchdog。多候选按声明指标和 key 选择。
- 历史只追加。固定 system 前缀不含时间戳/请求 ID；旧 observation 在首次限长后冻结。`model.token_counter=chars_div4_conservative` 保留兼容字段名，但实现使用 **UTF-8 字节数加 overhead** 的保守上界，不使用会低估代码/CJK 的 chars/4；真实 tokenizer 检查尚未提供。
- `library_mode=continual` 将已提交成功产物发布给后续 query；`heldout` 使用固定空初始库，query 间不贡献。默认配置为 heldout，demo 显式 continual。当前不导入其他 run 的函数库；不会把外部同标签条目当作可信零调用证据。`reuse_enabled=false` 同时移除库上下文和复用路径。
- 预算字段必须显式列出，`null` 表示明确不设该维度上限。controller、RM 物理 attempts 共同消耗 `model_requests`，评分另计 `scoring_requests`。每次派发前持久化预留，unknown 保留上界；指标报告另列已观测 token 和未知用量。价格缺失为 null。
- 暂时网络错误只重试同一不可变请求；协议/代码错误进入下一次 controller 决策；鉴权、存储或未知内部错误暂停外层。环境不可用在有限内部重试后直接暂停，采用比配置 circuit breaker 更保守的单次暂停策略。不会自动重启取消的 run。
- 绝对 UTC deadline 持久化，运行中由 monotonic 计时和 POSIX watchdog 中止阻塞调用；停机时间不会延长预算。相同 run 的输入、profile、suite、Python/dependency 或实现版本变化时拒绝 resume，需新建 run。
- 内存 index 与 checkpoint 可以从 journal/results 重建。blob 原子写入并 fsync；result 行是 commit，library 的孤立行不发布。`flock` 单写者；只修复未换行的末尾撕裂记录，中间损坏或缺 blob 立即失败。
- subprocess 只称**受控原型**：每个 example/component 使用新解释器，白名单环境、CPU/wall/output 限额、进程组清理与父进程死亡检测。macOS 不宣称内存/进程数量限制；同用户进程仍可访问宿主文件和网络，HMAC 文件也不构成对恶意代码的安全边界。

## 完整 LLM trace 与蒸馏

`llm/client.py` 导出的 `LLMClient` 统一进入 `llm/durable.py`。adapter 先完成最终 body，保存完整 body/messages/其他字段和 attempt 意图，之后把同一个 body 传给 transport。HTTP adapter 没有 SDK 内部重试；Authorization 只放 header。每个物理 attempt 分别记录 prepared/dispatched/returned/failed/unknown、原始可观察响应、完整性、schema、history commit 与 action dispatch。

`export-llm-calls` 每行展开实际完整请求与响应，含重试和无响应项。缺失/篡改 blob 会失败，旧格式仅有摘要时计为 `legacy_trace_unavailable`，不补造 prompt。观察回放及所有导出不会启动任何请求。

SFT 视图独立选择，不应默认混合训练：

- `per_call`：当前完整 prompt + 当前真实 assistant target；`prompt_loss_mask` 全 false，只有 target 参与 loss。保留合法但测试失败的动作和修复顺序。
- `full_trace`：每个 episode 一份；`message_loss_mask` 对每个被选中的 assistant 只标一次。
- `final_program_only`：初始输入和已验证 `target_program`，明确标注 `target_origin=verified_artifact`、`loss_scope=program_only`，是派生程序视图，不伪造自动提交的 assistant 发言。

默认只选 train/training split 的开发成功 episode。非法 schema、截断、迟到或未提交响应不是正向目标，但真实历史中的非法回复仍保留为条件。每个 episode 沿用固定 split，不逐 call 随机划分；操作者应先按任务家族划分输入。窗口不足明确排除并计数。export manifest 标记 Qwen3 revision/tokenizer/template/thinking 尚未指定，消息级监督范围已导出，**未声称 token-level mask 验证或模型训练完成**。

## 验收与恢复证据

```bash
.venv/bin/python -m pytest tests --acceptance-output acceptance-results.json
.venv/bin/python scripts/recovery_demo.py --output-dir runs/recovery
.venv/bin/python scripts/export_schemas.py
```

`acceptance-results.json` 为实际 pytest hook 生成的机器可读 AT-ID → 测试/结果映射。完整测试覆盖真实 reward/evaluator、loopback HTTP/RM 契约、乱序工具、真实 SIGKILL/SIGTERM、死循环、大 stdout、子进程、请求与 commit 顺序；没有将真实服务跳过项计为通过。

`runs/recovery/recovery_evidence.json` 保存六个强制杀进程边界的退出码、恢复计数、unknown、结果 hash 和再次 resume 无变化的断言。对应子目录保留完整 run、LLM 调用和 SFT 导出。测试还覆盖执行中的取消、父进程被杀后的 worker 清理、deadline 过期及预算竞争。

## Python 接口与文件

`driver.construct_file` 是可恢复文件入口；`driver.construct_stream(records, config, runner=..., llm_client=..., run_dir=...)` 逐条 yield sidecar 结果。内存流由调用方维持顺序；持久化 resume 使用文件入口。函数库由 run-dir 管理，外部 library 导入不在当前实现中。`episode.construct_one(record, EpisodeContext)`、`cli.score_reward`、`ToolDispatcher.test_reward/finalize` 和 `llm.client.llm_call` 可独立集成。

`schemas/` 由类型定义生成；`tests/golden_hash.json` 固定 canonical hash。`examples/make_fixtures.py` 可重建合成数据，不读取或改写项目真实数据。`configs/real_service.template.yaml` 故意包含 REQUIRED，preflight 一次列出所有缺项。

## 未联调与 P1

尚未配置或验证：真实 controller/RM endpoint、model revision 和密钥环境变量，正式隔离 runner、真实 task packs/阈值、Qwen3 tokenizer/chat template。当前不声称真实任务质量改善、缓存加速、正式审计或已训练模型。

P1 未实现：生产服务/隔离 runner adapter、文件/Bash tools、Parquet 输入（extra 仅预留依赖）、大库检索、原生 provider tool calls、扩展消融及真实 tokenizer 验证。没有自动 Git commit、PR、部署，也没有改动论文、`codebase/data/` 或 `codebase/analysis/`。
