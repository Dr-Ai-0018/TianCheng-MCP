[手册目录](README.md) · [Windows 首次使用](quickstart-windows.md)

# 本机配置

## 配置与版本控制边界

Git 只跟踪可移植默认值和示例：`launcher.defaults.json`、
`launcher.local.example.json`、`agent-profiles.example.json` 与 `.env.example`。
以下文件只属于当前机器，均被 `.gitignore` 排除：

- `config/launcher.local.json`：工作区、工具路径和本机 Tunnel 设置；
- `config/agent-profiles.json`：私有 provider/profile、独立 `CODEX_HOME` 与凭据变量名；
- `.env`、`exec-env.allowlist`：本机变量值和显式透传名单；
- `config/access-policy.json`、`config/agent-sources.json`：本机授权策略；
- `state/`、`logs/`：运行状态、Catalog 与审计日志。

Windows TUI 和 SAFE/DEV/GRANTS 启动器共用 Python 配置模型。`tc.ps1 -ConfigPath <文件>`
生成的 MCP 命令会把同一个配置文件传给启动器；三个 `run-mcp*.ps1` 也都接受 `-ConfigPath`。
workspace、Python、policy、sources、Catalog、Agent profiles、env 文件、audit 目录及超时设置
均使用该配置。相对路径以项目根目录为基准；`TIANCHENG_WORKSPACE` 覆盖配置中的 workspace。
Python CLI 可显式传 `--runtime-config <文件>`；明确 CLI 参数优先于环境 workspace、local 配置和 defaults。
执行、外部 grant 和策略热更新只由显式开关启用，不从 JSON 推断授权。

旧默认 profile 仍可使用默认配置。已有 profile 若没有记录所选自定义配置，或记录的是另一份配置，
TUI 会在 doctor/start 前阻止启动；使用同一 `-ConfigPath` 执行 `set-mode` 或 `edit-profile`
更新原有 profile，无需另建 Tunnel。`info -Json` 可查看所选路径与 SAFE MCP 命令；
新窗口启动也会保留这份配置。配置值无效时明确失败，不静默回退。

需要自定义 Agent 时复制示例后再修改；不要直接修改随仓库提交的示例：

```powershell
Copy-Item .\config\agent-profiles.example.json .\config\agent-profiles.json
```

## 出站代理（可选）

在 `config/launcher.local.json` 的 `proxy` 段可填写 `http`、`https`、
`noProxy` 和 `agent`，格式见 `config/launcher.local.example.json`。
`http` / `https` 分别控制目标为 HTTP / HTTPS 的请求，可填
HTTP(S) 代理 URL（例如 `http://127.0.0.1:7890`），也可填
SOCKS5 代理 URL（例如 `socks5://127.0.0.1:7891` 或
`socks5h://127.0.0.1:7891`）；`socks5h` 让代理端解析目标域名。
`noProxy` 为逗号分隔的直连主机列表。SOCKS 支持依赖项目安装的 `socksio`。
也可以在启动 `tc` / MCP 之前设置进程环境变量
`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY` 或其小写形式。
代理需要账号密码时使用 `协议://用户名:密码@主机:端口`；用户名或密码里的
`@`、`:`、`/`、`#`、`%` 等保留字符须分别进行 URL 编码。
含凭据的 URL 放在本地配置文件会以明文保存；也可以只在启动 `tc` 的 PowerShell
进程环境里设置，不要提交或发送实际凭据。
各字段按“本地 launcher 配置 → 启动进程环境变量 → 默认配置”合并；
同一进程里大小写变量同时存在时大写优先。仅设置其中一项也不会抹掉其他来源的字段。
本地配置中的空值会显式禁用对应代理，不会回退到进程环境；删除该字段可恢复继承。
菜单设置后选 `0` 保存，下次检测/启动即读取新配置，无需重新打开 PowerShell；
选 `8` 会先保存当前修改，再检测连通性和出口 IP，无需退出菜单。
已运行的 MCP/Tunnel 需重启。本地未设置的字段仍可通过进程环境中的空值禁用默认代理。

未配置任何代理时保持系统原有代理行为。配置代理后，MCP 进程及 Tunnel Supervisor
自身的出站 HTTP 请求使用合并结果；`localhost`、`127.0.0.1`、`::1` 会加入
`NO_PROXY`，Supervisor 对 `127.0.0.1` 的健康探测始终直连。
`tc.ps1` 本机健康检查原有的 `-NoProxy` 也仍是直连。
`.env` 只用于已有的凭据回退，不会自动载入代理变量。

`proxy.agent` 有三种模式：`off`（默认）保持 Agent 原有隔离环境；`selective`
只在 `agent_session(create|attach)` 传入 `use_proxy=true` 时为该 session 注入代理；
`always` 为所有新 Agent session 注入代理。旧值 `inherit` 兼容为 `always`。
只有 `selective` 模式会在 MCP 工具 schema 中提供 `use_proxy` 参数；另外两种模式
的工具说明会写明当前策略，并且服务端始终忽略绕过 schema 送来的 `use_proxy` 值。
Agent 的 `proxy_enabled` 状态可在 session 返回值中查看，`workspace_info` 提供
`agent_proxy_mode` 和 `agent_proxy_configured`。Agent 子进程中已有的同名环境变量
不会被覆盖（Windows 下按不区分大小写处理）。这项模式只控制 Agent，
不扩大通用命令或其他子进程的环境透传范围。更改配置后需重启 MCP/Tunnel 才会生效。
`tc` 主菜单的「P. 出站代理设置」可查看代理主机、端口、认证状态及配置来源，
设置或清除 HTTP/HTTPS 目标代理、NO_PROXY 和 Agent 模式；「A. 启动器设置」也提供入口。
菜单不会显示用户名或密码。选 8 可手动检测已保存配置的连通性：通过对应代理
访问 `api.ipify.org`，显示代理出口 IP 或错误类别；不会在每次打开菜单时自动联网。
在真实交互终端输入代理 URL 时，输入内容会隐藏。菜单保存的本地配置优先于进程环境变量，菜单会标明来源。已有配置不会因查看菜单而改写。

## 显式透传一个业务环境变量

DEV 子进程默认仍然拿不到任意业务 secret。如果确实需要让某个开发工具读取一个变量，
只把变量名加入服务仓库内、MCP 工作区外的执行白名单文件（文件只存名称，不存值）：

```powershell
Set-Content -LiteralPath .\exec-env.allowlist `
  -Value '# names only','EXAMPLE_AGENT_KEY' -Encoding utf8
.\run-mcp-exec.ps1
```

也可以直接使用 CLI 参数：

```powershell
.\.venv\Scripts\python.exe -m tiancheng_mcp `
  --workspace '<WORKSPACE>' --audit-dir '<REPO>\logs' `
  --allow-exec --pass-env EXAMPLE_AGENT_KEY
```

`--pass-env` 可以重复使用，但只接受合法环境变量名；`CONTROL_PLANE_API_KEY`、OpenAI
控制面 key、PATH/SystemRoot/TEMP 等策略变量永远不能透传。值不会写入审计日志或 workspace，
但被透传的程序本身可以读取、打印或上传它，所以这仍属于 DEV 高风险能力。修改白名单后，
必须重启 Tunnel/MCP；在 ChatGPT Plugins 中打开连接并选择 **Refresh**。

真实执行验收入口及模型调用边界见 [开发说明](development.md)。

新建 profile 默认使用 `run-mcp.ps1`（SAFE）。需要完整开发权限时，可在
**Profile 管理 → 一键切换当前 Profile 为 DEV**，它会把真实 profile 的 MCP command
改成 `run-mcp-exec.ps1`；切回 SAFE 同理。DEV 启动时仍会显示一次风险确认。
