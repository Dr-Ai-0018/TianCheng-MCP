[手册目录](README.md) · [Windows 首次使用](quickstart-windows.md)

# 权限与命令预设

## 先选需要的能力

| 需求 | 本机入口 | 生效方式 |
| --- | --- | --- |
| 工作区文件与本地 Git | 默认 SAFE | 创建并启动 SAFE Profile |
| 普通命令、远程 Git、Agent 运行 | 主菜单 6 → DEV | 明确确认后重启 |
| 选择命令预设、增删命令 | 主菜单 C | 保存后重启；用 workspace_info 核对运行快照 |
| 外部目录访问 | 主菜单 D 配置路径；Profile 选择 GRANTS 或 GRANTS+EXEC | 按规则和 grant 要求生效 |

命令预设与外部路径权限是两套配置。先选择所需能力，再按下面的规则缩小范围。SAFE 支持工作区文件修改，不表示只读。

## 默认能力与边界

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
   配置由 Git 自身按真实语法解析，不跟随仓库 include。所有专用 Git 工具禁用 fsmonitor、
   hooks、自动签名、自动维护和 ext 协议；SAFE 还禁用全局外部过滤器，并保留用户 Git 身份。
   需要服务端受信任过滤器的流程使用明确启用的 DEV。安全 Profile 不注册任何联网 Git 工具；Dev Profile 则显式
   继承用户/系统 Git config 与 GCM，以便访问 GitHub 等远程仓库。

上述边界的目标是保证 MCP 文件和 Git 工具不会把用户提供的路径解析到
`<WORKSPACE>` 之外。安全检查故意保守：即使链接最终仍指向工作区内部，也会拒绝。

## 命令白名单与开发预设

普通 `run_command`、`start_process`、`external_run_command` 及其后台调用共用启动时加载的命令策略。
SAFE/DEV 仍决定是否开放执行，选择预设或添加命令不会自行打开执行。
专用 Git 工具和固定模板 Agent 使用自己的能力边界，不受此普通命令名单控制。

| 预设 | 普通命令能力 |
| --- | --- |
| `minimal` | 只允许已安装 Git/rg 的完整参数 `--version`；日常文件与 Git 操作使用已有专用工具 |
| `balanced`（默认） | 与原 DEV 工具名单兼容，允许 Python、Node、uv 等开发代码运行；不是 OS sandbox |
| `elevated` | balanced 加当前平台已安装 Shell：Windows 的 pwsh/powershell/cmd，POSIX 的 bash/sh |
| `unrestricted` | elevated 加任意程序名或绝对程序路径；保留本机直接调用禁用规则 |

主菜单 `C` 或 `tc -Action commands` 可以查看工具可用性、切换预设、添加内置工具别名或自定义规则、
禁用命令、撤销禁用、移除本机添加和恢复默认。合法修改即时保存，**重启 MCP 后生效**。
`tc -Action commands -Json` 查看下次启动配置（`configuration_role=next_start`）；运行实例的实际快照看
`workspace_info.command_policy`（`configuration_role=runtime_snapshot`）。管理命令无法判断运行实例的执行开关，
因此管理状态的 `execution_enabled` 为 `null`；运行实例返回实际布尔值。
两处 `policy_revision` 不同表示有效规则配置不同，需要重启采用；相同只代表规则配置一致，
不代表 PATH、程序安装状态或程序内容相同。修改文件不会改变正在运行实例的快照。
拒绝信息保留 `PermissionError`，并提供稳定代码和处理提示：`EXEC_DISABLED`（执行总开关未开放）、
`COMMAND_DISABLED`（本机禁用）、`COMMAND_NOT_ALLOWED`（预设未允许）、`COMMAND_UNAVAILABLE`（程序未找到）、
`ARGUMENTS_NOT_ALLOWED`（精确参数不匹配）；这些策略拒绝消息不回显输入参数、凭据或自定义程序路径。
`elevated` 增加显式 Shell；`unrestricted` 取消普通命令名单。两档使用 MCP 的宿主账户，均不申请管理员权限。
`unrestricted` 支持启动时 PATH 绝对目录中的程序名，以及绝对可执行文件路径（包括工作区程序）。
Windows 只直接执行 `.exe`，`.cmd/.bat/.ps1` 通过显式解释器运行；POSIX 需要可执行权限。
名称调用仍优先应用本机 `add` 的固定前缀和精确参数，禁用按直接调用别名或绝对路径文件名生效。
绝对路径调用不使用别名参数模板；别名/路径是不同调用方式，禁用不递归限制 Shell 或子进程。
`workspace_info.command_policy.mode` 报告模式；无限制下命令列表只列配置规则，不能枚举全部程序。

例如 elevated 下可显式调用 `command="pwsh"`、`args=["-NoProfile", "-Command", "Write-Output 'hello'"]`；
POSIX 使用 `command="bash"`、`args=["-c", "printf 'hello\\n'"]`。
服务器始终使用分离的 argv 和 `shell=False`；Shell 解析只发生在调用者选定的解释器内。
unrestricted 支持直接选择绝对程序路径，但仍检查执行开关、cwd 授权、参数大小和直接 Git/gh 凭据输出。
本机禁用只控制直接入口；允许 Shell 或开发代码时，不能据此保证某个程序或系统操作永远无法发生。


默认预设在 `config/command-policy.defaults.json`，也随 wheel 打包。
本机覆盖在忽略的 `config/command-policy.local.json`，示例见 `config/command-policy.local.example.json`。
通过 launcher 的 `commandPolicyPath` 或 CLI 的 `--command-policy <绝对路径>` 可选择独立配置；
配置必须位于工作区外。合并规则是“预设 + add，最后 disable”，禁用优先。
切换预设会保留本机添加和禁用；若要使用纯预设，先恢复默认清除覆盖，再选择预设。
禁用作用于命令别名的直接调用，不拦截已授权程序启动的子进程；自定义规则可能扩大原预设能力。
只删 `add` 中的条目会恢复同名预设规则；要真正关闭该命令，应加入 `disable`。

```json
{
  "schema_version": 1,
  "preset": "balanced",
  "add": {
    "git-version": {"builtin": "git", "arguments": {"exact": [["--version"]]}}
  },
  "disable": ["gh"]
}
```

每条规则必须二选一：`builtin` 引用已知内置工具，或 `argv` 指定可信程序和固定参数数组。
`arguments` 为 `"any"` 或 `{"exact": [["--version"]]}`；exact 匹配完整参数，不是前缀或 Shell 表达式。
自定义 `argv` 首项必须是工作区外、现存、非链接的可执行程序绝对路径；Windows 首项必须是 `.exe`。
pnpm 等脚本包装器须配置可信 Node 的绝对路径，以及 CLI 脚本的固定参数，不能隐式执行 `.cmd/.ps1`。
别名添加到普通名单不会替换固定模板 Agent 的启动程序；任何自定义开发程序仍可能执行代码和访问宿主。
状态不显示固定参数或精确参数内容；配置文件会明文保存这些值，不应放入凭据。
非法配置失败关闭；缺少内置工具只标记为不可用。运行中的实例及它创建的外部目录服务共享策略快照。

## 重要：路径 jail 不等于 OS sandbox

`run_command` 能启动 Python、Node、Git、GitHub CLI、Codex、ripgrep、uv 等开发工具。任意代码执行本身就可能读取
工作区外文件、访问网络、启动其他程序或产生子进程；仅限制 cwd 和可执行文件名称无法
构成 Windows OS 沙箱。因此：

- exec 默认关闭且 tool 不注册；
- 开启后遵守所选命令策略；前三档使用启动时解析的名单，unrestricted 支持动态程序及绝对路径；
- `command` 与 `args` 分离，始终 `shell=False`；
- cwd 仍需通过 workspace jail；
- minimal/balanced 默认不开放 Shell；elevated/unrestricted 显式允许 Shell，其系统访问由宿主账户决定；
  需要回收站语义的删除应使用 trash-aware `delete` 工具；
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

## 聊天内动态外部授权（可选）

默认仍严格限制在 `<WORKSPACE>`。使用 `run-mcp-grants.ps1`（或命令行参数
`--allow-external-grants`）后，ChatGPT 才能申请临时外部目录能力：先调用
`external_access_request`，让用户在聊天中明确确认后，再提交 `request_id + challenge + confirmation="批准"`
给 `external_access_approve`。challenge 是一次性随机值，不是密码；不要在同一个模型/MCP
通道中传递任何第二因子。
授权只存在当前 MCP 进程内存，最多 10 分钟；`external_grant_status` 可查看状态，
`revoke_external_access` 可由 ChatGPT 主动立即撤销，`external_access_cancel`
可取消尚未批准的请求。MCP/Tunnel 重启后全部失效。

活动 grant 最多3项，待批准请求最多16项；批准时重新检查当前策略、目录和容量。
grant 不能覆盖静态 `deny`；授权根后来被禁止时，grant 失效并取消关联后台任务。
父目录的授权也不会放宽子目录规则：搜索、列目录和 glob 省略不可访问节点，
copy/move/delete 在修改前检查整个源树，copy/move 同时检查派生的目标路径。

若未来引入第二因子，审批必须走模型与 MCP 均无法读取的独立通道；当前聊天 challenge
只用于把用户的明确确认绑定到一次待审批请求，不构成独立的身份验证因子。
外部 grant 的读写、删除和 exec 权限彼此独立；`external_run_command` 仍是开放世界
能力，路径 jail 不等于 Windows OS sandbox。

服务会从
`config/access-policy.json` 加载静态规则，使用最长路径匹配、`deny` 优先和 fail-closed 校验，
并在 `workspace_info` 报告规则摘要。启用 external grants 后，匹配到
`require_approval=false` 的静态外部规则会直接签发受限 grant；对应 `external_*` 工具也可以
省略 `grant_id`，直接使用白名单允许的绝对路径，不会绕过现有路径检查或能力限制。默认行为
仍只有 `<WORKSPACE>`。

传入 `grant_id` 时，`external_*` 的 `path`、`base_path` 和 `cwd` 可以是授权根目录内的
绝对路径，也可以是相对授权根目录的路径；默认的 `.` 指授权根目录。省略 `grant_id`
时必须传入静态策略允许的绝对路径。两种方式都拒绝越界路径和重解析点。

TUI 主菜单的“D. 外部路径白名单 / 访问策略”已经可以查看、新增、编辑、启用/禁用和删除规则，
并提供“测试路径权限”和“验证策略并提示 reload”。保存时会先生成同目录 `.bak` 快照，使用
临时文件原子替换并尝试收紧 Windows ACL；策略加载器验证失败会自动恢复上一份有效快照。
保存后可调用 `access_policy_reload` 读取文件并更新运行中 MCP 的策略快照，也可以重启 MCP；
该工具不会编辑策略文件或批准新的授权，有审批要求的规则仍必须走 grant 流程。

## 白名单模式与 Agent 工作目录

规则的 `mode` 有五档：`deny`、`browse`、`read`、`write`、`full`。

`browse` 只允许列目录，且每次只返回一层。可以逐级下钻——列授权根、进入某个子目录、再列一层——
但读不到任何文件内容，也不能写或执行；`browse` 与 `allow_exec` 不能同时设置。它的用途是让
调用方先看清目录结构，再决定把哪些目录提升进白名单。

Agent 的工作目录可以超出 `<WORKSPACE>`。白名单覆盖的任何目录都可以承载 Codex/Claude 会话：
`read-only` 需要规则具备 `read`，`workspace-write` 需要 `write`，因此 `browse` 规则不能跑 Agent。
启动 Agent 不算 `exec`——命令模板由服务端固定并限定在该目录，与任意 `external_run_command`
分开授权，你可以只开 Agent 而不开任意命令执行。

每次 run 之前都会重新校验工作目录授权，续接的历史会话也一样。规则被撤销、缩小或换根时，
下一轮立即拒绝，不会继续在原目录执行。

两条使用前提：

- `external_*` 文件工具只在开启 external grants 时注册。只开热重载可以把目录写进白名单，
  但没有文件工具能操作它，只有 Agent 能用；要完整可用请使用 GRANTS 或 GRANTS+Exec Profile。
- Codex 默认拒绝在非 git 仓库的目录中运行（`Not inside a trusted directory`）。确有需要时，
  可在单次 `agent_run(start)` 的 `codex_options` 中明确设置
  `skip_git_repo_check: true`；这只关闭 Codex 内层仓库检查，不会绕过 TianCheng 的 cwd、路径或
  access-policy 校验。Claude 不受这项 Codex 检查影响。

## 热重载模式（高危，默认关闭）

默认是冷重载：白名单只能在 TUI 里修改，改完重启 MCP 或调用 `access_policy_reload` 生效。

加上 `--allow-policy-hot-reload` 后会额外注册
`access_policy_change_request/approve/cancel/status` 四个工具，让人不在电脑前也能
在对话里扩大授权。临时授权则使用
`external_access_request/approve/cancel`，不会写入策略文件：

1. 调用方用 `access_policy_change_request` 提交目录和模式，得到一次性 challenge——**这一步不授予任何权限**；
2. 把确切路径和模式念给用户，等用户明确答复；
3. 用 `access_policy_change_approve` 提交 `request_id`、challenge 和 `confirmation='批准'`；
4. 服务端原子写入 `access-policy.json`（保留 `.bak`）并立即生效，不重启。

以下目标永远拒绝：服务端自身目录（代码、策略、日志）、盘符根目录、Windows 系统目录、
路径中含 credential/secret/key 等敏感组件的目录，以及被显式 `deny` 规则覆盖的路径。
批准时会重新校验一次，避免暂存期间策略已被改动。

请清楚这一档的实际含义：challenge 会返回给调用方，所以「必须用户批准」是一道**对话层面的
约定**，而不是密码学上的强制。它默认关闭，只有你明确开启时才存在。
