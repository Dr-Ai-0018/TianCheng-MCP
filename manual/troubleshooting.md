[手册目录](README.md) · [Windows 首次使用](quickstart-windows.md)

# 常见问题

从失败的步骤开始排查。先看控制台中的配置来源和 Doctor 输出，再检查运行状态；不要把密钥值或带凭据的代理 URL 粘贴到日志和聊天中。

| 现象 | 下一步 |
| --- | --- |
| 找不到 `pwsh`、`uv` 或 `tunnel-client` | 安装相应程序并检查 PATH；未加入 PATH 的 pwsh / tunnel-client 可在本机配置中填写绝对路径。安装后新终端才会继承新 PATH。 |
| 工作区为空或不是自己填写的目录 | 检查 `launcher.local.json` 的 workspace、JSON 转义和 `TIANCHENG_WORKSPACE`；用 `tc.ps1 -Action info -Json` 看实际解析结果。 |
| 没有 Profile | 主菜单 6 → 1，用真实 Tunnel ID 创建 SAFE Profile；名称与 Tunnel ID 是两项不同的值。 |
| Key 显示未配置 | 主菜单 7 配置运行密钥；`.env` 只由控制台加载，直接运行 tunnel-client 不会自动读取。 |
| 修改 `.env` 后仍使用旧 key | 密钥优先级为进程 → Windows 用户环境变量 → `.env`。查看 key-status 的来源，更新或移除优先级更高的旧值。 |
| 配置路径与 Profile 不一致 | 用同一个 `-ConfigPath` 编辑 Profile 或设置模式，将选定配置写进 MCP 启动命令，再重启。 |
| 端口占用、重复启动 | 检查主菜单 8 的运行状态；先停止当前 Profile，确认端口占用者，再启动。程序不会自动结束未知占用进程。 |
| 代理 `ConnectError` | 检查代理程序正在运行、协议和监听端口是否一致。本地配置优先于进程代理；菜单 8 先保存再检测，0 保存返回，不必为了保存代理重开 PowerShell。 |
| 清除代理后又看到进程环境 | 删除本地字段会恢复继承；本地空值显式禁用对应代理。查看来源，更改后重启已经运行的 MCP/Tunnel。 |
| 本机 Ready，但 ChatGPT 找不到 Tunnel | 检查 Tunnel ID、Platform 组织与 ChatGPT 工作区关联、Read + Use 权限及工作区自定义 MCP 策略。 |
| 创建自定义 MCP 插件时出现 429 或不直观的错误 | 先确认已在 Tunnel 工具中完成连接验证，并保持 Tunnel 运行，再重试创建。这是实际使用中遇到的一个坑；若验证后仍失败，继续按返回信息排查。 |
| ChatGPT 工具还是旧的 | 重启本机服务，在 ChatGPT 连接里 Refresh，再新开对话；本机控制台无法清除 ChatGPT 工具快照。 |
| SAFE 里没有命令或 Agent 工具 | 这是默认能力。需要执行时，在本机明确切换 DEV；命令预设本身不会开启 DEV。 |
| 调整命令规则后仍被拒绝 | 控制台展示 next_start 配置；重启后查 workspace_info 的 runtime_snapshot / policy_revision，并按错误原因代码检查禁用规则、参数模板和程序是否可用。 |
| 耗时操作返回 `job_id` | 用 job_status / job_result 查询原任务；不要因为客户端超时就再次提交写入、提交或命令。 |

Tunnel 工作区权限与 ChatGPT 连接入口见 [官方 Tunnel 指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 和 [官方连接指南](https://developers.openai.com/plugins/deploy/connect-chatgpt)。更细的代理与环境变量规则见 [本机配置](configuration.md)。

## 反馈问题时带哪些信息

提供失败步骤、错误类别、服务版本、所用模式，以及已脱敏的 `info` / `status` 结果。先检查输出是否含工作区路径等个人信息；不要提供 key、token、完整 .env 或原始 Agent transcript。
