---
name: mcli-mcp-caller
description: 指导 Agent 通过命令行工具 mcli 规范、高效地调用外部 MCP 服务，实现按需探测参数、管道过滤输出与报错自愈，避免上下文膨胀。当用户要求调用 MCP 工具、使用 mcli、访问外部挂载服务（如 GitHub、数据库、Notion 等），或需要生成调用外部 MCP 服务的终端命令时使用此技能。
---

# mcli-mcp-caller

指导 Agent 通过命令行工具 `mcli` 规范、高效地调用外部 MCP 服务，实现按需探测参数、管道过滤输出与报错自愈，避免上下文膨胀。

## When to Use

- 需要通过终端命令调用挂载的外部 MCP 服务（如 GitHub、数据库、文件系统、Notion 等）时
- 需要查询当前环境可用的 MCP 服务或工具参数定义时
- 需要构造 `mcli` 命令并结合 Unix 管道处理返回结果时

## Steps

### 1. 先查后用，严禁盲猜参数（懒加载）

在首次调用任何不确定的服务或工具前，按层级通过 `--help` 探测用法，不要凭记忆编造子命令或参数名：

- 查看可用服务：`mcli ls`
- 查看某服务下的工具列表：`mcli <server> --help`
- 查看具体工具的参数定义：`mcli <server> <tool> --help`

### 2. 合理选择传参方式

- 简单参数：直接使用扁平选项，例如 `mcli github list_issues --owner foo --limit 5`
- 复杂嵌套参数（对象或数组）：不要拼接复杂 flag，直接使用 `--json '<JSON字符串>'` 或通过标准输入传入（`echo '{...}' | mcli <server> <tool> --stdin`）

### 3. 善用 Unix 管道，防止上下文膨胀

`mcli` 的 stdout 是纯净的文本或 JSON。对于可能返回海量数据的查询命令，在命令末尾通过管道过滤、裁剪后再输出：

- 截断行数：`... | head -n 50` 或先看总量 `... | wc -l`
- 关键词过滤：`... | grep -i "error"`
- 结构化提取（推荐）：`mcli github list_issues --owner foo --repo bar | jq '.[:5] | map({number, title, state})'`

### 4. 报错自愈流程

**最高优先级规则**：当 `mcli` 返回**退出码 13** 或 stderr 包含 `[PERMISSION_DENIED]` 时，说明该操作被安全策略拦截或被用户手动拒绝。**严禁重试或尝试绕过**（包括换参数重试、改用 `--json`/`--stdin` 传参等），必须立即停止调用并如实向用户汇报。

当 `mcli` 返回其他非零退出码（Exit Code == 1 等）时，按以下顺序排查并重试：

1. **读 stderr**：优先根据错误提示修正参数格式或缺失的必填项。
2. **重查文档**：如果是参数解析失败，立即执行 `mcli <server> <tool> --help` 核对 Schema 后重试（同一报错最多重试 2 次）。
3. **排查连接**：若提示 Daemon 无响应或连接超时，执行 `mcli daemon status` 检查状态，必要时仅重试一次；若仍失败再如实向用户汇报。

## Gotchas

- 避免直接裸跑未加限制的全量查询命令，防止海量返回结果撑爆上下文窗口。
- 遇到同一报错超过 2 次时及时止损并向用户说明情况，不要陷入无限重试循环。
- Windows (cmd/PowerShell) 环境下没有 `|`、`grep`、`head`、`jq` 时，改用 `--json` 精确传参缩小返回范围，或提示用户在 Git Bash/WSL 中执行管道命令。
