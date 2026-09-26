# AIHub Claude API 可用性检查

检查时间：2026-09-24 15:40–15:43（北京时间）。使用本项目 `cc_start.sh` 已有 API Key，发送内容仅为 `Reply OK only.`。结果只代表该 Key 在测试时刻的可用性；文档标为“已接入”不等于当前有可用账号。

## 结论与修复

原配置固定 `provider=anthropic`，通过 `http://api.aihub.woa.com/v1/messages` 调用 `claude-opus-5`，实测 HTTP 500、`PlatformNoAvailableAccount`、错误码 `1011`：暂无可用供应商账号。初始失败 trace_id：`BEHzYZ2Ab4sio58mT5g2W4`。

`cc_start.sh` 使用标准协议 `http://api.aihub.woa.com/standard`，认证使用原有 `AIHUB_API_KEY`，默认模型仍是 `claude-opus-5`。15:40 曾验证自动路由成功；17:11 发现自动路由会选到仅支持透传的 `aws_mantle` 并报 400，现改为默认固定 `aws_third`，可用 `AIHUB_PROVIDER` 覆盖。脚本具有 Bash shebang 与当前用户可执行权限。

## 已验证可用的组合

| Claude 版本 | 调用模型 ID | 供应商 | 调用方式 |
|---|---|---|---|
| Opus 5 | `claude-opus-5` | AWS Bedrock `aws_third` | 标准 Messages 自动路由 |
| Opus 5 | `anthropic.claude-opus-5` | AWS `aws_mantle` | `/anthropic/v1/messages` 透传 |
| Sonnet 5 | `claude-sonnet-5` | AWS Bedrock `aws_third` | 标准 Messages 自动路由 |
| Opus 4.8 | `claude-opus-4-8` | AWS Bedrock `aws_third` | 标准 Messages 自动路由 |
| Fable 5.1 | `claude-fable-5-1` | AWS Bedrock `aws_third` | 标准 Messages 自动路由 |
| Fable 5.1 | `claude-fable-5-1` | Microsoft Azure `azure` | `/anthropic/v1/messages` 透传 |
| Fable 5 | `claude-fable-5` | Microsoft Azure `azure` | 标准 Messages 自动路由 |

以上请求均 HTTP 200，并返回文本 `OK`。自动路由结果的供应商来自响应的 `model` 字段。AWS Mantle 与 Azure 透传结果来自明确指定的 provider。

## 启动与切换

在项目目录执行：

```bash
./cc_start.sh                         # 默认 Opus 5
./cc_start.sh --model claude-fable-5-1
./cc_start.sh --model claude-sonnet-5
./cc_start.sh --model claude-opus-4-8
AIHUB_PROVIDER=azure ./cc_start.sh --model claude-fable-5
```

`--model` 切换主会话模型；脚本里原有的 Opus/Sonnet/Haiku 别名映射及子代理模型保持原配置，子代理仍指定 Opus 5。

标准协议的核心配置（示例不含密钥）：

```bash
export ANTHROPIC_BASE_URL="http://api.aihub.woa.com/standard"
export ANTHROPIC_AUTH_TOKEN="${AIHUB_API_KEY}?provider=${AIHUB_PROVIDER:-aws_third}"
export ANTHROPIC_MODEL="claude-opus-5"
```

当前默认固定 `${AIHUB_API_KEY}?provider=aws_third`，用于 Opus 5、Sonnet 5、Opus 4.8、Fable 5.1。Fable 5 使用 `AIHUB_PROVIDER=azure`。本次自动路由出现协议不兼容的供应商选择，不能仅凭一次成功假设它会持续正确选路。

AWS Mantle 透传须使用 base URL `http://api.aihub.woa.com/anthropic`、认证后缀 `?provider=aws_mantle`、模型 ID `anthropic.claude-opus-5`；Azure 透传使用同一 base URL、认证后缀 `?provider=azure`、模型 ID `claude-fable-5-1`。这些透传组合已验证最小 Messages 请求，未单独验证完整 Claude Code 工具调用。

## 本次不可用的组合

| 供应商 | 模型 | 结果 |
|---|---|---|
| `anthropic` 原厂 | Opus 5、Fable 5.1 | HTTP 500 / 1011：暂无可用供应商账号 |
| `google-vertex` | Opus 5、Opus 4.8 | HTTP 500 / 1011：暂无可用供应商账号 |
| `azure` | Opus 5 | HTTP 500 / 1011：暂无可用供应商账号 |

Azure 的 Opus 5 失败不影响已验证成功的 Fable 5／5.1。不能从一次失败推断某供应商已整体下线。

## 验证范围

- 本机 Claude Code 版本：`2.1.278`；脚本通过 Bash 语法检查。
- 修复后的脚本已通过 Claude Code Opus 5 和 Fable 5.1 连通性测试，两个进程均退出码 0，`is_error=false`，返回 `OK`；测试保留脚本的 high effort 设置。
- CLI 测试在临时目录进行，启用 safe mode、禁用工具与会话持久化，避免加载项目材料；未验证用户插件、MCP、长上下文和完整编码工作流。
- AIHub 模型广场页面需要 iOA 登录，本次供应商接入信息来自 iWiki，实际可用性来自 API 实测。

## 文档与入口

- [Claude Code 接入 AIHub 2.0](https://iwiki.woa.com/p/4032163148)
- [调用协议与端点](https://iwiki.woa.com/p/4032161389)
- [Opus 5：接入供应商与透传路径，2026-09-23 更新](https://iwiki.woa.com/p/4029828135)
- [Fable 5.1：接入供应商与参数，2026-09-23 更新](https://iwiki.woa.com/p/4038534281)
- [Sonnet 5](https://iwiki.woa.com/p/4031870218)
- [Fable 5](https://iwiki.woa.com/p/4031533230)
- [模型广场](https://aihub.woa.com/models) · [API Key](https://aihub.woa.com/resource/apikey) · [容量与平台负载](https://aihub.woa.com/observability)

## API 实测记录

| 时间（北京时间） | 模型 | 请求供应商 | 协议 | HTTP | 返回模型或错误 |
|---|---|---|---|---|---|
| 15:40:30 | `claude-opus-5` | `auto` | standard | 200 | `aws_third/anthropic.claude-opus-5` |
| 15:40:30 | `claude-opus-4-8` | `auto` | standard | 200 | `aws_third/anthropic.claude-opus-4-8` |
| 15:40:30 | `claude-sonnet-5` | `auto` | standard | 200 | `aws_third/anthropic.claude-sonnet-5` |
| 15:40:33 | `claude-fable-5-1` | `auto` | standard | 200 | `aws_third/anthropic.claude-fable-5-1` |
| 15:41:04 | `claude-opus-5` | `azure` | anthropic-prefix | 500 | `PlatformNoAvailableAccount / 1011` |
| 15:41:04 | `claude-opus-5` | `google-vertex` | standard | 500 | `PlatformNoAvailableAccount / 1011` |
| 15:41:08 | `claude-fable-5` | `auto` | standard | 200 | `azure/claude-fable-5` |
| 15:41:12 | `anthropic.claude-opus-5` | `aws_mantle` | anthropic-prefix | 200 | `claude-opus-5` |
| 15:41:34 | `claude-fable-5-1` | `anthropic` | native | 500 | `PlatformNoAvailableAccount / 1011` |
| 15:41:35 | `claude-opus-4-8` | `google-vertex` | standard | 500 | `PlatformNoAvailableAccount / 1011` |
| 15:41:39 | `claude-fable-5-1` | `azure` | anthropic-prefix | 200 | `claude-fable-5-1` |

## 17:11 路由故障复查：no healthy deployments

用户报告：`400 You passed in model=aws_mantle/anthropic.claude-opus-5. There are no healthy deployments for this model`。

使用同一 Key、最小消息 `Reply OK only.` 进行对照：

| 请求 | 结果 |
|---|---|
| `/standard/v1/messages`，模型 `claude-opus-5`，不指定供应商 | HTTP 400，内部路由 `aws_mantle/anthropic.claude-opus-5`，`UserParamError/2001` |
| 同一标准接口，指定 `provider=aws_mantle` | HTTP 400，同样错误 |
| 同一标准接口，指定 `provider=aws_third` | HTTP 200，返回 `OK` |
| `/anthropic/v1/messages`，模型 `anthropic.claude-opus-5`，指定 `provider=aws_mantle` | HTTP 200，返回 `OK` |

[Opus 5 文档](https://iwiki.woa.com/p/4029828135) §2 明确写着“aws_mantle 仅透传 Messages”；末尾的端点说明要求 Mantle 使用 `/anthropic/v1/messages`，认证带 `?provider=aws_mantle`，请求模型为 `anthropic.claude-opus-5`。

因此，错误中的供应商前缀是 AIHub 自动路由生成的；客户端不必传入这个完整模型名，也能复现相同错误。证据指向标准协议选择了未提供对应标准部署的 Mantle 链路。Mantle 的透传请求仍然成功，不能据此宣称其原生资源全部不可用。确切的 deployment 配置或健康过滤原因仍需平台内部日志确认。

这次错误不依赖长上下文：极短、无会话历史的请求同样失败。`CLAUDE_CODE_AUTO_COMPACT_WINDOW` 与本次最小复现没有因果证据。

[错误码表](https://iwiki.woa.com/p/4027739965) 将 `2001` 归类为 `UserParamError`，与原厂之前的 `1011 / PlatformNoAvailableAccount` 不同；当前客户端使用文档规定的统一模型 ID 也会失败，所以不能仅凭 `2001` 将原因归为用户写错模型。

自动路由失败 trace_id：`eiT3Q6HE8i6AvCjoe3Qhxb`；显式 Mantle 标准协议失败 trace_id：`94mKEnN3veyCnQ7gvvtjd2`。可用于平台排查（未自动发送给任何人）。

修复后需退出旧 Claude Code 进程，在项目目录运行 `./cc_start.sh --continue` 继续最近会话；已运行进程不会自动读取脚本里修改的环境变量。

## 19:02 启动后的再次核查：恢复会话重放旧错误

用户再次贴出的 “I'll finish the code audit of the remaining modules...” 与 Mantle 400，在原会话 `9ecd6e2c-5994-47ef-b6fd-a4ab1d99744d` 中分别记录于 2026-09-24 16:02:26 与 16:02:31（北京时间）。日志第 377 行是这条历史错误；恢复会话后的末尾追加仅为 cost-state 元数据，核查时没有新 API 错误记录。

当前交互式 Claude Code 进程于 19:02:06 启动，工作目录为本项目。直接读取该进程的环境并脱敏核对，确认 `ANTHROPIC_BASE_URL=http://api.aihub.woa.com/standard`、`ANTHROPIC_MODEL=claude-opus-5`，认证参数确实带 `provider=aws_third`。因此这不是只检查磁盘脚本得出的结论。

为验证工具调用后的后续请求，使用相同启动脚本在临时目录运行受控测试：Read 读取测试标记文件 → Bash 执行 printf → 模型返回 `MULTITURN_OK`。Claude Code 报告 `num_turns=3`、`is_error=false`、退出码 0。未访问或修改项目业务文件，也未替用户继续原代码审计任务。

现有证据表明，屏幕中的相同文字是恢复会话时显示的旧错误。当前交互会话需要输入一条新消息，例如“继续刚才的代码审计”，才会实际发起使用新配置的请求。
