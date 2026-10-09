[手册目录](README.md) · [Windows 首次使用](quickstart-windows.md)

# 运行维护

无需安装快捷命令即可运行 `pwsh -NoProfile -File .\tc.ps1`。先连接见 [首次使用](quickstart-windows.md)。

## `tc` 中文控制台

安装器会在 PowerShell `CurrentUserAllHosts` Profile 中加入一个带边界标记的幂等函数块，
不会覆盖原有 Profile 内容：

```powershell
.\install-tc.ps1
```

重开 PowerShell 后直接输入：

```powershell
tc
```

菜单底部显示用途和生效提示；输入 **H** 可查看每个选项的说明，帮助不修改配置。完整安装与快捷命令排错见 [单页教程](quickstart-windows.md)。

主界面、Profile、Key、代理、命令规则、路径权限和 Agent 来源七个菜单使用统一 TUI。在普通 PowerShell 7 控制台使用上下键选择、Enter 执行、Home/End 跳转、R 刷新；原数字/字母快捷键继续可用。底部灰色说明随选择更新，小窗口可滚动。主界面的 Esc/0 退出控制台；代理页的 Esc/0 **保存并返回**；其他子菜单返回上级。窗口小于 60 列或 20 行、输入输出被重定向或控制台能力不足时使用文本菜单，选项与帮助来自同一份定义。

顶部只显示状态摘要，较长的配置、规则和来源列表可按 **V** 查看全部。H 帮助和 V 详情使用当前页已经读取的状态，返回后恢复选中位置，不重复探测。**R** 才主动刷新页面状态；代理出口联网检测仍需明确选择 8。H/V、主菜单 5（Doctor）与 8（完整状态）使用分页结果页：上下键滚动、PgUp/PgDn 翻页、Home/End 跳到首尾、Enter/Esc 返回。Doctor 显示实际退出码，非零代表检查失败；完整状态查询失败显示未知或注明时间的缓存。输入表单和权限确认沿用原流程。

菜单支持：

- 一键执行 `doctor`，成功后在当前窗口运行 Tunnel；Tunnel 自动拉起 stdio MCP；
- 在独立可见 PowerShell 窗口启动；
- 创建/重建、选择和编辑多个 tunnel-client profile；
- 当前进程、Windows 用户环境变量和项目 `.env` 三种密钥方式；
- 从真实 profile YAML 的 `mcp.commands[].command` 判断 SAFE/DEV，不再依赖易漂移的名单；
- 一键切换 SAFE/DEV、显示实际运行中的 profile、停止/重启 Tunnel；
- Profile 管理中可直接切换“聊天外部授权（一次性 challenge）”或“外部授权 + Exec”，无需手动编辑 YAML；
- 状态检查同时显示 Git、GCM、`gh` 可用/登录布尔状态，不输出账号 token；
- Tunnel 默认由 Supervisor 托管：stdio transport 出现确定性的内部 502、连续 upstream
  failure，或 tunnel-client 退出时，会按退避和重启预算自动重建完整进程树；
- 管理 UI、非敏感启动器设置和显式 Dev profile；
- “E. 本地 Agent / 会话源管理”可探测 Codex/Claude CLI 与固定历史根，并由用户显式添加、启停、删除、验证、刷新或重建 metadata Catalog；
- `tc -Action info|profiles|key-status|status|agents -Json` 非交互诊断。

启动前会检查当前 Profile 是否已在运行，以及本机健康监听端口是否被占用。端口冲突时会提示
检查占用进程，避免重复开新窗口；菜单无法识别运行进程但检测到端口占用时也不会显示为
`Running: none`。它不会自动结束占用进程或改用别的端口。

工具名称变更后，应重启本机 MCP，并在 ChatGPT 的 TianCheng MCP 连接中执行 Refresh、
新开对话核对工具列表。本机新启动的 stdio MCP 会重新注册工具；`tc` 没有能清除
ChatGPT 端工具快照的本地缓存操作。

首次运行时，本机尚无 profile；进入 **Profile 管理 → 创建或重建 Profile**，填写现有
`tunnel_...` ID 即可。安全模式为默认选择。Exec 模式需要输入两次醒目确认，并在以后
每次启动时再次确认。

API key 加载优先级为：当前进程 → Windows 用户环境变量 → `.env`。状态界面只显示
“是否已配置”和来源，从不显示值。`.env` 位于项目根目录、已被 `.gitignore` 排除，写入
后会尝试移除继承 ACL 并只授权当前 Windows 用户；它仍然是明文文件，不是加密保险箱。
`.env` 只由 `tc` 加载，直接执行 tunnel-client 不会自动读取它。

## Tunnel 自动恢复与连接 TTL

`tc start` 和 `tc start-new` 默认不再直接裸跑 `tunnel-client`，而是由本地 Supervisor
托管。默认配置将 `--mcp.connection-max-ttl` 显式设为 `24h`，并在连接出现确定性的
`client_internal + upstream_response_received=false` 时立即重建 Tunnel/MCP 进程树；较模糊
的 deadline/upstream 错误必须在短窗口内连续出现才会触发恢复。控制面普通 poll 断线继续由
`tunnel-client` 自己退避重连，不会因此误重启 stdio MCP。

Supervisor 默认 10 分钟内最多恢复 5 次，之后熔断为 `FAILED`，避免配置错误造成重启风暴。
`tc stop` 会停止 Supervisor 和完整子进程树；用户主动停止不会被自动拉起。可在“启动器设置”
里开关 Supervisor 或修改 transport TTL。这里的 TTL 是 **Tunnel 到 stdio MCP 的连接寿命**，
不是 `agent_run.max_runtime_seconds`，也不是 `start_process.max_runtime_seconds`。

`supervisor.enabled=false` 是显式逃生开关：此时 `tc` 完全走旧的直接启动路径，不启动 Python
Supervisor、不要求 Supervisor Python 存在，也不追加 transport TTL 参数。正常 `tc stop` 使用
stop request 让 Supervisor 自己回收 Job Object 子树，随后确认并删除 `.lock/.stop` 文件；若
graceful stop 超时才按已核验 PID 强制收尾。

`tc status` 将健康拆成 Tunnel `/readyz`、`MCP Inferred` 和 `MCP Verified` 三栏。没有真实 MCP probe 时，`MCP Verified` 显示 `not-available`；
`/readyz=ready` 不能证明工具 roundtrip 成功。
在上游提供同 transport probe 前，首次暴露 transport 失效的请求仍可能返回可重试错误；
Supervisor 只恢复连接，绝不自动重放结果未知的写入、命令、Agent 或 Git 请求。

每次恢复都会向 `logs/tunnel-supervisor-<profile>.jsonl` 追加一行 JSON，至少包含
`generation/recovery_reason/restart_count/backoff_until`；同样的最新字段会进入
`state/tunnel-supervisor-<profile>.json`。两处都不记录 key、env、工具参数或正文。

如果只想直接启动裸 stdio MCP，可运行：

```powershell
.\run-mcp.ps1
```

这是 stdio 服务，会等待 MCP 客户端输入，并不是普通交互式 CLI。不要向它的 stdout
写调试信息。

高风险执行版本需要显式运行：

```powershell
.\run-mcp-exec.ps1
```

## 审计日志

默认文件：

```text
<REPO>\logs\tiancheng-mcp-audit.jsonl
```

每行只含 UTC 时间、tool、相对路径、success/failure、duration 和错误类型。不会记录
文件正文、command args、API key 或环境变量内容。恶意绝对路径在进入日志前会替换为
`<rejected-path>`。日志达到 5 MiB 后自动轮转，最多保留 5 个历史文件。
