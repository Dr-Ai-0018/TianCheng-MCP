[手册目录](README.md) · [Windows 首次使用](quickstart-windows.md)

# 开发说明

产品使用从 [首次使用指南](quickstart-windows.md) 开始。本页面向修改源码和验证集成的贡献者。

## 安装开发依赖与构建

在仓库根目录执行：

```powershell
uv sync --frozen --extra test
uv build
```

服务使用锁定的 MCP Python SDK；具体版本以 `pyproject.toml` 和 `uv.lock` 为准。SDK 参考：[源码](https://github.com/modelcontextprotocol/python-sdk)、[文档](https://py.sdk.modelcontextprotocol.io/)。stdout 只传输 MCP 协议，应用日志不写入 stdout。

版本与迁移记录维护在 [CHANGELOG](../CHANGELOG.md)。修改用户路径时同时检查 README、对应手册以及默认/示例配置；不要把本机路径、密钥或研发流水记录写入公共文档。

## 测试

```powershell
Set-Location -LiteralPath '<REPO>'
uv run --frozen python -m pytest --basetemp '.tmp-tests' -q
```

测试覆盖正常读写/覆盖、中文路径、BOM/二进制/截断、`..`、绝对路径、其他盘符、UNC、
不存在写入目标、symlink/junction/reparse point、glob、全文搜索、回收站删除、本地 Git、
审计脱敏、exec secret 清理、TUI 密钥脱敏、隔离 profile 创建、快捷函数幂等安装，以及
真实 stdio `initialize`、`tools/list`、tool call。

### 本地 Agent 真实链路验收

`pytest` 使用 fake runtime，覆盖不到真实 CLI 的参数、进程和轮询行为。修改 Agent
runtime 后，手工执行一次真实验收：

```powershell
uv run python .\scripts\accept_agent_stdio.py
```

它经 `run-mcp-exec.ps1` 走真实 stdio，对 Codex 和 Claude 各完成
create → 写入任务 → 同 session resume → events/result → cancel → close，并且每条
"文件已创建"都用 MCP `read_text` 独立读回验证，不采信模型自报。可用 `--providers`
限定单个 profile，`--providers ""` 只检查工具面而不调用模型。它会真实消耗模型额度，
因此不进入 `pytest`；报告写入被忽略的 `.tmp` 目录。

白名单、`browse` 档和策略热重载另有一份验收：

```powershell
uv run python .\scripts\accept_policy_hotreload.py
```

它使用一次性策略文件，不会修改机器上真实的 `access-policy.json`。覆盖批准前拒绝、
暂存不授予权限、错误 confirmation 被拒、服务端自身目录/盘符根被拒、`browse` 只返回一层、
提升为 `write` 后 Codex/Claude 在工作区外真实写文件并由 MCP 独立读回验证、以及把规则
收窄后立即失效。

## 开发回归与隔离验收

常规回归使用合成配置与工作区，不读取开发机器的真实 Agent 环境文件：

```sh
uv run pytest
uv run python scripts/accept_policy_hotreload.py --workspace "<TEST_WORKSPACE>" --test-root "<OUTSIDE_SOURCE>" --prepare-only
```

`--prepare-only` 只验证独立 Git 工作区和临时策略的准备阶段，不启动 MCP 或模型。
真实热更新验收可去掉该参数，用 `--providers "<CODEX_PROFILE>"` 选择已配置的 profile；
可选 `--codex-model` 仅覆盖这轮验收的模型，不改变全局 Codex 配置。
`--keep` 保留产物；策略、审计和失败诊断均位于独立测试目录，正式白名单不被修改。
真实验收会消耗模型额度，测试目录须与开发源码及正式工作区分开。

Linux 只读拒写及写入对照使用 `scripts/accept_linux_agent_readonly.py`，要求真实命令结果、
随机诊断 token、文件状态及 session 关闭证据，文件没有出现或模型文字不能单独通过。
人工审批使用 `scripts/accept_agent_manual.py --root "<OUTSIDE_SOURCE>" --decision cancel`；
接受测试只有在人工明确批准脚本内的固定命令和指定目录后才可使用
`--decision accept --authorized`。脚本检查命令、cwd 和审批 ID，接受后通过 MCP 独立回读 marker；
取消的原生终态条目可能没有退出码，不将其误判为实际命令执行。

## 源码位置

- `src/tiancheng_mcp/`：MCP 服务、文件/命令权限、运行时与 Agent adapter。
- `config/`：可移植默认配置与本机配置示例；local 文件不跟踪。
- `tc.ps1` 与 `run-mcp*.ps1`：Windows 控制台和能力启动入口。
- `run-mcp.sh` 与 `scripts/linux_tunnel_runtime.py`：Linux 启动和 Tunnel 运维入口。
- `tests/`：自动回归；`scripts/accept_*.py`：独立的真实链路验收。
- `manual/`：公开用户手册；README 提供产品与首次使用入口。
