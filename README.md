# Claude Code MCP for Codex

将本机 Claude Code CLI 封装为 STDIO MCP。Codex 负责实现、核实、修复和测试，Claude 根据明确提供的材料做独立审查或失败分析。

## 运行条件与安装

- Windows，Python 3.11+，Git，以及已安装并可用的原生 `claude.exe`。已在 Windows/Python 3.12 验证；其他平台尚未完成验收。
- Claude 使用自己的现有认证和服务配置。本项目不包含凭据、服务地址或用户配置。
- MCP 服务和测试只使用 Python 标准库，不需要安装 Python 依赖。

先在终端验证 `claude --version` 和已有 Claude 模型调用。下载本仓库后，预览本机配置：

```powershell
python setup_mcp.py --allowed-root "<absolute-project-directory>"
```

`--allowed-root` 可重复指定。找不到原生可执行文件时，增加 `--claude-exe "<absolute-path-to-claude.exe>"`。预览会输出本机路径，请留在本机。

确认预览后安装：

```powershell
python setup_mcp.py --allowed-root "<absolute-project-directory>" --install --install-skill
```

脚本读取 `CODEX_HOME`，未设置时使用用户主目录下的 `.codex`；已有配置先备份。若 `claude_cli` 或目标技能已存在，脚本拒绝覆盖，请手动比较更新。`--config` 可指定其他配置文件。配置示例见 [examples/config.toml](examples/config.toml)。

安装后重新加载 MCP 服务或重新打开 Codex，使工具和新说明生效。当前聊天尚未加载工具时，可通过同一注册配置调用：

```powershell
python mcp_client.py --tool claude_status --arguments-file local-status.json
python mcp_client.py --tool review_diff --arguments-file local-review.json --output local-result.json
```

`local-status.json` 内容为 `{"live_probe": false}`；改为 `true` 会实际请求模型、消耗已有服务用量。辅助脚本在 stderr 显示阶段与耗时，在 stdout 返回 JSON；可用 `--quiet-progress` 关闭显示。原生 MCP 客户端能否显示通知取决于客户端支持。

## 四个工具

| 工具 | 作用 |
| --- | --- |
| `claude_status` | 版本、认证摘要、允许目录；可选真实连通性探测 |
| `ask_claude` | 独立问答；通过 `prompt`、`context` 明确提供材料 |
| `review_diff` | 自动收集指定 Git 改动或明确文件，返回结构化候选缺陷与建议 |
| `diagnose_failure` | 收集明确文件及提供的失败日志，区分源码依据与日志假设 |

Claude 无文件、终端或嵌套 MCP 工具；失败命令只作为文本，不执行命令或修改源码。工具请求使用安全模式，无持久会话、无自定义命令。一次只运行一个调用，其余请求返回忙碌错误。

审查参数示例：

```json
{
  "project_root": "<absolute-project-directory>",
  "scope": "working_tree",
  "files": ["module.py"],
  "requirements": "实际需求及可检验的验收条件",
  "timeout_seconds": 180,
  "include_related": true,
  "use_cache": true
}
```

`working_tree` 比较 HEAD 与当前文件，包含暂存、未暂存和未跟踪内容；`staged` 审查索引，`unstaged` 比较索引与当前文件。新仓库无首次提交时也可收集。`base_ref` 仅用于 `working_tree`。非 Git 目录选择 `files` 并明确列出文件。没有选中改动时返回 `verdict=no_changes`，不调用模型。

诊断参数包含 `project_root`、`files`、`log` 或项目相对 `log_file`，以及可选 `command`、`requirements`。日志尾部最多 15,000 字符；命令不会执行。

## 结果如何核实

成功的工具响应同时包含 `structuredContent` 和 JSON 文本；`isError=true` 代表失败，不能当作没有问题。

- `findings` 是候选缺陷，包含 P0–P3、代码位置、精确引文、原始文件 SHA-256、触发条件、预期/实际行为、需求或源码契约依据、验证步骤。位置和依据匹配后仍标记 `verification_status=candidate`；Codex 必须复现或证实语义缺陷。
- 文档、风格、可选测试等意见进入 `suggestions`。行为与标识相同的重复位置合并进 `related_locations`；证据不足的意见进入 `rejected_findings`。模型摘要单独保留，不能当作验收结论。
- `hypotheses` 仅用于诊断，必须匹配所给日志；`tests_to_run` 是建议，未实际执行。
- 检查 `coverage` 的跳过项、截断和引用行范围。`coverage.complete` 只表示所选材料的收集覆盖，不能证明整个项目正确。
- `freshness.changed`、`freshness.unreadable`、`freshness.verification_timeout` 分别表示变化、读取失败、复核超时；后两者不被冒充为文件已变化。`freshness_verified=false` 时先解决复核问题。

默认流程：Codex 实现和初测 → Claude 审查 → Codex 核实候选意见 → 修复与复测。通常最多两轮审查；已有失败证据时使用诊断工具。技能详见 [SKILL.md](skill/codex-claude-review/SKILL.md)。

## 边界、缓存与截止

自动读取限制在配置允许目录和指定项目中，拒绝目录越界、链接逃逸、备用数据流及常见凭据/依赖目录。最多 24 个文件、每文件 1 MB、每份源码摘录约 10,000 字符、源码包 70,000 字符。默认最多补四个本地导入或相邻测试，不执行源码；`include_related=false` 可关闭。截断或跳过会明确返回。

已知本机环境/设置凭据及常见密钥文本会脱敏，不能保证识别所有秘密。调用前仍需选择合适材料。原始源码哈希用来核对快照。完整匹配的审查和脱敏源码摘录缓存仅存在于当前进程中，默认五分钟、分别最多 16/32 项。每次重新读取、哈希并复核新鲜度；`use_cache=false` 同时停用两种缓存。缓存命中不会发起新的模型请求，成本字段为零，原请求元数据单独保留；CLI 成本估计不等于服务账单。

`timeout_seconds` 是收集、模型请求和复核的统一预算（10–600 秒）。进度通知只报告当前阶段及累计耗时，不延长截止或虚构百分比。失败返回 `failed_phase` 与 `timing`。辅助客户端额外留 15 秒协议清理时间；超时发送取消。Windows Job Object 回收该请求自己的子进程树。

CLI/Git 的输入和输出用临时文件捕获，关闭后删除，避免 Windows 继承管道造成等待。源码/审查缓存没有磁盘持久化。辅助脚本主动指定的输出文件、健康检查结果和失败日志可能含本机路径、服务地址、代码引文，应保留在本机。不要上传本机配置、备份、日志、运行输出或真实测试项目。

## 验证

```powershell
python -m unittest -v test_bridge.py test_workflow.py test_client.py test_utilities.py test_setup.py
```

CI 在 Windows 上运行离线单元与故障模拟测试，不需要服务密钥，不请求模型。覆盖总截止与取消、初始化清理、stderr 排空、路径限制、证据过滤、缓存/进度、Git 新仓库及新鲜度分类。符号链接测试会在缺少 Windows 权限时明确跳过。

本机另做过真实 MCP 验收：合成回归样例审查、缓存命中、失败诊断、Codex 修复后原测试通过、无改动跳过模型。仓库不包含那次运行结果或个人配置。实际延迟由模型服务决定，环境网络限制不能直接归因于服务故障。
