# RLAR submission 修订方案

日期：2026-09-23。状态：已核对当前 LaTeX、提交 PDF、图像和主表数字，并纳入作者确认。本文件是修订计划，未改动论文或运行新训练。

**作者已确认：** 主实验使用正文的 GSM8K / MATH / LeetCode / FLORES / UltraChat；agent 背后模型为 GPT-5.1；实验代码和记录不在本地；截止时间还剩 72 小时。补跑预算、远端记录的可访问方式和组内奖励工具选择粒度仍待确定。GPT-5.1 的确认只适用于 agent，不能据此替换数据处理或评测 judge 的版本。

**建议顺序：先恢复实验可追溯性，修复评测与奖励接口，再补足比较证据，最后重写主张。** 数据、parser、RewardBench 标签隔离和组内奖励协议属于结论有效性的前置条件，不能只靠措辞调整解决。

**建议定位：** 异构任务上的可复用奖励工具合成与路由系统，研究其效果、适用边界和总成本。是否可以主张更优性能、更低成本、动态适应和抗 reward hacking，应分别由对应实验决定。

## 1. 已核实的事实与需要纠正的批评

| 问题 | 当前证据 | 修订判断 |
|---|---|---|
| 数据描述冲突 | Table 1 / §4.1 与 Appendix D.1 是不同数据构成；附录还明确说不采用复杂数学任务 | 作者确认主实验以正文数据为准；D.1 应据此重写，具体版本/split/数量仍需日志核对 |
| `+61.9%` | `(47.16-29.12)/29.12`，对应 Qwen RLAR 相对 Base 的九基准复合均分增益 | 数学上可复算；叙述中容易被误读为相对强基线，必须明确比较对象并降低其标题地位 |
| GSM8K 排名 | Qwen：Human 95.30，RLAR 93.70，GPT5-Judge 91.96 | “RLAR 被 Human 和 GPT5-Judge 都超过”不符合现有表格；RLAR 低于 Human、高于 GPT5-Judge |
| GSM8K 主导增益 | 按当前汇总公式，GSM8K 贡献 Qwen 总增益约 38.0%；去掉 GSM8K 后，相对 Base 仍提升约 43.7% | 贡献很大，但并非全部或多数增益。分类 RM 的零分不进入这项相对 Base 的计算，不能说它们直接造成 `+61.9%` |
| 三个 RM 的 GSM8K 零分 | 论文将其归因为输出 `\\boxed{}` 而非 `####`，未提供完整重评分证据 | 确认异常存在；目前无法区分格式遵循问题、解析器故障、训练奖励故障与 reward hacking |
| RewardBench 标签泄漏 | 用公开 top-50 模型记录构建工具池并评分，未交代 prompt、metadata、调参和测试标签的边界 | 存在风险，但“离线评分使用测试标签”本身不是泄漏；需要审计标签是否影响选择过程 |
| 单次运行 | checklist 明确写明结果来自 single run | 已确认，不能把小分差表述为统计显著 |
| Router 指标缺失 | 有最终 RewardBench 结果和 tool executability，但没有 completeness 判断、合适工具选择、路由 regret 等指标 | 已确认；94.9% 可执行率不等于奖励语义正确率 |
| 模型命名 | 在线实验写 GPT-5；RewardBench scaling 写 GPT-5.1；复现说明写主要 GPT-4.1 | 作者确认 agent 为 GPT-5.1；其他角色独立核对，不能全局替换成同一名字 |
| 相关工作 | 已引用 RewardAgent，缺少实证对照；未见 EUREKA 引用 | 补定位与可比实验，不能仅靠“GRPO 而非 DPO”支撑新颖性 |
| GRPO 奖励协议 | 方法允许 router 读取单个 candidate，未交代 group 内是否冻结工具和工具版本 | 核心实现未知，需要先查看实际调用链 |
| 图表 | Fig. 3 无数值或误差线；Figs. 5、7 都加载 `adv_max_qwen.png` | 已确认。Fig. 7 图与解释不对应 |

核查来源：[主实验源码](/Users/andrewfeng/Desktop/overleaf_projects/202605_RLAR/Chapters/5_experiment.tex)、[数据附录源码](/Users/andrewfeng/Desktop/overleaf_projects/202605_RLAR/Appendix/A_data_details.tex)、[方法源码](/Users/andrewfeng/Desktop/overleaf_projects/202605_RLAR/Chapters/4_method.tex)、[附加分析源码](/Users/andrewfeng/Desktop/overleaf_projects/202605_RLAR/Appendix/G_additional_analysis.tex)。

### 现有数字支持怎样的结论

原表实际上对九个 benchmark 等权：四个数学、两个代码、两个翻译、一个对话；每个翻译 benchmark 先取 `0.5 BLEU-2 + 0.5 LLM-Judge`。这不是四个领域等权。

| 汇总方式 | Llama：RLAR | Llama：GPT5-Judge | 差值 | Qwen：RLAR | Qwen：GPT5-Judge | 差值 |
|---|---:|---:|---:|---:|---:|---:|
| 原九基准均分 | 41.73 | 39.89 | +1.84 | 47.16 | 45.85 | +1.31 |
| 去除 GSM8K，剩余八基准等权 | 37.74 | 35.88 | +1.86 | 41.34 | 40.09 | +1.25 |
| 四领域等权，领域内基准等权 | 46.06 | 45.76 | +0.30 | 50.73 | 50.45 | +0.28 |

以上均由发表表格中已四舍五入的数字重算，用于诊断，不是新实验，也没有置信区间。原均分中 RLAR 对 Human 的优势为 Llama +8.16、Qwen +2.76 分，因此“所有强基线都只差 1–3 分”也不准确。

下一版应以逐任务结果及相对强基线的差值为主体，保留并准确标注原九基准汇总，增加四领域等权和 leave-one-benchmark-out 敏感性分析。不要事后挑一个最有利的汇总方式。不同任务都放到 0–100 并不意味着这些分数具有相同效用；复合均分是约定的描述性指标。

## 2. P0：先修复可信度与有效性

### P0-A：建立真实实验清单，清理两个版本的混用

由原始配置、样本 ID 和 run ID 建立唯一的 experiment manifest，至少包括：

- 数据集仓库、版本、split、训练/验证/测试数量、过滤规则、抽样 seed、语言对与混合权重。
- 训练和评测 prompt、参考答案来源、是否生成 GPT 参考、是否存在 SFT、SFT 与 RL 各自的数据。
- policy checkpoint、tokenizer/chat template、Qwen thinking 模式、agent 和 evaluator 的精确 API model ID。
- RL steps、epochs、prompt batch size、每 prompt rollout 数、minibatch、采样与解码配置、代码 commit。
- 每张表和图对应的 run ID、产物路径、评分脚本及生成脚本。

作者已确认主实验使用正文数据，因此 D.1 将围绕 GSM8K / MATH / LeetCode / FLORES / UltraChat 重写，补上仅作测试的 AIME、MBPP、WMT-24 的来源与划分，并同步清理旧语料相关描述。旧语料若确有独立工具生成实验，则单独命名、列明样本量与用途；否则删除对应旧分析。不能因主体数据集已确认，就推定工具生成统计和曲线也属于同一实验。若主表与日志无法对应，该部分不能作为已验证结论继续使用。

必须一起查清：

- FLORES-101 / FLORES-200 的冲突。
- §4.1 将 WMT-24 写入训练 downsampling，与 Table 1 的 train=0、OOD 声明冲突；若真实参与训练，必须取消相应 OOD 主张或另设独立测试集。
- Table 1 训练数合计为 11,085，与附录“8,000+ training samples”是否属于同一实验。
- “uniformly downsample”与各来源不同样本量是否分别指抽样概率和数据规模。
- 附录文字 batch=128，超参表和成本段 batch=256；80 steps 与 5 epochs 的关系。
- “Base/Zero-Shot”是否就是 RL 的起点，还是 RL 前还做过两轮 SFT。若后者，补 SFT-only 对照，避免把 SFT 收益计入 RLAR。
- 图例的 `qwen3-0.6`、约 100 步曲线与 Qwen3-8B、80 steps 的对应关系；不能只改图例。
- MBPP 974 个评测样本、MATH 500 子集、AIME 各 30 题的确切来源和划分。
- 翻译同一源句的多语言版本必须按源句分组划分，避免跨语言对的训练/测试重叠。
- 表头 NEM、Pass@k、pass@1、Best@1 的定义，尤其 MATH 被标成 Pass@k。
- 工具清单的“Top@9”标题与实际 10 个模型，以及 Seed-X 的 7B/8B 名称冲突。

**验收：** 正文、附录、表格、图、代码配置由同一清单对齐；每项主结果均可定位原始产物。此阶段不需要重训，但可能决定哪些实验必须重做。记录不在本地不等于已经丢失；优先由能访问远端的作者或合作者执行核查，并提供 run ID、配置与重评分汇总。

### P0-B：查明 0.00，再决定是否保留 hacking 论点

优先对已保存输出进行统一重评分，覆盖 RLAR、三种分类 RM、GPT-Judge、手工奖励与 Base，不能只修正某一方法。

1. 将数学评测拆为：严格格式遵循率、与格式无关的答案正确率、同时满足二者的比例、解析失败率。区分空输出、截断、格式不符与答案错误。
2. 使用确定且公开的答案提取规则处理 `####`、`\\boxed{}`、负数、分数、多个中间数和最终答案；对所有模型使用相同规则。抽样盲审解析失败与解析器不一致样本，报告数量及一致性。
3. 对语义相同的答案仅替换格式，比较 parser 输出和 RM 分数；同样用固定格式替换正确/错误答案，验证 reward 是否重内容。
4. 沿调用链检查 raw reward、后处理 reward、`<think>` tag gate、异常默认零、chat template、截断、tokenizer、NaN、批次索引和答案提取。三个 RM 共用的封装器应优先审计。
5. 若有 checkpoint/训练轨迹，检查训练 reward 是否上升而独立正确率/约束满足率下降。仅观察到格式迁移或长答案不足以认定策略在利用奖励漏洞。

**按结果采取行动：** 纯评测 parser 错误可先重评分并更新所有相关数字；训练奖励代码错误必须重训受影响条件；真实格式不遵循应报告为独立现象。只有对照和轨迹支持代理奖励被利用时，才保留特定的 reward-hacking 结论，且限定在已测攻击条件。

verbosity 部分同理：长度差异只是观察。若保留 bias/hacking 主张，增加语义相同、长度不同的对照，或独立质量评测与长度控制分析；报告长度分布和截断率，而不是从平均长度直接推出漏洞。

### P0-C：明确 GRPO 的奖励单位、工具选择粒度与更新时机

先确认实际实现属于哪种情况，再决定说明或改算法：

| 实现情况 | 解释与修订 |
|---|---|
| 同一个 prompt 的所有候选使用同一工具及版本 | 不要求不同 prompt 的奖励都处于同一数值范围。给出组内不变性及边界，重点审计零方差、epsilon、负向指标和更新顺序 |
| 同组候选分别选用不同工具 | 组内标准化不能消除工具间偏移/语义差异。优先改为每组共享同一评价函数并重做受影响实验；若保留候选级路由，必须有独立校准和验证 |
| 对每个候选都使用相同的一组工具并固定组合权重 | 必须在组合之前处理各工具尺度，否则大尺度工具会支配加权和。组合参数用训练/开发数据确定 |

推荐新增明确算法框：先依据 query、允许使用的训练参考和任务元数据选择/合成工具；验证后固定该组的工具版本、校准参数和组合权重；对全部候选评分；计算 advantage；完成使用该 rollout batch 的优化后，才允许下一批启用新版本。新增设计必须标为修订后的实现，不能写成原实验已经采用。

若原路由器读取候选内容，需报告并分析这一选择；可改为生成前的 query-level routing，或使用对候选顺序不敏感的 group-level routing。生成的 verifier 也不能为某个候选单独定制打分规则。

在同组共享、且没有额外裁剪或门控时，若 `r'_i = a r_i + b` 且 `a > 0`：

`A'_i = (r_i - mean(r)) / (std(r) + epsilon/a)`。

因此只有 epsilon 为零或可忽略时，正仿射变换才严格或近似抵消。任意非线性映射、异工具逐候选评分、零方差以及后置格式 gate 不在这一保证内。全组同分时 advantage 应有限、可解释，并报告零方差组比例；不能靠人为放大这些组制造训练信号。

工具接口表需列：输出范围、越大/越小越好、校准方法、参考答案要求、异常语义和 fallback。异常不能悄悄混同为答错零分；策略应对整个 group 一致且有日志。手工翻译奖励中的 BLEU 与 normalized SeedX 也必须核查范围。

跨训练步骤更换工具不必然破坏组内归一化，但改变了优化目标，GRPO 不能自动保证这种非平稳目标的稳定性。至少比较冻结工具库与按批更新工具库，记录切换率、固定 probe 集上的奖励漂移/排序变化、零方差比例、KL 和评测曲线。若要保留“适应变化分布”的强主张，另需预先设定的任务分布切换实验。

同时修正 [Preliminaries](/Users/andrewfeng/Desktop/overleaf_projects/202605_RLAR/Chapters/2_preliminary.tex) 中的目标公式：`min(ratio * A, clip(ratio) * A)` 不能一般写成 `min(ratio, clip(ratio)) * A`，负 advantage 时二者不同。核对 token/sequence 粒度和参数下标，并区分 reward model 与 value model。公式错误不等于实际 verl 实现错误，应分别查证。

**验收：** 可复现同组评分与版本冻结；有限且正确的 advantage；明确非平稳性边界。若协议变更影响训练奖励，相关结果需要重训。

### P0-D：重建 RewardBench 的无泄漏评测协议

- 保存当时实际给 router 的 prompt、metadata、候选顺序与工具池构建过程，审计是否包含 chosen/rejected 标签、逐题 pass/fail、同测试集领域分数或针对测试题的工具建议。
- 测试标签只能用于最后离线计分和事后 oracle；不能进入 routing、synthesis、prompt 选择、温度校准或模型池筛选。候选匿名化并随机打乱，报告顺序敏感性。
- 在独立开发集确定工具池、路由 prompt、校准与 ensemble 权重；冻结后测试。若公开 leaderboard 的测试结果已用于构池，单纯在同一测试集内再切分不足以消除先前选择影响：保留为公开 benchmark 上的回顾性分析，并补独立 held-out 数据验证。
- 以明确、可复现的 benchmark 官方评分协议为主；当前“preferred softmax probability > 0.5”依赖 logits 尺度，不等价于“正确答案排名第一”。核实它是否为官方协议；若为自定义，单列并解释，不直接与官方榜单分数混称。
- 补 best-single（由开发集选出）、随机选择、任务/元数据规则路由、经开发集校准的 ensemble；未校准的 raw-logit mean@50 不能单独证明 ensemble 无效。
- 将 “Theoretical Best / upper bound” 改为 **固定候选池上的事后 oracle**。这是使用测试标签计算的特定池、特定指标下的参考上限，并非可部署算法或普适理论上界。
- 报告实际分母、每类样本数、排除 Tie 的规则与排除顺序。当前“400 个实例”与百分比需从整数通过数复算，区分 micro 与 macro 聚合。

在同一测试协议下，以 `Y_ij` 表示工具 j 对题 i 是否通过，则 selection success 为 `mean(Y_i,selected(i))`，事后 oracle 为 `mean(max_j Y_ij)`；多个工具通过时不强行定义唯一正确工具。

**验收：** 测试标签与选择流程隔离、prompt 可审计、工具池与评分分母可重现。若不能补独立验证，应降低此实验的地位，删除“验证可靠逼近理论上限”等强结论。

## 3. P1：补足统计、创新与效率证据

### P1-A：预先固定主要比较与统计方案

主要比较建议设为 RLAR vs GPT-Judge、task-specific hand-crafted reward、RewardAgent；另保留修复后的静态 RM 对照。把“Human”更名为 **Hand-crafted task-specific reward**，避免误以为是人类评分或人类表现上限。

- 每个关键条件至少 3 个独立训练 seed，预算允许时采用 5 个。使用同一组预定 seed 和一致的数据、初始化、训练预算；不因结果不理想而选择性增加 seed。
- 训练重复是否重新运行 router/synthesis 必须明确。固定一个工具库只测到 policy 训练方差；如主张端到端系统稳定，应让工具构建/agent 随机性进入独立系统运行，或另外报告这种方差。
- 表格给 mean ± SD、seed 数；主要成对差值给 95% CI，并解释 CI 捕捉哪些随机性。3 seeds 的训练方差估计仍很粗，不能承诺一定得到显著差异。
- 在相同 prompt 上成对重采样，分层保持 benchmark 和语言对权重；如同时测训练随机性与样本不确定性，可做保留配对结构的分层/层级 bootstrap。不能把同一模型生成的多个候选当成独立训练重复。
- AIME 每年仅 30 题，1 题就是 3.33 个百分点：报告答对题数及适当区间，不把 1 题差距视为稳定优势。增加采样次数也不能等价替代更多独立题目。
- 若 BLEU 为 corpus BLEU，重采样后重算 corpus 指标，不能直接平均句子 BLEU；同时明确 BLEU-2、分词器、大小写和 smoothing。
- 以冻结的主要 endpoint 做推断，次要大量比较使用适当多重比较控制，或明确为探索性分析。独立评测 judge、盲化模型身份和固定 rubric 可减少训练 judge 与测试 judge 共享偏好造成的混淆。
- 若 CI 覆盖 0，只能说“尚无明确优越证据”；这也不是统计等价的证明。预算不足时可提供基于现有输出的 CI，但必须注明不能反映重训方差。

### P1-B：直接测 Router 和工具语义质量

从训练外样本构建审计集，覆盖已有工具可用、缺失工具、边界/混合任务。先冻结工具库，给出独立标注的可接受工具集合，标注分歧与裁决规则。

| 能力 | 指标/对照 |
|---|---|
| 是否应调用 synthesis | 缺工具判断的 precision、recall、F1；不必要 synthesis 率、漏触发率 |
| 工具选择 | 可接受工具集合命中率、chosen-tool success、相对事后 oracle 的 regret、fallback 率、各任务分解 |
| 代码工具 | executability 与 semantic correctness 分开；测试正确/错误、格式变化、空输出、边界输入，报告误接收/误拒绝 |
| Web wrapping | 与原模型官方评分实现的一致性、部署成功率、失败原因、时延和成本 |
| 可复用性 | 去重后工具数、缓存命中率、工具复用次数、跨任务适用范围；避免把重命名脚本计为新工具 |

对合成 verifier 做开发测试与独立验收测试，不能只用 agent 自己生成的少量用例证明正确。单次 synthetic triplet 返回数字只支持“能执行”。

关键消融：相同工具库下 agent router vs 任务元数据规则 router vs 随机 router；冻结初始/已构建工具库 vs 动态合成；w/o CodeVerify、w/o WrapLLM。共享可比较的工具资源、训练步数与评测协议，并报告成本，避免把“工具更多”和“选择更准”混为一个因素。

现有 118 个脚本、21 个 repo、94.9% executability、96.4% LLM 工具调用率，以及 Fig. 4 Sankey 都必须先验证来自哪套语料。若它们来自旧语料，应明确为独立实验或重做；不能作为当前四领域 RL 的机制证据。

### P1-C：完整成本账本和公平效率比较

重算成本时覆盖：数据过滤与 reference 生成、router、检索、工具代码生成、验证、修复重试、模型下载/加载、reward inference、policy rollout/training、缓存和 fallback。数据准备、一次性建库、每次训练、最终评测分别列出，说明哪些在方法间共享。

每类 API 调用报告 model ID、调用次数、输入/输出/缓存/可计费 reasoning tokens、失败重试和计价日期；本地计算分别列 GPU 型号、数量、活跃 GPU-hours 和 wall-clock。H100 与 A100 小时不能不加区分地混合解释，外部 API 隐藏算力也不能视为零。

同时给 cold-start 总成本、warm-cache 单次成本和多次复用的摊销公式：

`C_amortized(K) = C_build / K + C_router + C_reward + C_policy`。

K 的设定要来自明确部署/复用情境。原 120M vs 23.7M tokens、288 vs 72 GPU-hours 暂视为待审计数字，不能在未补账前继续声称端到端省 80%/75%。

比较既要有相同训练步数/rollout 预算下的质量与费用，也应尽可能给 cost–quality 曲线或达到预先固定质量目标的成本。若收益主要是成本，论文可据此定位，但必须由完整账本支持。

### P1-D：EUREKA 与 RewardAgent

- 阅读并准确引用 EUREKA，比较自动奖励代码生成、反馈迭代、环境与任务、动态检索和工具复用；避免声称“首次让 LLM 设计新的奖励函数”。该文的环境和任务不同，不能未经适配直接要求同一套 LLM benchmark 分数。
- 重新核对已引用的 RewardAgent 原文与公开实现，明确其工具/验证机制和 RLAR 的差别，避免把差别压缩为 GRPO vs DPO。
- 优先运行原生 reward-scoring 对照；在可兼容前提下，把 RewardAgent 奖励接入同一 GRPO pipeline，使用相同 policy、数据、judge/agent 预算和训练条件。任何适配都说明清楚，不能把自定义削弱版本称为官方方法。
- 若两者支持的任务范围不同，在交集上做直接比较，并另外报告覆盖率、fallback 与成本。若实现确实不可获得或不可复现，解释具体障碍并缩小比较主张，不能用单纯引用代替实验。

相关工作修改位置：[Related Work](/Users/andrewfeng/Desktop/overleaf_projects/202605_RLAR/Chapters/7_related_work.tex) 与 [bibliography](/Users/andrewfeng/Desktop/overleaf_projects/202605_RLAR/custom.bib)。本轮是修订计划，未完成外部文献或 RewardAgent 实现复现。

## 4. P2：图表、数学解释和叙事重写

- **Fig. 3：** 用逐任务数值表加分组点图或差值图替代 radar。主文可画相对 full RLAR 的变化与 CI，附录保留所有原始值及 seed 数；没有重复实验就不画伪造的误差线。
- **Figs. 5–7：** Fig. 7 明确复用了 Fig. 5，应删除或根据真实需要另画概念示意，并标为示意而非实验。优先验证现有曲线是否来自本次实验，再决定是否保留。
- **Advantage 解释：** PPO/GRPO 标准 clipping 作用于 policy probability ratio，不是由 advantage 达到某阈值触发。删除“更大 absolute advantage → 更高 clipping → 更好学习”的因果推断。更高 clipping rate 也不自动代表更好。
- **曲线重绘：** 使用实际 trace、统一可解释的坐标范围、标明组大小和标准差定义；必要时同时给全范围与明确标注的局部放大。折线图截断纵轴本身并非一律错误，但不能掩盖绝对差异。补真实 ratio clip fraction、KL、零方差组比例与性能曲线，而非只画 min/max。
- **Fig. 4：** Sankey 当前显示摘要、写作和 En–Fr/Fr–En 等旧语料来源，必须与数据审计同步处理。
- **主表：** 修正 best/second-best 高亮。Qwen T&G 的 RLAR 63.03 高于 Human 62.32，但当前把 Human 标成第二。逐列自动生成高亮并统一并列规则。
- **主张：** 摘要和引言明确比较对象、绝对分差与不确定性；删去无法支持的“across all domains”“without degradation”“near-saturated”“confirms robustness”等表述。Llama RLAR 的 UltraChat 67.03 低于 Base 68.97，不能写各任务都无下降。
- **模型说明：** 增设角色—版本—用途表，分别记录 policy、agent、数据过滤/reference、训练 judge、最终 evaluator 和 RewardBench scaling model；用日志确认，而非统一名字。
- **Checklist/LLM disclosure：** 与最终证据一致，统计回答只在补足后调整。根据实际写作过程审阅“LLMs 未用于 data analysis 或 substantive technical content”的绝对声明，本轮已使用 AI 做数值核查和修订规划。

建议结构：方法中增加 group-level 评分及更新算法；实验中依次交代可追溯 setup、主要比较、路由与合成评估、成本；robustness 放独立诊断小节；附录保留版本、逐 seed 结果、原始表、prompts、工具语义测试和失败样例。

## 5. 执行顺序与实验预算档位

| 阶段 | 工作与产物 | 依赖/完成标准 |
|---|---|---|
| 1. 审计 | experiment manifest、问题清单、所有表图到 run ID 的映射 | 主数据和 agent 版本已确认；继续找到远端日志；无需重训 |
| 2. 修复与离线重评 | parser 分解评测、真实汇总、RewardBench 标签审计、组内评分协议 | 确认哪些结果可复用、哪些条件必须重训 |
| 3. 冻结实验协议 | 主要 endpoint、对照、seed、预算、checkpoint 选择和成本口径 | 在查看新结果前确定；修复后的实现不再边跑边改 |
| 4. 关键补跑 | 多 seed 主比较、RewardAgent、关键路由/合成消融 | 保存逐样本输出、完整成本和中间轨迹 |
| 5. 重写与验收 | 新主表/图、CI、证据匹配的论点、可复现附录 | 全文交叉核对、LaTeX 编译及 PDF 视觉检查 |

可选择的预算档位，尚未获作者确认，也未启动训练：

- **只能修文和重评分：** 完成阶段 1–2 与对应重写；保留 single-run 限制，撤去统计显著、普遍抗 hacking 和未经验证的动态稳定性主张。这能修复呈现与部分评测问题，不能解决训练随机性或缺失的关键对照。
- **72 小时内优先争取的补证据方案，取决于预算和单跑时长：** 一个预先指定的主要 backbone 上，RLAR、GPT-Judge、手工奖励各 3 seeds，先完成 9 个完整运行；RewardAgent 接入通过正确性核查后增加 3 个，形成 12 个运行的主比较。再补第二个 backbone 的 RLAR、GPT-Judge 各 3 seeds，共 18 个。限制在较窄的跨 backbone 主张；控制消融优先在主要 backbone 上做。每一层只有资源允许且有足够分析时间时再进入，不预设都能完成。
- **充足预算下的完整方案：** 两个 backbone × 四个关键方法 × 3 seeds = 24 个完整运行；主要 backbone 再做 w/o CodeVerify、w/o WrapLLM、简单任务路由、冻结工具库四个条件 × 3 seeds = 12 个，共 36 个。另做离线 RM 重评分、Router 审计和 RewardBench 重评。各控制条件必须设计成能隔离目标因素。此档不是当前 72 小时内的默认承诺。

以上是应具备的总运行数，不是一定要新增的数量。仅当原有运行通过审计、与冻结协议完全一致时才能计入；方法或训练奖励已修复的旧运行不能混入新 seed 汇总。先用一次可核算的运行测量实际时长与费用，再确定排期；目前不能根据未审计的 72 GPU-hours 给出可靠预算。

### 72 小时安排与决策节点

下列为执行窗口，训练能否放入窗口由远端实测决定。当前只制定方案；作者要求先不要着急，因此不因截止时间主动启动计算作业。

| 剩余周期中的时间 | 主要工作 | 应做出的决定 |
|---|---|---|
| 0–12 小时 | 远端配置/输出审计；确认 group reward；定位零分来源；确定真实数据版本和各模型角色；本地准备修文清单 | 哪些结果可通过重评分修复，哪些需要重训；预算、负责人与关键对照是否可落地 |
| 12–36 小时 | 修复后的统一重评分；Router/RewardBench 离线审计；若资源具备，启动预先确定的关键多 seed 比较；并行整理数据/方法附录 | 在第 36 小时根据实际吞吐和正确性状态冻结补跑规模，避免不断追加实验 |
| 36–60 小时 | 完成已安排运行、统计与成本汇总；重做图表；根据证据决定保留、收窄或删除论点 | 在第 60 小时冻结数字和论点；尚未完成或未核查实验不作为确定结果写入 |
| 60–72 小时 | 全文与附录一致性复核、引用与 checklist、编译与 PDF 检查、合作者交叉检查 | 提交可追溯的版本，保留最后修错缓冲 |

不应为了赶时间而先补上假定实现细节，或只重复 RLAR 而不给主要对照同等验证。若第 12 小时仍无法取得日志或第 36 小时确认补跑来不及，转入有限证据版本：明确单次运行、删除未被证明的机制/robustness/成本主张，完整修复已知文本错误，并标明仍未解决的有效性问题。

## 6. 已确认事项与待确认问题

**已确认：**

1. 主 RL 使用 GSM8K / MATH / LeetCode / FLORES / UltraChat。
2. Agent 使用 GPT-5.1；代码与日志不在本地。
3. 距截止还有 72 小时，先完成方案，不急于启动实验。

**当前待答的两组问题：**

1. 同一 prompt 的多个候选是否共用同一奖励工具及版本，还是逐候选路由？
2. 是否有作者/合作者可在这 72 小时内核查远端记录并安排补跑，可用 GPU/API 预算大致是多少？

**下一步从远端记录确认的数据与模型细节：** FLORES 版本和具体 split；WMT-24 的训练字段是否只是笔误；旧语料对应分析是否有独立实验；GPT-5.1 的确切 API ID；数据过滤、reference、训练 judge、最终 evaluator 的版本。

**从代码/日志中优先确定；若不可取得，再由作者补充：**

- Router 是每 prompt、每 group、还是每 candidate 调用？synthesis 是否依赖当前候选？一个 group 是否可能跨工具或版本？
- RewardBench 的 router 是否见到标签、chosen-first 顺序、逐题记录或同测试集统计？top-50 池、prompt 与参数如何确定？
- 0.00 使用的数学 parser、共同 wrapper、`<think>` gate 的实际实现是什么？原始生成是否还在？
- Base/SFT/RL 的起点关系，是否所有方法共享相同数据与初始化？
- 工具生成统计、Sankey、advantage 曲线分别来自哪个实验？
- 23.7M tokens 是否已经包括 router、检索、代码生成、验证与重试？GPU-hours 是否含 A100 奖励推理集群？

这些事实未明确前，不把它们补写为“已实施”的方法细节，也不推断零分必然是 bug 或 RewardBench 必然已经泄漏。

## 7. 修订完成的判定

每个保留的核心论点必须有相应证据：性能对应公平对照和不确定性；路由对应独立选择质量及简单对照；合成对应语义正确性和冻结库消融；效率对应完整总成本；动态适应对应分布/工具变化实验；hacking robustness 对应受控漏洞测试。

若补实验不支持某项主张，删除或收窄该主张即可，不预设 RLAR 必须在所有任务获胜。最终产物应是一篇设置一致、比较可复现、证据边界清楚的论文。
