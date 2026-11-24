# mcli

`mcli` 把 MCP 服务的工具变成终端命令，供人和 AI Agent 按需查看、调用和处理结果。CLI 首次使用时自动启动本地 daemon，由 daemon 维持 MCP 连接、缓存工具列表并在执行前检查工具权限。

## 快速开始

需要 Python 3.11+、Git 和 uv。在仓库根目录运行以下命令，使用随仓库提供的本地 mock 服务。

```sh
git clone https://github.com/175gkd/mcli.git
cd mcli
uv sync --locked
uv run mcli ls
uv run mcli mock --help
uv run mcli mock add --help
uv run mcli mock add --a 2 --b 3
```

`ls` 会显示 `mock` 的连接状态和工具数量。最后一条命令在 stdout 输出 `5`。用完可运行 `uv run mcli daemon stop`。

如果需要直接使用全局 `mcli` 命令，在仓库根目录运行 `uv tool install -e .`。试跑仓库的 mock 时建议继续使用 `uv run mcli`，它会将项目环境中的 Python 放到 PATH 上，供 mock 子进程使用。正式使用前，请按下文配置自己的服务。

## 配置 MCP 服务

配置文件名为 `mcp_servers.json`，顶层使用 Claude Desktop 兼容的 `mcpServers` 字段。查找顺序如下。

1. 环境变量 `MCLI_CONFIG` 指向的文件。显式指定后，路径无效会报错。
2. 当前工作目录的 `mcp_servers.json`。
3. 用户配置目录中的 `mcli/mcp_servers.json`。Windows 使用 `%APPDATA%`，macOS 和 Linux 使用 `$XDG_CONFIG_HOME`，默认 `~/.config`。

下面的示例包含本地 stdio 服务和两个远程传输的配置写法。将 URL 换成实际端点，或只保留需要的服务。

```json
{
  "mcpServers": {
    "mock": {
      "command": "python",
      "args": ["tests/mock_server.py"]
    },
    "remote": {
      "type": "streamable-http",
      "url": "https://example.com/mcp"
    },
    "legacy": {
      "type": "sse",
      "url": "https://example.com/sse"
    }
  }
}
```

stdio 服务可设置 `cwd` 和 `env`。建议给本地脚本使用绝对路径，或显式设置 `cwd`。远程服务可通过 `headers` 添加自定义请求头。含凭据的配置请保存在仓库外。

daemon 按配置文件的绝对路径隔离。修改服务配置或同目录的策略文件后，用同一份配置运行 `mcli daemon restart`。

## 查找和调用工具

| 命令 | 用途 |
| --- | --- |
| `mcli ls` | 列出服务、传输方式和连接状态 |
| `mcli <server> --help` | 查看服务提供的工具 |
| `mcli <server> <tool> --help` | 查看参数类型、必填项、枚举和默认值 |
| `mcli daemon start\|stop\|status\|restart` | 管理当前配置对应的 daemon |

简单参数使用 `--key value`。嵌套对象和数组放在 JSON 参数对象的相应字段中，可用 `--json` 或 `--stdin` 传入。多种方式一起使用时，同名扁平参数会覆盖 JSON 中的值，合并后的参数会按工具的 JSON Schema 校验。

```sh
# Bash / Git Bash
uv run mcli mock add --json '{"a": 2, "b": 3}'
printf '%s\n' '{"a": 2, "b": 3}' | uv run mcli mock add --stdin
```

```powershell
# PowerShell
'{"a": 2, "b": 3}' | uv run mcli mock add --stdin
```

成功时，工具文本直接写入 stdout。图片和二进制资源会保存到系统临时目录的 `mcli_assets` 子目录，stdout 输出文件路径。可用 `MCLI_ASSETS_DIR` 指定其他目录。只有工具本身输出 JSON 时，才适合接 `jq`。

## 权限与人工审批

权限文件 `mcli_policy.json` 和 `mcp_servers.json` 放在同一目录。缺少策略文件时，启动会生成示例模板，默认动作是 `ask`。无效 JSON、规则或正则会阻止 daemon 启动，错误详情写入 daemon 日志。

仓库自带的 mock 策略如下，可按实际服务和工具名修改。

```json
{
  "default_action": "ask",
  "rules": {
    "mock": {
      "allow": ["echo", "add"],
      "ask": ["render"],
      "deny": ["fail"]
    }
  }
}
```

判定依次检查 `param_guards` 的 `deny_regex`、工具名通配符规则、MCP 工具注解和全局 `default_action`。同一工具同时命中多条显式规则时，优先级为 `deny` → `ask` → `allow`。注解兜底先检查 `destructiveHint` 并要求审批，再检查 `readOnlyHint` 并放行。

- `uv run mcli mock add --a 2 --b 3` 直接执行。
- 每次判定为 `ask` 的调用都单独请求桌面审批，例如 `uv run mcli mock render --mode svg`。Windows 在 daemon 所在的交互桌面显示原生置顶弹窗，macOS 使用 `osascript`，Linux 需要图形会话和 `zenity`。默认等待 60 秒。CLI 会区分策略拒绝、用户拒绝、审批超时和弹窗不可用，这些结果均返回 `[PERMISSION_DENIED]` 和退出码 `13`。
- `uv run mcli mock fail --msg demo` 被策略拒绝，stderr 包含 `[PERMISSION_DENIED]`，退出码为 `13`。Bash 可用 `echo $?` 查看，PowerShell 可读取 `$LASTEXITCODE`。

## 给 AI Agent 使用

将仓库中的 [mcli-mcp-caller Skill](.devin/skills/mcli-mcp-caller/SKILL.md) 放到宿主支持的技能目录，并确保 Agent 的终端能运行 `mcli` 且能找到目标配置。

首次使用某个服务时，依次运行 `mcli ls`、`mcli <server> --help` 和 `mcli <server> <tool> --help`。随后按 Schema 构造参数，按需处理 stdout。

**遇到退出码 `13` 或 `[PERMISSION_DENIED]` 时，立即停止调用并向用户说明。禁止重试或换一种传参方式绕过。** 一般参数或工具错误返回 `1`。遇到超时或断线时，先确认有副作用的操作是否已经生效。

## 排查

- **找不到服务或策略改动未生效**。检查当前目录和 `MCLI_CONFIG`，然后对该配置运行 `mcli daemon restart`。
- **daemon 启动失败或审批异常**。查看 `daemon.log`。默认位于 Windows 的 `%LOCALAPPDATA%/mcli/<配置路径哈希>/`，或 macOS 与 Linux 的 `${XDG_STATE_HOME:-~/.local/state}/mcli/<配置路径哈希>/`。设置 `MCLI_STATE_DIR` 可以改用其他状态目录。
- **需要调整超时**。工具调用默认 30 秒，审批默认 60 秒，daemon 默认空闲 30 分钟退出。可分别设置 `MCLI_CALL_TIMEOUT`、`MCLI_CONFIRM_TIMEOUT` 和 `MCLI_IDLE_TIMEOUT`，单位为秒，修改后重启 daemon。

在仓库根目录运行 `uv run --locked pytest -q` 执行单元与本地端到端测试。
