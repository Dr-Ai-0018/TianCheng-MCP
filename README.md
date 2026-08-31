# TianCheng Local MCP

面向 Windows 11 / PowerShell 7 的本地 stdio MCP Server。它只向 MCP 客户端暴露
**你自己指定的一个工作区目录**内的文件与本地 Git 能力；服务端代码、依赖和审计日志留在
仓库目录里，不在客户端可写的工作区内。

工作区没有内置默认值，必须由你显式配置——它就是这个项目的安全边界，猜错的代价是
把错误的目录暴露出去。

当前版本：`0.9.0`。依赖锁定到官方维护的 MCP Python SDK `2.1.0`，使用当前
`MCPServer`、`MCPServer.tool()`、`ToolAnnotations` 与 stdio transport API。

- MCP Python SDK：<https://github.com/modelcontextprotocol/python-sdk/tree/v2.1.0>
- SDK v2 文档：<https://py.sdk.modelcontextprotocol.io/>
- OpenAI Secure MCP Tunnel：<https://developers.openai.com/api/docs/guides/secure-mcp-tunnels>

版本策略遵循 SemVer：补丁版本（`x.y.Z`）只修复兼容性 bug 或文档/测试问题；小版本（`x.Y.0`）增加向后兼容的工具、参数或运行能力；大版本（`X.0.0`）用于破坏现有调用契约、默认安全边界或需要迁移的变更。每次发布同步更新
`pyproject.toml`、`src/tiancheng_mcp/__init__.py`、`uv.lock`、README 和
`CHANGELOG.md`。

## 架构

```text
ChatGPT Local MCP plugin
        |
OpenAI Secure MCP Tunnel
        |
tunnel-client v0.0.12
        | stdio JSON-RPC (UTF-8)
run-mcp.ps1
        |
TianCheng Local MCP
   |-- WorkspaceJail  --> <YOUR_WORKSPACE_PATH> only
   |-- file/search/git tools
   `-- audit log      --> <REPO>\logs
```

服务不需要 OAuth、HTTP Server、LLM API 或模型权限。stdout 只用于 MCP 协议；
应用日志不写 stdout，审计日志写到项目自己的 `logs` 目录。

## 工具

默认启动注册 31 个工具：

| 分类 | 工具 | 说明 |
| --- | --- | --- |
| 信息 | `workspace_info` | 工作区、版本、能力、exec/Git 状态；不返回用户、环境变量或主机信息 |
| 文件读取 | `list_dir`, `stat`, `hash_file`, `read_text`, `read_text_chunk` | 有递归深度、读取量和二进制拒绝限制；大文件可用稳定字节游标续读；哈希有大小上限 |
| 文件写入 | `write_text`, `edit_text`, `append_text`, `mkdir`, `move`, `copy` | UTF-8；覆盖/精确替换采用同目录临时文件 + `os.replace`；写入和追加支持 SHA-256 乐观锁 |
| 回收 | `delete`, `trash_list`, `trash_restore`, `trash_purge` | 默认可恢复；只有显式 purge 永久销毁并标成 destructive |
| 查找 | `glob`, `search_text` | `search_text` 优先使用 ripgrep，先枚举受 ignore 规则约束的候选文件并按 glob 过滤，再按总扫描字节、结果、输出和超时限制 |
| 本地 Git | `git_status`, `git_diff`, `git_log`, `git_init`, `git_add`, `git_commit` | 默认安全 Profile 只提供工作区内的本地仓库操作 |
| 长任务控制 | `job_status`, `job_result`, `job_cancel`, `job_list` | 任意工具超过交互预算会自动转为后台 job；通过 job_id 查询、分页读取或取消 |
| 本地 Agent Catalog | `agent_catalog` | 只查询已由本地配置授权的 Codex/Claude 会话 metadata；不能添加路径或读取原始 transcript |

`run_command` 默认根本不注册。只有使用 `run-mcp-exec.ps1` 或命令行
`--allow-exec` 时，才额外注册 `git_remote_list/add/set_url/remove`、
`git_clone/fetch/pull/push`、`run_command`，以及
`start_process/process_status/process_output/process_input/list_processes/stop_process`、
`agent_session/agent_run`，共 48 个工具。
Dev Profile 会复用当前
Windows 用户的 Git 配置、Git Credential Manager 和 GitHub CLI 登录；remote URL 不得
内嵌密码或 token，`git credential*` 与 `gh auth token` 这类直接输出凭据的入口会被拒绝。
当前 SDK annotations 已为
只读、写入、destructive 和 open-world 工具分别标注；`delete` 即使采用回收站也标为
destructive，`run_command` 同时标为 destructive/open-world。

### 聊天内动态外部授权（可选）

默认仍严格限制在 `<YOUR_WORKSPACE_PATH>`。使用 `run-mcp-grants.ps1`（或命令行参数
`--allow-external-grants`）后，ChatGPT 才能申请临时外部目录能力：先调用
`request_external_access`，让用户在聊天中明确确认后，再提交 `request_id + challenge + confirmation="批准"`
给 `approve_external_access`。challenge 是一次性随机值，不是密码，也不是 TOTP 密钥。
授权只存在当前 MCP 进程内存，最多 10 分钟；`external_grant_status` 可查看状态，
`revoke_external_access` 可由 ChatGPT 主动立即撤销，`cancel_external_access_request`
可取消尚未批准的请求。MCP/Tunnel 重启后全部失效。

旧版 TOTP 初始化工具仍可用于本机保管第二因子，但聊天授权流程不要求把 TOTP 传给 MCP。
不要把验证码或密钥写进普通文件、提交到 Git，或粘贴到其他日志。
外部 grant 的读写、删除和 exec 权限彼此独立；`external_run_command` 仍是开放世界
能力，路径 jail 不等于 Windows OS sandbox。

0.6.2 已完成白名单策略引擎 Phase A/B/C：服务会从
`config/access-policy.json` 加载静态规则，使用最长路径匹配、`deny` 优先和 fail-closed 校验，
并在 `workspace_info` 报告规则摘要。启用 external grants 后，匹配到
`require_approval=false` 的静态外部规则会直接签发受限 grant；对应 `external_*` 工具也可以
省略 `grant_id`，直接使用白名单允许的绝对路径，不会绕过现有路径检查或能力限制。默认行为
仍只有 `<YOUR_WORKSPACE_PATH>`。

TUI 主菜单的“D. 外部路径白名单 / 访问策略”已经可以查看、新增、编辑、启用/禁用和删除规则，
并提供“测试路径权限”和“验证策略并提示 reload”。保存时会先生成同目录 `.bak` 快照，使用
临时文件原子替换并尝试收紧 Windows ACL；策略加载器验证失败会自动恢复上一份有效快照。
保存后可调用 `access_policy_reload` 立即生效，也可以重启 MCP；有审批要求的规则仍必须走 grant 流程。

### 白名单模式与 Agent 工作目录

规则的 `mode` 现在有五档：`deny`、`browse`、`read`、`write`、`full`。

`browse` 只允许列目录，且每次只返回一层。可以逐级下钻——列授权根、进入某个子目录、再列一层——
但读不到任何文件内容，也不能写或执行；`browse` 与 `allow_exec` 不能同时设置。它的用途是让
调用方先看清目录结构，再决定把哪些目录提升进白名单。

内置两个 profile：`codex-default` 与 `claude-default`，分别驱动标准 Codex CLI 和
Claude Code CLI，命令模板由服务端固定。

`codex-default` 默认**不**传 `-p`，即使用你 Codex 的默认配置。如果你在本机配置了自己的
Codex profile，设置环境变量即可启用，仓库里不会保存任何人的私有 profile 名：

```powershell
$env:TIANCHENG_CODEX_PROFILE = 'your-profile-name'
```

该值只接受裸 profile 名（首字符为字母或数字，其后可含字母、数字、点、短横、下划线，最长
64 字符）。含空格、斜杠或以 `-` 开头的值会被直接拒绝，避免往固定命令模板里塞进额外参数。

Agent 的工作目录不固定为工作区。白名单覆盖的任何目录都可以承载 Codex/Claude 会话：
`read-only` 需要规则具备 `read`，`workspace-write` 需要 `write`，因此 `browse` 规则不能跑 Agent。
启动 Agent 不算 `exec`——命令模板由服务端固定并限定在该目录，与任意 `external_run_command`
分开授权，你可以只开 Agent 而不开任意命令执行。

每次 run 之前都会重新校验工作目录授权，续接的历史会话也一样。规则被撤销、缩小或换根时，
下一轮立即拒绝，不会继续在原目录执行。

两条使用前提：

- `external_*` 文件工具只在开启 external grants 时注册。只开热重载可以把目录写进白名单，
  但没有文件工具能操作它，只有 Agent 能用；要完整可用请使用 GRANTS 或 GRANTS+Exec Profile。
- Codex 拒绝在非 git 仓库的目录中运行（`Not inside a trusted directory`）。服务端不会传
  `--skip-git-repo-check`，因为那是 Codex 自己用于保证改动可回滚的安全网。把普通目录加入
  白名单后 Codex 仍会拒绝，Claude 不受影响。

### 热重载模式（高危，默认关闭）

默认是冷重载：白名单只能在 TUI 里修改，改完重启 MCP 或调用 `access_policy_reload` 生效。

加上 `--allow-policy-hot-reload` 后会额外注册 `access_policy_change`，让人不在电脑前也能
在对话里扩大授权：

1. 调用方 `request` 提交目录和模式，得到一次性 challenge——**这一步不授予任何权限**；
2. 把确切路径和模式念给用户，等用户明确答复；
3. 用 `approve` 提交 `request_id`、challenge 和 `confirmation='批准'`；
4. 服务端原子写入 `access-policy.json`（保留 `.bak`）并立即生效，不重启。

以下目标永远拒绝：服务端自身目录（代码、策略、日志）、盘符根目录、Windows 系统目录、
路径中含 credential/secret/key 等敏感组件的目录，以及被显式 `deny` 规则覆盖的路径。
批准时会重新校验一次，避免暂存期间策略已被改动。

请清楚这一档的实际含义：challenge 会返回给调用方，所以「必须用户批准」是一道**对话层面的
约定**，而不是密码学上的强制。它默认关闭，只有你明确开启时才存在。

## 在新设备上部署

### 1. 前置条件

- Windows 11
- PowerShell 7（`pwsh`，需在 PATH 上）
- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Git（可选，本地 Git 工具需要它）
- OpenAI tunnel-client（只有走 Secure MCP Tunnel 时才需要）

### 2. 取得代码

公开仓库的 clone 与 pull **不需要 GitHub token**：

```powershell
git clone https://github.com/Dr-Ai-0018/TianCheng-MCP.git
Set-Location -LiteralPath .\TianCheng-MCP
```

### 3. 安装依赖

```powershell
uv sync --frozen --extra test
```

### 4. 选定工作区

工作区是这个服务器唯一可以触碰的目录。**它没有默认值**，配置缺失时启动会直接报错，
而不会退回到任何人的机器路径。

```powershell
New-Item -ItemType Directory -Path '<YOUR_WORKSPACE_PATH>' -Force | Out-Null
```

不要把仓库自身或其父目录设为工作区。

### 5. 生成本机配置

```powershell
Copy-Item .\config\launcher.local.example.json .\config\launcher.local.json
```

然后编辑 `config\launcher.local.json`，至少填写 `workspace`。`launcher.local.json`
已被 `.gitignore` 排除，**机器专属路径只应该出现在这个文件里**。

`tunnelClient` 与 `powerShell` 留空即表示从 PATH 上自动探测；只有当它们不在 PATH 上时
才需要写绝对路径。也可以用环境变量 `TIANCHENG_WORKSPACE` 覆盖工作区。

### 6. 选择运行档位

| 档位 | 启动方式 | 能力 |
| --- | --- | --- |
| SAFE | `.\run-mcp.ps1` | 只有工作区内的文件与本地 Git，**默认档** |
| GRANTS | `.\run-mcp-grants.ps1` | 增加聊天内动态外部授权（一次性 challenge + 显式确认） |
| DEV | `.\run-mcp-exec.ps1` | 增加白名单 `run_command` 与受管进程 |
| GRANTS+Exec | `.\run-mcp-grants.ps1 -AllowExec` | 同时开启上面两项，风险最高 |

热重载（`-AllowPolicyHotReload`）是可选的高危附加项，默认关闭。

### 7. 为这个节点建立独立的 Tunnel

**每台新设备都必须有自己独立的一套**，不要复用别的机器的：

- 独立的 `tunnel_id`
- 独立的 runtime / control-plane API key
- 独立的 tunnel-client profile

ChatGPT 端也要**新建一个独立的 MCP 连接器实例**并绑定到这个新 Tunnel。

任何 key、Tunnel ID 或连接器 ID 都**不要提交进 Git**。

### 8. 验证

```powershell
uv run pytest -q
uv run python .\scripts\smoke_stdio.py
```

`smoke_stdio.py` 需要 `TIANCHENG_WORKSPACE` 指向一个可写的测试目录。

### 本地文件的存放边界

以下文件都已被 `.gitignore` 排除，**只属于本机**：

| 文件 | 内容 |
| --- | --- |
| `.env` | 明文本地变量（不是加密保险箱） |
| `config/launcher.local.json` | 本机路径、工作区、profile 名 |
| `config/access-policy.json` | 白名单规则 |
| `config/agent-sources.json` | Agent 历史来源授权 |
| `state/agent-catalog.sqlite3` | Agent 会话 metadata 索引 |
| `logs/` | 审计日志 |
| `exec-env.allowlist` | 允许透传的环境变量**名**（不存值） |

### 需要先知道的三条限制

1. **默认不读取任何真实 Agent 历史。** 出厂 source 数为 0，必须由你在本机显式授权
   来源后才会扫描。
2. **热重载在 ChatGPT 里不保证可用。** `access_policy_change_confirm` 会被 ChatGPT
   连接器的安全层拦下，这是平台侧行为，不是本项目的缺陷。该能力目前只在本地 stdio
   客户端验证通过。
3. **路径 jail 不等于 OS sandbox。** 见下方「重要：路径 jail 不等于 OS sandbox」。

## `tc` 中文控制台

安装器会在 PowerShell `CurrentUserAllHosts` Profile 中加入一个带边界标记的幂等函数块，
不会覆盖原有 Profile 内容：

```powershell
.\install-tc.ps1
```

如果你仍想额外配置本机 TOTP（聊天 challenge 流程并不要求），可以运行二维码初始化：

```powershell
tc -Action totp-setup
```

上面的命令会在本机打开二维码；用 2FA 软件扫描后输入一次 6 位验证码，验证成功
才会把密钥写入项目 `.env`。二维码默认会在验证成功后删除。当前聊天授权使用
的是 MCP 返回的一次性 challenge，不需要把 TOTP 作为工具参数传递。也可以直接执行：

```powershell
pwsh -NoLogo -NoProfile -File .\tc.ps1 -Action totp-setup
```

重开 PowerShell 后直接输入：

```powershell
tc
```

菜单支持：

- 一键执行 `doctor`，成功后在当前窗口运行 Tunnel；Tunnel 自动拉起 stdio MCP；
- 在独立可见 PowerShell 窗口启动；
- 创建/重建、选择和编辑多个 tunnel-client profile；
- 当前进程、Windows 用户环境变量和项目 `.env` 三种密钥方式；
- 从真实 profile YAML 的 `mcp.commands[].command` 判断 SAFE/DEV，不再依赖易漂移的名单；
- 一键切换 SAFE/DEV、显示实际运行中的 profile、停止/重启 Tunnel；
- Profile 管理中可直接切换“聊天外部授权（TOTP）”或“外部授权 + Exec”，无需手动编辑 YAML；
- 状态检查同时显示 Git、GCM、`gh` 可用/登录布尔状态，不输出账号 token；
- 管理 UI、非敏感启动器设置和显式 Dev profile；
- “E. 本地 Agent / 会话源管理”可探测 Codex/Claude CLI 与固定历史根，并由用户显式添加、启停、删除、验证、刷新或重建 metadata Catalog；
- `tc -Action info|profiles|key-status|status|agents -Json` 非交互诊断。

首次运行时，本机尚无 profile；进入 **Profile 管理 → 创建或重建 Profile**，填写现有
`tunnel_...` ID 即可。安全模式为默认选择。Exec 模式需要输入两次醒目确认，并在以后
每次启动时再次确认。

API key 加载优先级为：当前进程 → Windows 用户环境变量 → `.env`。状态界面只显示
“是否已配置”和来源，从不显示值。`.env` 位于项目根目录、已被 `.gitignore` 排除，写入
后会尝试移除继承 ACL 并只授权当前 Windows 用户；它仍然是明文文件，不是加密保险箱。
`.env` 只由 `tc` 加载，直接执行 tunnel-client 不会自动读取它。

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

### 显式透传一个业务环境变量

DEV 子进程默认仍然拿不到任意业务 secret。如果确实需要让某个开发工具读取一个变量，
只把变量名加入项目目录外的执行白名单文件（文件只存名称，不存值）：

```powershell
Set-Content -LiteralPath .\exec-env.allowlist `
  -Value '# names only','EXAMPLE_SERVICE_KEY' -Encoding utf8
.\run-mcp-exec.ps1
```

也可以直接使用 CLI 参数：

```powershell
.\.venv\Scripts\python.exe -m tiancheng_mcp `
  --workspace '<YOUR_WORKSPACE_PATH>' --audit-dir .\logs `
  --allow-exec --pass-env EXAMPLE_SERVICE_KEY
```

`--pass-env` 可以重复使用，但只接受合法环境变量名；`CONTROL_PLANE_API_KEY`、OpenAI
控制面 key、PATH/SystemRoot/TEMP 等策略变量永远不能透传。值不会写入审计日志或 workspace，
但被透传的程序本身可以读取、打印或上传它，所以这仍属于 DEV 高风险能力。修改白名单后，
必须重启 Tunnel/MCP；ChatGPT 端再点击 **Local MCP → 刷新**。

受控 Exec stdio smoke：

```powershell
uv run python .\scripts\smoke_exec_stdio.py
```

新建 profile 默认使用 `run-mcp.ps1`（SAFE）。需要完整开发权限时，可在
**Profile 管理 → 一键切换当前 Profile 为 DEV**，它会把真实 profile 的 MCP command
改成 `run-mcp-exec.ps1`；切回 SAFE 同理。DEV 启动时仍会显示一次风险确认。

## 安全边界

每个文件路径必须是相对于 workspace 的路径。校验并非字符串 `startswith()`：

1. 拒绝绝对路径、盘符、UNC、extended/device path、`..`、NUL、ADS 冒号、Windows
   设备名与易产生别名的尾随点/空格。
2. 对现有目标逐段检查，并解析 canonical path 后做 case-insensitive common-path 校验。
3. 对尚不存在的写入目标，解析最近的现有父目录，再校验目标仍位于 canonical root。
4. 工作区根目录以及其下任何 symlink、directory junction 或 reparse point 都被拒绝；
   递归 copy/move 和 Git 仓库还会先扫描整棵目标树。
5. move/copy 不覆盖现有目标；目录不能被复制或移动到自身内部。
6. Git 只支持拥有真实 `.git` 目录的独立仓库。拒绝 worktree `.git` 文件、object
   alternates、reparse point、include/filter/credential/alias 等危险仓库本地配置；专用
   commit 禁用 hooks 与签名。安全 Profile 不注册任何联网 Git 工具；Dev Profile 则显式
   继承用户/系统 Git config 与 GCM，以便访问 GitHub 等远程仓库。

上述边界的目标是保证 MCP 文件和 Git 工具不会把用户提供的路径解析到
`<YOUR_WORKSPACE_PATH>` 之外。安全检查故意保守：即使链接最终仍指向工作区内部，也会拒绝。

### 重要：路径 jail 不等于 OS sandbox

`run_command` 能启动 Python、Node、Git、GitHub CLI、Codex、ripgrep、uv 等开发工具。任意代码执行本身就可能读取
工作区外文件、访问网络、启动其他程序或产生子进程；仅限制 cwd 和可执行文件名称无法
构成 Windows OS 沙箱。因此：

- exec 默认关闭且 tool 不注册；
- 开启后只允许启动时解析出的 allowlist 命令，禁止传入 executable path；
- `command` 与 `args` 分离，始终 `shell=False`；
- cwd 仍需通过 workspace jail；
- `cmd`、PowerShell、`del`、`rm` 等 shell/直接删除入口不在 allowlist；删除应走
  trash-aware 的 `delete` 工具；
- Git 与 `gh` 在 Dev Profile 中可执行本地和远程开发工作流，并复用用户 Git config/GCM；
  只有会直接输出 keyring token 的 credential plumbing 命令被硬拒绝；
- 子进程环境使用最小 allowlist，默认不继承 `CONTROL_PLANE_API_KEY` 或其他环境 secret；只有
  启动时显式配置的 `--pass-env NAME` 才会透传对应变量；
- Python、uv、pip、npm 缓存和临时目录重定向到 workspace 内的 `.tiancheng-tmp`；
- 默认 timeout 60 秒，最大 300 秒；stdout/stderr 分别有内存与返回上限；
- Windows 使用 kill-on-close Job Object；超时会终止整个子进程树，正常退出也不允许遗留
  后台 daemon；
- 需要开发服务器等长任务时使用 `start_process`；最多同时 32 个、默认最长 1 小时，
  stdout/stderr 只保存在有上限的内存尾部缓冲区，支持 `after_bytes` 增量游标和
  `process_input` UTF-8 stdin，MCP 退出时会回收进程树；
- 审计日志只记 tool/cwd，不记录 command args。

allowlist 只约束首个 executable，不约束程序内部行为，也不会把参数伪装成路径牢笼。
Python、Node、Git、`gh`、pytest、npm script 或项目代码都可以动态构造路径、访问网络或
调用系统 API，因此 Dev/Exec Profile 代表“允许任意代码执行”，不是强安全边界。

需要真正阻止任意代码访问工作区外资源时，应在单独的低权限 Windows 用户、VM、
Windows Sandbox 或同等级 OS 隔离环境中运行，而不是开启本工具的 exec 模式。

## 可靠性限制

### 自动后台兜底

大多数可能耗时的工具调用都会先进入受控 job worker，并默认最多等待 75 秒返回同步结果；
`workspace_info/stat/read_text` 等有严格上限的轻量工具会直连执行，以便在重型任务运行时仍能恢复。超过交互预算时，
服务会在 Tunnel deadline 之前返回 `execution=background` 和 `job_id`；原任务继续由后台 worker
管理。使用 `job_status` 查看状态，使用 `job_result` 读取完成结果，使用 `job_cancel` 请求取消。
这不是把 Tunnel 的单次响应期限调大，而是在期限前释放 MCP 请求。写入、删除、移动、Git 提交和
命令执行等副作用操作必须以 job_id 为准确认最终状态，不能因为客户端 timeout 就重复提交。

取消是协作式但有强制收尾：扫描循环会立即检查取消信号，ripgrep/Git/run_command 会终止
其 Windows 进程树；撤销 external grant 会取消关联 job，并使已完成的外部结果失效。单个
Python 扩展若完全不检查取消信号，线程无法被 CPython 安全地强杀，因此服务会保留其状态
而不会伪造“已停止”。job 状态、结果和取消接口走轻量直连，不会等待重型 worker 队列。

写入、移动、复制、删除、trash 恢复/清理、Git 配置/clone/fetch/pull/push、Git 提交和命令执行工具支持可选的 `idempotency_key`。调用方在
无法确定上一次请求是否到达时，应使用同一个 key 重试；服务会复用原 job/result。相同 key
绑定不同参数会被拒绝，避免因为网络重试重复产生副作用。

`start_process` 是显式的常驻进程 API，返回稳定的 `session_id` 和兼容用的 `process_id`；使用
`process_status`/`process_input`/`process_output` 管理生命周期和增量输出。它不会伪装成
一次性 job，重复调用会按请求启动新的进程，需由调用方自行避免重复启动。

0.7.1 增加服务器自有的 `codex-default` profile registry 和有界 Codex JSONL 事件解析器，
作为后续 `agent_session`/`agent_run` 的安全基础；本版本尚未暴露任意 Agent 命令工具。

0.8.0 暴露受控的 `agent_session`/`agent_run` MVP：仅允许服务器内置 `codex-default`，
session cwd 固定在工作区，sandbox 只允许 `read-only`/`workspace-write`，运行立即返回
`run_id`，事件通过分页 cursor 获取。0.8.1 补齐 `inspect`、独立 `result`、最长 10 秒的
`events(wait_ms)`、同 session 自动 resume，以及失败/超时/取消/shutdown 的明确终态。
事件最多保留 2,000 条，session/run 分别限制为每个 MCP 进程 128 个、每个 session 100 次；
stderr、错误摘要和最终消息都会截断并脱敏。尚未支持任意 profile、config override、
交互式 steer、持久化 task/job 或 `danger-full-access`。

当前 `main` 的 0.9 开发阶段已把通用 session/run runtime 与 provider adapter 分离：
`workspace_info` 会返回服务器已注册 provider 的 machine-readable capabilities，session/run
同时返回 provider-neutral `native_session_id` 和兼容字段 `thread_id`。未声明的
Claude runtime、steer/interaction 等能力仍会稳定拒绝。

0.9b 增加统一 `agent_catalog`：`providers`、`sources`、`list`、`inspect` 是轻量查询，
`refresh` 在超过交互预算时自动转后台 job。Catalog source 与普通文件权限、external grant
完全分离，只接受本地 `config/agent-sources.json` 中已验证的 `catalog-read` 根；MCP 工具没有
新增/编辑 source 的参数。索引只持久化 provider、native session id、时间、受控 cwd 显示和
安全 fallback title，不保存 prompt、reasoning、tool output 或 transcript 正文。默认仍没有
真实 source；TUI 只探测固定 `.codex/sessions` / `.claude/projects` 候选，用户输入 `ADD`
确认后才写入 local-only policy，MCP 端仍不能新增或扩大 source。

0.9c 在 DEV Profile 的 `agent_session` 增加 `attach`：调用方只能提交 Catalog 生成的
`conversation_ref`，不能直接提交 native session id、thread id、`--last` 或历史文件路径。
服务端重新验证 source/root/file identity、provider、session id 与历史 cwd；cwd 不在
`<YOUR_WORKSPACE_PATH>` 时仍可查看安全 metadata，但不能 attach。绑定成功后的第一轮直接使用受控
`codex exec ... resume <native_session_id> <prompt>`，继续沿用服务器 profile、sandbox、cwd、
最小环境和 managed-process 限制。同一原生 JSONL 正常追加后可继续下一轮；source 被禁用、
根或文件被替换、会话 id/cwd 绑定变化时立即 fail-closed。当前仍未配置或扫描真实 source。

0.9d 增加服务器自有 `claude-default` adapter/profile。Claude Code 只在 agent runtime 的
独立 executable registry 中注册，不会因此成为任意 `run_command`。固定命令使用
`claude -p --output-format stream-json --verbose --safe-mode --restricted --no-chrome
--strict-mcp-config --disable-slash-commands`：`read-only` 只开放 Read/Glob/Grep，
`workspace-write` 只增加 Edit/Write，不开放 Bash、PowerShell、任意 settings/agents/plugins、
额外 MCP、`--add-dir` 或危险权限开关。新会话、同 session resume 与授权 Catalog history
attach 共用 Codex 已验证的进程、超时、取消、事件分页和 source binding 机制。已配置的
业务环境变量只进入明确声明需要它的 Codex profile，不会透传给 `claude-default`。当前只完成
fake Claude/合成 history 验证；真实模型 smoke 尚未自动执行。

0.9e 增加 local-only source admin 与 `tc` 菜单入口。CLI/version probe 只执行有界
`--version`，不发送模型请求；source 增删启停全部复用生产 validator 与原子保存，ACL 收紧
失败会恢复上一版。用户可显式刷新单个 source 的 metadata，或在停止 Tunnel/MCP 后把旧
SQLite/WAL/SHM 移为时间戳备份再重建。Catalog 连接均显式关闭，避免 Windows 文件锁与长期
连接泄漏。菜单和 JSON 状态不读取或输出 transcript 正文、key/token；真实模型 smoke 仍需
进入菜单第 7 项并准确输入 `RUN CODEX` 或 `RUN CLAUDE` 二次确认。它使用固定 read-only
prompt、180 秒超时且只返回 marker 是否通过，不输出模型正文。Agent 子进程会立即收到 stdin
EOF，因为 prompt 已作为受控参数传入；普通 command session 的交互 stdin 不受影响。当前已在
Windows 实机验证 `codex-default` 与 `claude-default` marker 均成功。Claude prompt 固定紧跟
`-p`，不会被可变长度 `--tools` 参数吞入工具列表；失败结果会返回脱敏后的实际错误摘要。

| 项目 | 默认 | 硬上限 |
| --- | ---: | ---: |
| 自动后台交互等待 | 75 s | 90 s |
| `read_text` 返回 | 256 KiB | 1 MiB |
| `read_text` 行范围扫描 | 8 MiB | 8 MiB |
| `read_text_chunk` 单块源数据 | 256 KiB | 1 MiB |
| `edit_text` 文件大小 | - | 16 MiB |
| `list_dir` 深度 | 1 | 5 |
| `list_dir` 结果 | - | 1,000 entries |
| `glob` 结果 | 200 | 1,000 |
| `glob` 扫描 | - | 100,000 entries |
| `search_text` 结果 | 100 | 500 |
| `search_text` 总扫描 | 32 MiB | 64 MiB |
| 单个搜索文件 | - | 2 MiB |
| ripgrep 搜索 timeout | 30 s | 120 s |
| `git_diff` | 512 KiB | 1 MiB |
| command stdout/stderr | 各 256 KiB | 各 1 MiB |
| command timeout | 60 s | 300 s |
| managed process 输出缓冲 | 512 KiB | 每流 2 MiB |
| managed process 生命周期 | 1 h | 24 h |
| Agent event retention | - | 每个 run 2,000 条 |
| Agent events 等待 | 0 s | 10 s |
| Agent session/run | - | 每进程 128 session；每 session 100 run |
| Agent Catalog metadata 读取 | - | 每文件 256 KiB；1,000 行；单行 64 KiB |
| Agent Catalog list | 50 records / 128 KiB | 200 records / 512 KiB |

`read_text` 优先 UTF-8，识别 UTF-8 BOM 与 UTF-16 BOM。`read_text_chunk` 返回下一块的
`next_offset_bytes`，调用方应原样续传，避免从多字节字符中间开始。没有 BOM 的 NUL 内容
或非法 UTF 文本会作为二进制拒绝，不会用 replacement character 强行读取。

`edit_text` 必须给出精确旧文本和预期命中次数；不唯一、已变化或 SHA-256 前置条件不匹配
都会拒绝写入。`write_text` 和 `append_text` 也可携带上次返回的 `sha256`，防止静默覆盖或追加到并发修改后的文件。

`hash_file` 只读取 workspace 内文件并返回 SHA-256；默认最多处理 256 MiB，避免对异常大文件
进行无界扫描。

`search_text` 检测到 `rg` 时使用 literal JSON 搜索：默认包含 `.github` 等 hidden 开发文件、
尊重各层 `.gitignore`，并排除 `.git`、`.tiancheng-trash`、`.tiancheng-tmp`、`node_modules`
与 `.venv`。可显式设置 `respect_gitignore=false` 或 `include_internal=true`；路径匹配仍由
服务端二次校验。机器没有 `rg` 时自动回退到有总扫描字节上限的 Python 实现。

## 审计日志

默认文件：

```text
<REPO>\logs\tiancheng-mcp-audit.jsonl
```

每行只含 UTC 时间、tool、相对路径、success/failure、duration 和错误类型。不会记录
文件正文、command args、API key 或环境变量内容。恶意绝对路径在进入日志前会替换为
`<rejected-path>`。日志达到 5 MiB 后自动轮转，最多保留 5 个历史文件。

## 测试

```powershell
Set-Location -LiteralPath '<REPO>'
uv run pytest --basetemp '.tmp-tests' -q
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

## OpenAI tunnel-client v0.0.12

以下命令只在 profile 中保存 MCP 启动命令；默认 secret reference 仍为
`env:CONTROL_PLANE_API_KEY`，不会把 key 值写入 profile。先确保当前 PowerShell 已经按你
现有流程设置该变量，再替换真实 tunnel id：

> Windows 注意：`--mcp-command` 内的路径应使用正斜杠。v0.0.12 的命令解析器会把
> 未正确转义的反斜杠当成转义字符；TUI 会自动完成这项规范化。

把 `<...>` 占位符换成你自己机器上的实际值：

```powershell
Set-Location -LiteralPath '<YOUR_TUNNEL_CLIENT_DIR>'

.\tunnel-client.exe init `
  --sample sample_mcp_stdio_local `
  --profile tiancheng-local `
  --tunnel-id '<YOUR_TUNNEL_ID>' `
  --mcp-command '<YOUR_PWSH_PATH> -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File <YOUR_REPO_PATH>/run-mcp.ps1'

.\tunnel-client.exe doctor --profile tiancheng-local --explain
.\tunnel-client.exe run --profile tiancheng-local
```

保持 `run` 进程运行。然后在 ChatGPT 中点击 **Local MCP → 刷新**，重新发现 TianCheng
工具；原先 embedded stub 的 `echo`、`server_info`、`uppercase` 应被这里的 31 个工具
取代。

## 项目文件

```text
README.md
pyproject.toml
uv.lock
run-mcp.ps1
run-mcp-exec.ps1
tc.ps1
install-tc.ps1
.env.example
run-mcp-grants.ps1
exec-env.allowlist.example
config/
  launcher.defaults.json        # 随仓库提交，只含相对路径
  launcher.local.example.json   # 复制成 launcher.local.json 后填本机值
src/tiancheng_mcp/
  __init__.py
  __main__.py
  agent_adapters.py
  agent_admin.py
  agent_catalog.py
  agent_sources.py
  agents.py
  audit.py
  cli.py
  grants.py
  jobs.py
  policy.py
  security.py
  server.py
  service.py
tests/
  conftest.py
  test_agent_admin.py
  test_agent_catalog.py
  test_agent_sources.py
  test_agents.py
  test_audit.py
  test_cli.py
  test_external_grants.py
  test_files.py
  test_git.py
  test_jobs.py
  test_launcher.py
  test_policy.py
  test_processes.py
  test_security.py
  test_stdio.py
scripts/
  smoke_stdio.py
  smoke_exec_stdio.py
  smoke_jobs.py
  policy_explain.py
  setup_totp.py
  accept_agent_stdio.py
  accept_policy_hotreload.py
```
