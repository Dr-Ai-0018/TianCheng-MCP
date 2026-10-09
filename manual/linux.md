[手册目录](README.md) · [Windows 首次使用](quickstart-windows.md)

# Linux 使用说明

## Linux 本地 stdio

在 Linux 上使用 Python 3.13 与 `uv`，将工作区放在源码或安装目录之外：

```sh
uv sync --frozen
./run-mcp.sh --workspace /absolute/path/to/workspace
```

这是前台 stdio 进程，由 MCP 客户端关闭输入流或 Ctrl+C 结束；不启动后台服务。
也可以安装构建后的 wheel，再运行 `tiancheng-mcp --workspace ...`。默认 SAFE
模式无需 Tunnel 或模型 API key；`--allow-exec`、外部授权和 Agent 能力需要单独启用。
Linux 默认从 `~/.config/tiancheng-mcp` 读取服务配置，
在 `~/.local/state/tiancheng-mcp` 保存审计与 Catalog 状态；设置
`XDG_CONFIG_HOME` 或 `XDG_STATE_HOME` 后，分别改用该目录下的 `tiancheng-mcp` 子目录。
服务自有目录必须由当前用户持有且权限为 `0700`。可以通过命令行参数逐项指定
独立配置路径。Linux 上的工作区文件仍服从运行用户的系统权限，服务不会改写整棵
工作区的权限。Agent 验收需要宿主事先配置可用的原生 CLI 和模型凭据。

Agent 真实验收入口与隔离要求见 [开发说明](development.md)。

## Linux Tunnel 运行

以下脚本管理默认 SAFE Tunnel；客户端负责进程启动、状态和停止，不设置开机自启。

先把 [示例配置](../config/tunnel.linux.example.json) 复制到**工作区外**的私有目录，
填写绝对路径、现有 Tunnel ID 和别名。`runtime_root`、审计及配置路径都应在
MCP 工作区外；`client` 指向已核验的 Linux `tunnel-client` 可执行文件，
`python` 指向安装了本项目的 Python。`key_file` 是仅由运行用户持有且不允许组/其他用户
访问的普通文件，内容为单行 `CONTROL_PLANE_API_KEY=...`；不要把 key 写进 JSON、
命令参数、仓库或工作区。运维脚本只输出选定的运行状态字段，不输出客户端原始日志。

```sh
python3 scripts/linux_tunnel_runtime.py connect --config /absolute/private/tunnel.json
python3 scripts/linux_tunnel_runtime.py status  --config /absolute/private/tunnel.json
python3 scripts/linux_tunnel_runtime.py stop    --config /absolute/private/tunnel.json
```

`connect` 创建或复用该别名，`status` 应显示 `runtime_state=ready` 且
`process_running=true`、`ready=true`；`stop` 应显示 `runtime_state=stopped`。
需恢复时再次运行 `connect`。脚本将 Tunnel 的 XDG 配置、状态和数据隔离在
`runtime_root` 下。当前实现只提供可脚本化的 Linux 运维入口，尚未移植 Windows
`tc.ps1` 的交互式菜单。Tunnel 连接成功不代表真实 Agent 或 DEV 能力已验收。
