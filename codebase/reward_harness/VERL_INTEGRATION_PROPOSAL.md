**Reward harness → verl 接入方案（讨论稿）**

日期：2026-09-30。本文提出待讨论的接口与实施方案，不替代当前 PRD，也不表示接入或训练验收已完成。本轮仅静态阅读源码并撰写方案。

后续实施范围已收窄为独立的薄 adapter：默认输入映射、复用现有评分及成功结果包装；不实现异常回传。入口和用法见 README 的“RL 框架 adapter（verl）”。下文的 bundle 导出、样本索引及完整训练生命周期仍是候选后续方案，不是本次已实现功能。

参考版本：本项目 `eb5d099`；本地 `/Users/andrewfeng/Desktop/verl-agent` 为 `f1c27141`。方案以该 checkout 的实际加载入口为准。

**1. 建议的合并边界**

把 harness 的成功产物导出为 verl 可加载的 reward 算子；harness 负责生成、验证、冻结及评分执行，verl 负责 rollout、token 解码、reward tensor 和 RL 更新。

```text
构建阶段：query → suite → reward components → 验证 → frozen bundle
训练阶段：verl rollout → compute_score → 输入映射与路由
                                   → 同一 runner / aggregator → score
```

建议首版使用 `reward.custom_reward_function` 接口和新版 `naive` manager。标准文本评分不需要修改 verl trainer。生成代码仍经 runner 执行；由工程实现的可信入口负责加载 bundle 和输入校验。

源码依据：

- `verl/trainer/ppo/reward.py:get_custom_reward_fn` 从 `config.reward.custom_reward_function` 加载函数，支持同步/异步及 `reward_kwargs`。
- `verl/experimental/reward_loop/reward_manager/naive.py:run_single` 调用四参数函数，并接受 scalar 或含 `score` 的字典。
- `verl/experimental/reward_loop/reward_manager/base.py:assemble_rm_scores` 把样本标量写入最后一个有效 response token。
- 旧 `verl/workers/reward_manager/` 仍存在，但当前 loader 使用新版 registry。顶层 `custom_reward_function` 有旧配置迁移逻辑；新方案直接使用 `reward.custom_reward_function`，避免混用新旧接口。

**2. 对外调用契约**

导出的训练入口建议为：

```python
def compute_score(
    data_source: str,
    solution_str: str,
    ground_truth,
    extra_info=None,
    *,
    bundle_dir: str,
    execution_run_dir: str,
    **runtime_kwargs,
) -> dict:
    ...
```

`bundle_dir` 和 `execution_run_dir` 由可信训练配置通过 `reward_kwargs` 注入；不得覆盖四个样本参数。`runtime_kwargs` 用于明确适配 verl 可能追加的 router/tokenizer 参数，不能直接暴露给生成的组件，也不能据此替换已冻结的 judge 模型。

建议从原始数据生成独立的训练 Parquet，保留以下字段：

```python
{
    "data_source": "openai/gsm8k",
    "prompt": [{"role": "user", "content": "实际提供给 policy 的问题及指令"}],
    "reward_model": {
        "style": "rule",
        "ground_truth": "构建阶段使用的原始参考解答，含推理和 #### 答案",
    },
    "extra_info": {
        "rlar": {
            "input_contract": "rlar.verl_input.v1",
            "dataset_id": "绑定源文件指纹的数据集标识",
            "query_id": "稳定样本标识",
            "reward_key": "冻结 reward 的内容摘要",
            "query": "构建阶段使用的原始 query",
            "metadata": {},
        }
    },
}
```

上面的 `input_contract` 是拟新增的适配协议标识，并非现有字段。`style=rule` 沿用 verl 自定义代码评分的数据格式，不据此决定组件是否调用 rubric judge。

| verl 输入 | harness 执行输入 | 约束 |
|---|---|---|
| `solution_str` | `response` | 使用约定解码方式得到的完整文本，不由桥接层提取最终答案 |
| `ground_truth` | `reference` | 类型及内容与构建契约一致，允许契约声明的 null |
| `extra_info.rlar.query` | `query` | 保留构建原文，不从套了 chat template 的 prompt 反推 |
| `extra_info.rlar.metadata` | `metadata` / 明确许可字段 | 做字段级白名单投影；保留字段名冲突检查 |
| `data_source`、`dataset_id`、`query_id`、`reward_key` | 可信路由与绑定校验 | 不默认传给生成代码 |

缺失必需字段、数据类型不符或绑定不匹配时明确失败，不用空字符串兜底。不要将整个 `extra_info` 作为 metadata 传入：其中可能包含工具配置、rollout 分数及非评分字段。

特别注意：本地 `examples/data_preprocess/gsm8k.py` 把 `ground_truth` 设为提取后的数字，同时把原始解答放在 `extra_info.answer`。直接采用该数据会改变现有 reference 的含义。建议新转换器从源数据写入原始 reference；已有 Parquet 若需导入，必须声明并记录 reference 来源映射，不能自动猜测。

`query` 与 policy 的 `prompt` 可以因固定格式指令而不同，但这种转换也应记录。适配器不负责猜测 query、删指令或拼接多轮消息。

**3. 是否改变模型生成的函数签名**

推荐首版保留组件 ABI：

```python
def score(example, context):
    # example: query / response / reference / permitted metadata
    # context: 受限 judge_spec / call_llm_api
    return {"raw_score": ..., "feedback": ..., "evidence": ...}
```

对外的完整 reward 使用 verl 的 `compute_score`；内部组件使用现有 ABI。两者分别服务于训练框架和受限组件执行。

这仍要求实质修改输入链路：新增严格的 verl 输入契约与共享映射器；开发测例、独立 score 和训练回调经过同一映射与白名单规则；合成者看到明确的字段类型、来源及训练时可用性；导出前验证两条调用路径一致。不能只在导出时临时拼一个字典。

这种方案保持已有 single/checklist、rubric context、结构化 evidence 和一次归一化语义。若要求“模型生成的组件源码本身也必须具有原生 compute_score 签名”，则需要同时版本化组件入口、worker 调用、静态准入、模板、schema、context 注入和历史兼容；仅改函数名不能完成。这是可选的后续 ABI 改造，接入 verl 本身不要求它。

**4. 样本如何找到正确的 reward**

当前 construction 按 query 产出结果；不能假设整个 `data_source` 自动共用一个已验证 reward。

建议 bundle 提供 `(dataset_id, query_id) → reward_key` 的不可变索引，并绑定 query/reference/许可 metadata 的摘要和 task pack。训练行携带的 `reward_key` 必须与该索引一致。相同定义可以按内容摘要去重保存，但验收适用范围仍独立保存。

`reward_key_for()` 当前明确不包含 query 和 reference，因此只校验 reward_key 无法证明样本匹配。新数据集上复用同一 reward 需要单独声明适用性和验证范围，不能仅按 data_source 自动回退。

**5. 返回值与失败语义**

入口返回固定键集合，例如：

```python
{
    "score": 0.75,
    "rlar_coverage": 1.0,
    "rlar_status": "ok",
    "rlar_reward_key": "...",
    "rlar_trace_ref": "...",
}
```

`score` 来自原 aggregator 的 `total_score`。完整 component results、feedback、evidence、模型调用和错误写入 trace，通过引用关联。不同 query 的组件数量可能不同，不宜直接把任意组件 ID 展开为返回键：本地 reward loop 从第一条结果取键集合，再索引后续样本；验证指标也会对非字符串值做数值统计。

不要默认返回 `acc=score`。checklist 的连续奖励不一定是任务正确率；若需要独立 accuracy，应另行声明定义。

| 情况 | 建议首版训练行为 |
|---|---|
| 完整成功且总分为 0 | 合法的 0 分，正常参与训练 |
| 完整成功且总分有限 | 返回总分，保持原等权聚合 |
| 部分组件失败 | 留存 partial 结果；默认拒绝本次训练评分 |
| 全失败、超时、缺 reward、无效输入 | 留存错误并抛出，不能伪装成 0 分 |

开发态现有“有效组件等权平均”不变；训练发布策略额外要求完整覆盖，避免不同 rollout 由不同组件子集评分。有限的基础设施重试耗尽后默认使当前训练调用失败。若要失败后继续训练，需另做明确的丢组/重采样方案，特别是 GRPO 不能随意删除组内一个样本。

本地 `rate_limited` manager 捕获异常和超时后返回 0；旧版 naive 也有 timeout→0 分支，均不满足上表。首版使用新版 naive，并在评分运行时处理必要的限流与重试。

**6. 冻结导出与运行生命周期**

建议新增 `export-verl`，导出：

- `adapter.py`：可信的标准入口。
- `manifest.json`：bundle 版本、文件摘要、源运行/验收引用、输入映射及返回策略版本。
- `rewards/`：冻结的 RewardDefinition，按内容摘要去重。
- `task_packs/`、`bindings.json`：许可输入、normalization、适用范围与样本绑定。
- 运行时依赖锁定信息和部署配置模板，不包含凭据、测例标签或开发期预算状态。

训练机器需要安装固定版本的 scoring runtime；首版不是单独拷贝一个 .py 就完全无依赖。每个 Ray worker 必须能读取同一 bundle；路径可变，内容摘要不能变。

从 evaluator/driver 中提取可独立初始化的 frozen scorer，复用 runner、policy、aggregate 和调用留档基础设施。训练评分不重新进入 construction、suite 合成或 harness-verifier。verifiable 不需要 LLM 客户端；rubric 只初始化冻结的 judge 配置及评分服务。

训练使用新的预算与证据运行，不接着消耗 construction 账本。所有新运行及导出放在本项目 `codebase/reward_harness/runs/YYYY_MM_DD_HH_MM_{PascalCaseTask}/`；训练源文件只读，原 construction trace 不覆盖。

并发必须显式处理：新版 reward loop 会并行调用样本；当前 JudgeUtility 的 bind_execution 有可变绑定，不能把同一个实例无保护地用于多个请求。首版可在每个 worker 内串行使用 scorer，以多个 worker 并行，或为每个调用创建独立执行上下文。不可并发追加同一份无协调 journal，也不能把进程内限流器当成跨 Ray worker 的总配额。共享模型预算应由单一服务/协调者预留与结算。

SubprocessRunner 仍是原型隔离等级。接入成功、行为一致和正式隔离验收是不同结论。

**7. verl 配置形态**

以下是待实现导出产物的配置示意，不是当前即可运行的命令：

```yaml
reward:
  num_workers: 8
  reward_manager:
    source: register
    name: naive
  reward_model:
    enable: false
  custom_reward_function:
    path: /deployment/frozen_bundle/adapter.py
    name: compute_score
    reward_kwargs:
      bundle_dir: /deployment/frozen_bundle
      execution_run_dir: /project/codebase/reward_harness/runs/YYYY_MM_DD_HH_MM_VerlRewardTraining
```

关闭的是 verl 自带 reward model 部署；harness rubric 仍可调用自身冻结的外部 judge。CLI 加入 YAML 默认未定义的 reward_kwargs 字段时，需要按 Hydra 的新增键规则处理。

**8. 分阶段 merge 与验收**

1. **输入契约和 frozen scorer**：统一输入构造、来源与白名单；抽出评分生命周期；保持现有合成和聚合行为。对版本变化明确迁移，不重写历史 manifest，不绕过 resume 指纹。
2. **bundle 与标准回调**：实现导出、绑定索引、数据转换器、compute_score 和配置；在本地 verl 的真实 loader/新版 naive 上做 CPU 接口测试。初始用单轮 verifiable 验证完整链路。
3. **rubric 与训练联调**：加入独立预算、完整真实调用留档、并发隔离及限流；再做小规模 RL smoke。若首版必须支持 rubric，则这一步也是首版完成条件。
4. **完整 agent 轨迹（按需求）**：必要时使用 verl 已有 importlib 扩展点，增加 RlarRewardManager；统一 messages、tool calls、observations 和执行状态的轨迹协议，并扩展 suite 的输入表达。

关键验收：相同规范化文本输入下，直接评分与 verl 回调的分项和总分一致；通过 tokenizer 路径验证解码后的真实文本、padding 和 reward tensor 位置；缺字段、错绑定、空生成、NaN、合法 0、partial 和全失败行为明确；混合任务输出键一致；并发无串样本/串 judge；导出在另一目录可加载且篡改被检出。

rubric 的工程一致性可通过保存的 judge 响应回放检查，但必须标为回放测试；不能把两次真实随机 judge 输出强行视为必然相等，也不能用回放代替真实接口联调。真实训练 smoke、真实模型调用和隔离能力分别报告。

**9. 需要讨论的范围选择**

目前建议：对外采用 verl 原生签名、内部保留组件 ABI；先按稳定 query 绑定冻结 reward；训练严格要求完整评分；单轮文本入口先接通。

如果目标是评价完整多轮工具轨迹，需要把它列为首版要求。本地新版 naive 对 multi-sequence 只取最后一条 sequence，并把 tool_extra_fields 合入 extra_info，这不保证获得完整结构化轨迹。还应防止工具字段覆盖保留的 rlar 命名空间；完整轨迹接入宜由自定义 manager 明确组织和校验输入。

另一个独立决策是“每条 query 的 reward”还是“每个任务共享 reward”。前者贴合当前结果绑定，后者需要增加跨 query 验证后再发布，不能仅修改 dispatch 规则。
