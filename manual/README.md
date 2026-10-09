# 使用手册

从 [Windows 首次使用](quickstart-windows.md) 开始；连接成功后，再按任务查阅详细说明。

| 文档 | 内容 |
| --- | --- |
| [Tunnel 与 Key 创建](tunnel-setup-windows.md) | 创建表单、最小权限、客户端下载与存放位置 |
| [常见问题](troubleshooting.md) | 找不到程序、密钥、Tunnel、端口、代理与配置生效问题 |
| [本机配置](configuration.md) | 配置文件、路径解析、出站代理、显式环境透传 |
| [权限与命令预设](permissions.md) | SAFE/DEV、四档命令预设、外部路径授权及隔离边界 |
| [工具参考](tools.md) | 文件、Git、能力开关、大小和数量限制 |
| [运行维护](operations.md) | 控制台、启停、恢复、连接 TTL 和审计日志 |
| [后台任务与进程](jobs-and-processes.md) | job、常驻进程与 Agent run 的不同生命周期 |
| [本地 Agent](agents.md) | Profile、登录态、运行、审批和历史索引 |
| [Linux 使用说明](linux.md) | 本地 stdio 和 Tunnel 运维入口 |
| [开发说明](development.md) | 测试、构建与真实链路验收 |

## 几个名字怎么理解

- **工作区**：你指定给 MCP 操作文件和本地 Git 的目录，应与服务仓库分开。
- **Tunnel**：OpenAI 提供的连接通道，用 Tunnel ID 关联电脑与 ChatGPT。
- **Tunnel Profile**：保存在本机的 Tunnel 客户端配置，包含 Tunnel ID 和 MCP 启动命令。
- **SAFE / DEV**：MCP 服务的能力开关。DEV 开启命令和 Agent 等执行能力。
- **命令预设**：DEV 中允许哪些普通命令和参数；不会自行打开 DEV。
- **Agent Profile**：服务端定义的 Codex、Claude 或 Pi 启动配置，和 Tunnel Profile 分开。

[返回产品首页](../README.md)
