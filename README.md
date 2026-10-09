# TianCheng Local MCP

让 ChatGPT 在你的电脑上读取、编辑文件并操作本地 Git。你指定一个工作区，服务通过 OpenAI Secure MCP Tunnel 与 ChatGPT 连接，电脑无需开放公网入站端口。

适合在聊天里整理资料、修改代码、查看差异和提交本地仓库。文件保存在你的电脑上；调用工具返回的内容会进入 ChatGPT 对话。

当前版本：`0.16.0`。支持 Windows / PowerShell 7 和原生 Linux。

## 先跑通一次

Windows 用户从 [从零安装到输入 tc 的完整指南](manual/quickstart-windows.md) 开始。需要：

- Python 3.12+、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和 PowerShell 7；本地 Git 操作另需 Git。
- [OpenAI tunnel-client](https://github.com/openai/tunnel-client/releases/latest)。
- 可用的 Tunnel ID、Tunnel 运行密钥，以及账号或工作区允许添加自定义 MCP 的权限。见 [OpenAI 官方说明](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。

整个流程是：**准备工作区 → 创建本机 Profile → 启动 → 在 ChatGPT 添加连接 → 验证文件写读**。

已经准备好依赖？在仓库根目录运行：

```powershell
uv sync --frozen
pwsh -NoProfile -File .\tc.ps1
```

首次使用指南包含 Tunnel/Key 创建截图、依赖安装、快捷命令安装与每个菜单说明。完成后在新开的 PowerShell 7 中输入 `tc` 即可打开控制台。

Linux 用户见 [Linux 使用说明](manual/linux.md)。其他支持 stdio 的 MCP 客户端可直接启动本地服务，无需 Tunnel。

## 默认能做什么

默认 **SAFE** 模式允许工作区内的文件读取、写入、移动、回收和恢复，以及本地 Git 查询与提交。**SAFE 允许修改文件，并不是只读模式。**

默认关闭普通命令执行、远程 Git 和本地 Agent 运行。外部目录需要单独授权；切换 DEV 或更高命令预设也需要你在本机明确启用。路径权限与命令权限分别管理，执行能力不提供操作系统沙箱。详见 [权限与命令预设](manual/permissions.md)。

启动时，服务源码、密钥、配置和日志应放在工作区之外。Tunnel 进程需要保持运行；关闭运行窗口后，ChatGPT 无法继续访问这台电脑。

## 按需要查手册

| 想做什么 | 看这里 |
| --- | --- |
| 从零连接 ChatGPT | [Windows 首次使用](manual/quickstart-windows.md) |
| 连接失败、改配置不生效 | [常见问题](manual/troubleshooting.md) |
| 配置工作区、代理、环境变量 | [本机配置](manual/configuration.md) |
| 开启命令、调整白名单、授权外部目录 | [权限与命令预设](manual/permissions.md) |
| 查看文件、Git 和其他工具 | [工具参考](manual/tools.md) |
| 启停、重启、检查状态和日志 | [运行维护](manual/operations.md) |
| 查询长任务、管理常驻进程 | [后台任务与进程](manual/jobs-and-processes.md) |
| 使用 Codex / Claude / Pi | [本地 Agent](manual/agents.md) |
| Linux 部署 | [Linux 使用说明](manual/linux.md) |
| 参与开发、运行测试 | [开发说明](manual/development.md) |

版本变更和迁移说明见 [CHANGELOG](CHANGELOG.md)。
