# Policy rollout 采样与管理

入口是 `scripts/sample_rollouts.py`，也可用 `python -m rlar_harness.rollout`。它直接调用太极 Chat Completions 流式接口，读取正式数据集的 `train_prompts.jsonl`，不需要上传平台数据集。只采集 policy 回答；不执行回答中的代码，不生成 reward 或偏好标签。

## 默认实验配置

`configs/rollout_qwen35_9b.yaml` 已配置：

| 项目 | 默认值 |
| --- | --- |
| 数据集 | `codebase/data/datasets/rlar_train_1000_v1`，全部 1,000 个 prompt |
| 模型 | `Qwen3.5-9B_inference`；不可变 revision 未获知，记录为 null |
| 每个 prompt 的回答数 | 1（配置 `samples_per_prompt` 可增加） |
| thinking / 输出上限 | disabled / 8,000 tokens |
| 采样 | temperature 0.7、top_p 0.6、top_k 20、beam_size 1、repetition_penalty 1.05 |
| 并发 / 派发速率 | 最多 32 个进行中的请求 / 每分钟最多 1,920 次启动（均匀间隔约 31.25 ms）；平台实际额度仍需确认 |
| 请求超时 | 600 秒，包括整个流式响应 |
| 重试 / 总请求预算 | 每个 rollout 最多 3 次物理请求，整个 run 最多 3,000 次，恢复不重置 |
| 凭据 | 运行时环境变量 `TMP_TOKEN`，不保存 token 或 Authorization |

`order_seed=20261004` 只控制本地 prompt 顺序及 `--limit` 子集，不代表模型生成可精确复现。`generation_seed=null` 表示不发送模型 seed；平台支持 seed 后可设置，候选 i 使用 `generation_seed+i`。原始多轮 `prompt` 原样作为 messages，不另加 system 消息，不混入独立参考答案。

32 并发表示最多同时等待 32 个完整的流式请求，某个 worker 完成后继续取下一条；不是每条 prompt 生成 32 个回答。派发速率与并发数是两个独立上限：原来的 60 次/分钟会限制短请求的有效并发，现已调整为客户端每秒最多启动 32 次。实际并发会受响应时长、剩余任务和平台限流影响；遇到 429 仍按原有策略退避重试。这一设置没有改变每条 1 个回答或 3,000 次总请求预算。

关闭 thinking 使用请求体 `chat_template_kwargs: {enable_thinking: false}`。2026-10-04 的独立单条真实试运行已验证平台接受该参数和 8,000 tokens 上限：HTTP 200、finish_reason=stop、无可见思考内容，输入 276 / 输出 117 tokens，耗时 1.35 秒。证据在 [QwenNineBRolloutPilot](runs/2026_10_04_20_54_QwenNineBRolloutPilot/summary.json)，完整 SSE 回放和 JSONL / Parquet / 证据包导出均通过。

脚本会在出现非空 `reasoning_content` / `reasoning` 或非空 `<think>` 内容时标记 `thinking_violation` 并停止后续派发；已派发的少量并发请求会收尾。平台若不支持这个参数，应先查明服务配置，然后新建运行，不能把已产生的思考内容删除后当作 no-thinking 数据。无可见思考内容本身也不是服务内部关闭思考的独立证明。

## 启动与恢复

当前已准备完整的 1,000 条 **32 并发**运行：`runs/2026_10_04_20_57_QwenNineBRolloutConcurrent32`，尚未派发请求，可以直接使用下面的 `sample` 命令。此前 `runs/2026_10_04_20_53_QwenNineBRollout` 保留原来的 4 并发冻结配置，不用于本次 32 并发采样。`prepare` 用于需要新建独立实验时；配置增加 `samples_per_prompt` 时，也要相应增加 `max_requests`，至少覆盖第一遍生成次数。

下面从项目根目录执行，使用已有的 Python 3.11+ venv（依赖 httpx / pydantic / PyYAML）。示例 shell 为项目默认 zsh。token 通过隐藏输入注入，不把 token 字面量写入命令历史：

```zsh
cd /Users/andrewfeng/Desktop/overleaf_projects/NAACL_RLAR
read -r -s 'TMP_TOKEN?Taiji token: '
export TMP_TOKEN

# 新建实验时：只冻结 1,000 条输入和配置，不发请求。
codebase/reward_harness/.venv/bin/python codebase/reward_harness/scripts/sample_rollouts.py prepare \
  --config codebase/reward_harness/configs/rollout_qwen35_9b.yaml --task QwenNineBRolloutConcurrent32

# 本次已准备的 run；若重新 prepare，则换成新输出的路径。
rollout_run='codebase/reward_harness/runs/2026_10_04_20_57_QwenNineBRolloutConcurrent32'

# 先发 1 次真实请求，确认平台接受 no-thinking / 8,000 tokens。
codebase/reward_harness/.venv/bin/python codebase/reward_harness/scripts/sample_rollouts.py sample \
  --run "$rollout_run" --max-new-calls 1
codebase/reward_harness/.venv/bin/python codebase/reward_harness/scripts/sample_rollouts.py status --run "$rollout_run"

# 使用同一个 run 继续剩余数据；已完成项不会重发。
codebase/reward_harness/.venv/bin/python codebase/reward_harness/scripts/sample_rollouts.py sample --run "$rollout_run"
```

`--max-new-calls` 包含物理重试次数，适合分批执行并复用同一运行。若只想独立试跑少量 prompt，可以在 `prepare` 加 `--limit 10 --task QwenNineBPilot`；该运行会固定为十条，不能原地扩大为一千条。

Ctrl-C 可以中断；再次 `sample --run ...` 继续。`result.json` 原子提交前的请求视为 uncertain，保留部分 SSE，但不默认重发，因为服务可能已经生成并计费。检查证据后，可显式使用 `--retry-uncertain`，接受可能重复生成/计费。每次重试写新 attempt，不覆盖旧响应。

自动重试只覆盖 HTTP 408、429、500、502、503、504；遵守数字秒形式的 Retry-After，加指数退避，并共享并发、速率和总预算。网络中断/超时停止后续派发，标为 uncertain。400/401/403/404 等、模型不符、流式协议错误、thinking 违规会停止派发，普通恢复也会阻止绕过已知的服务契约失败。凭据修正后，`--retry-failed` 可重试 failed / protocol_error / empty / unsupported_finish；thinking 或模型契约不符须解决服务配置并建立新 run。所有重试仍受原预算限制；程序不换模型、不改生成参数。

`finish_reason=length` 标为 truncated，不自动重抽，默认排除候选导出。`stop` 且有非空回答、完整 `[DONE]`、没有协议或模型错误才是 success。该状态只表示生成完整，**不代表答案正确**。语义上的文本拒答若没有服务端 refusal/content_filter 标记，不由本采样器自动判断。

## 目录与身份

```text
codebase/reward_harness/
  configs/rollout_qwen35_9b.yaml       # 可复制成不同模型/采样策略的配置
  scripts/sample_rollouts.py          # 统一命令入口
  runs/YYYY_MM_DD_HH_MM_QwenNineBRollout/
    manifest.json                    # 数据集/配置/实现指纹、run_id、模型、数量
    config.json                      # 冻结配置，只有凭据环境变量名
    inputs/
      prompts.jsonl                  # 固定顺序，含 sample_id、原始行号、prompt hash
      dataset_manifest.json
      rollout_contract.json
    calls/<rollout_id>/0001/
      request.json                   # 完整请求体，含唯一 request_id
      started.json
      response_headers.json          # 排除认证与 cookie 字段
      response.sse                   # 完整流；若服务回显 token，会脱敏
      history.json                   # 原始多轮历史 + 本次回答；工具观察为空
      result.json                    # 状态、回答/思考分开、usage、延时、指纹
    summary.json                     # 最近 sample 完成时的快照
    replay.json                      # 最近显式回放校验结果
    exports/<export_id>/              # 不可覆盖的导出快照
      rollouts.jsonl
      candidates.jsonl
      by_prompt.jsonl
      export.json
      evidence.tar.gz                # 可选 --bundle-evidence
```

每个模型/采样策略独立建 run；同分钟自动加 PascalCase `Run2` 等后缀，永不覆盖。`sample_id` 对齐原始 prompt；`rollout_id` 绑定 `(run_id, sample_id, sample_index)`，物理重试不产生新的逻辑候选。不同 run 的候选都保留，包括同一模型重复实验。run 内参数、输入或采样实现变化时拒绝恢复，需要建立新运行。

所有实验产物留在项目 runs 下；正式数据集版本只读。生成数据经确认可复用后，再按项目规则发布到 `codebase/data/datasets/` 的新版本，本工具不自动发布。

## 导出、回放和多模型合并

```zsh
codebase/reward_harness/.venv/bin/python codebase/reward_harness/scripts/sample_rollouts.py catalog
codebase/reward_harness/.venv/bin/python codebase/reward_harness/scripts/sample_rollouts.py replay --run "$rollout_run"
codebase/reward_harness/.venv/bin/python codebase/reward_harness/scripts/sample_rollouts.py export \
  --run "$rollout_run" --bundle-evidence

# 第二个模型：复制 YAML，填写真实平台 model 标识；其余参数按实验需要明确设置。
# prepare 时使用 --task QwenTwentySevenBRollout，再执行 sample。
model_b_run='codebase/reward_harness/runs/YYYY_MM_DD_HH_MM_QwenTwentySevenBRollout'
codebase/reward_harness/.venv/bin/python codebase/reward_harness/scripts/sample_rollouts.py merge \
  --runs "$rollout_run" "$model_b_run" --bundle-evidence
```

| 文件 | 用途 |
| --- | --- |
| `rollouts.jsonl` | 每个逻辑 rollout 一行，含 pending / uncertain / failed / truncated；完整记录覆盖率与缺失，保留 prompt、最终回答、思考、模型、参数、usage、证据路径及 SHA-256 |
| `candidates.jsonl` | 默认只含 success，用于后续评分或数据处理；可以显式 `--include-truncated`，状态仍保留 |
| `by_prompt.jsonl` | 每个 sample_id 一行，含原始 prompt 和各模型的 `rollouts` 候选数组；没有合格回答时数组为空，便于配对比较 |
| `export.json` | 源运行清单、导出规则、行数、状态分布、按模型候选数和各文件指纹 |
| `evidence.tar.gz` | 可移交的输入、配置、每次请求/响应/历史及本次导出；通过 `sources/<run_id>/` 定位原始证据 |

合并必须来自相同 dataset version 和训练 prompt 文件 SHA-256；相同 sample_id 的 prompt 内容还会复核。重复传入同一 run 不重复计数；同一 rollout_id 的冲突副本会报错。合并不要求模型生成参数相同，参数始终逐行保留，比较时需据此分组。不会按回答文本去重，不会自动生成 chosen/rejected 偏好。

`merge` 在 runs 下新建 `YYYY_MM_DD_HH_MM_RolloutMerge`，不修改源运行。默认 JSONL 无额外依赖；已安装 pyarrow 时加 `--parquet`，同时写对应 Parquet 文件。导出前会离线重放 SSE，检查请求绑定、内容/思考/usage/状态、完整历史及哈希；损坏证据阻止导出。未提交的 attempt 会列为 incomplete，不会当作完整候选。

`status` 可在采样期间查看；此时未提交 attempt 可能仍在进行中，进程退出后才应视作 uncertain。`summary.json` 是快照，最新状态以 `status` 为准。token 用量按**每次物理请求最后的累计 usage**计，不重复累加流式 chunk；重试也统计，缺失或不完整的最终用量显式标为 unknown。未配置平台价格，费用为 null，不把未知费用当成零。

采样与导出各使用单写者锁；采样期间的 export / replay / merge 会拒绝并发修改。JSONL 导出本身可独立使用；需要完整回放时同时移交 evidence 包。脚本没有自动执行远端工具或模型生成代码的能力，也不提供生产执行器隔离验收。

## 验证范围

`tests/test_rollout.py` 的 19 项测试覆盖流式重组、跨 chunk 脱敏、累计 usage、断点恢复、请求预算、重试、thinking/模型异常、截断筛选、数据指纹、回放防篡改、跨模型合并和证据包。新增并发测试在 36 个完整流式响应中观察到同时在途请求峰值恰好为 32，且没有超过上限。它们是明确的离线 HTTP fixtures，产物标记 `evidence_kind=offline_fixture`；此前整个 harness 回归 238 项通过，本次配置调整及并发测试后 rollout 专项 19 项通过。单条真实接口验证单独标记 `real_http`，不能据此推断整批吞吐、并发额度或答案质量。
