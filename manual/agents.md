[手册目录](README.md) · [Windows 首次使用](quickstart-windows.md)

# 本地 Agent

这是可选的进阶功能。先完成文件写读，再启用 DEV，并在宿主机安装、配置需要的原生 CLI 和登录态。Tunnel key 不提供模型访问权限。

## 先选哪种能力

| Provider | 使用范围 |
| --- | --- |
| Codex | 受控 session/run、续跑、历史 attach；可选择人工审批 |
| Claude | 受控文件工具；默认关闭 Bash，可信 Shell 需单独配置 |
| Pi | 无工具单次推理；当前不能读写项目文件或续跑 |

以 `workspace_info` 返回的实际 provider capabilities 和已注册 profile 为准。运行通常按 `agent_session(create)` → `agent_run(start)` → `events/result` → `close` 的顺序进行。结果需要独立核验，不能只采信 Agent 的完成声明。

每个 MCP 进程最多 128 个 session，每个 session 最多 100 次 run，每个 run 最多保留 2,000 条事件。运行超时、关闭与取消见 [后台任务与进程](jobs-and-processes.md)。

当前实现已把通用 session/run runtime 与 provider adapter 分离：
`workspace_info` 会返回服务器已注册 provider 的 machine-readable capabilities，session/run
只返回 provider-neutral `native_session_id`。Codex 原生 JSONL 里的 `thread_id` 仍由解析器读取，
但不会作为 TianCheng 的别名返回。未声明的 provider 能力或选项会明确拒绝。

## 配置 Agent Profile

Agent Profile 名称与 Codex CLI 自己的 config profile 是两层不同概念。服务器内置
`codex-default` 与 `claude-default`，两者复用各自 CLI 已有登录态，不绑定私人 provider 或
凭据变量。可选的本机 `config/agent-profiles.json` 严格加载额外 profile；该文件不由 Git
跟踪，结构参考 `config/agent-profiles.example.json`。v2/v3 默认用 `inherit_defaults=true` 保留可用
provider 的内置 profile，再按同名覆盖、新名追加、`enabled=false` 禁用；因此只配置 Codex
不会再误删内置 `claude-default`。配置文件缺失时安全回退到内置 profile，文件存在但字段、
provider 或认证声明无效时则拒绝启动，不会静默降级。配置只允许声明 provider、真正传给
Codex `-p` 的 `provider_profile`、Pi 的 `pi.provider`/`pi.model`、专用 `codex_home`、`windows_home_preflight` 检查策略和受控 `auth`，不能声明 executable、
argv 或任意环境变量表。示例中的隔离 profile 展示了如何绑定独立 `CODEX_HOME`；调用方只能
选择已经由服务端注册的名称，不能在 `agent_session` 或 `agent_run` 中提交、覆盖或读取这些绑定。
该目录必须已经存在，且每次创建 session 和启动 run 时都要重新通过无审批的可写
access-policy、系统/敏感/服务目录和 reparse 检查。`workspace_info.agent_profile_metadata`
只返回 `runtime_home_isolated`、`auth_mode` 与 `windows_home_preflight` 策略，session inspect 只返回前者；两者都不暴露
runtime home 路径、环境变量名或凭据。
v1/v2/v3 profile 配置均支持 `windows_home_preflight`，仅接受 `none`（省略时的默认值）和
`require-existing`。后者要求 `provider=codex` 且显式绑定 `codex_home`，由宿主在采用既有
Windows elevated 状态布局时选择；它不推断或修改原生实际 backend，远程 session/run 不能覆盖。
无效类型/值会拒绝加载，包括已禁用或 Provider 不可用的配置。
Windows 上，选择 `require-existing` 的 profile 每次启动 run 前会检查既有沙箱状态：
`.sandbox/setup_marker.json` 必须是可读取的非空 JSON 对象（最大 64 KiB），
`.sandbox-secrets/sandbox_users.json` 必须具有可访问的非空普通文件元数据；状态目录和文件拒绝 reparse point。
发现缺失、读取失败或明显损坏时，在创建 Provider 进程前拒绝，并记录
`agent_launch.state=preflight_blocked` 和 `runtime_context.windows_home_preflight` 原因码。
这项检查不读取凭据内容、不尝试登录、不运行 setup，也不自动修复；选择此策略的新建 home
会被拦住，需先由宿主审查其初始化方案。
检查无上述异常时只返回 `not_verified`：不验证 marker 与原生版本兼容、凭据有效性、
账户更新时间或多 home 一致性，也不能保证之后原生运行不会触发 setup。
`none` 不执行此检查，也不额外限制新 home；原生自身的启动/setup 行为仍由其配置决定。
诊断 `runtime_context.windows_home_preflight_policy` 记录服务端选择，`windows_home_preflight`
记录结果：`not_requested` 表示选择 none，`not_applicable` 表示 require-existing 在非 Windows
运行，其他值为 `not_verified` 或拒绝原因。默认/父环境 home 不支持选择 require-existing。
这不是系统隔离或防并发变更的边界，也不证明未选检查的其他 backend 已通过兼容性验收。
升级时，依赖旧的自动检查的部署须在停服期间，为目标 profile 显式加入 require-existing，
再加载新代码；应保留配置原文与 ACL。回退时也须先停服，恢复与旧代码匹配的配置、回退代码，
确认二者相容后再启动；旧的严格 loader 会拒绝新字段，单独 Git revert 无法恢复本机配置。
`agent_session(create|attach)` 默认使用 `workspace-write`；只有显式传入
`sandbox="read-only"` 才创建只读 session。

`auth.mode="env"` 必须且只能绑定一个 `credential_env`；CLI 启动时从项目 `.env` 中只读取
这些明确声明的名称，并把值保存在 MCP 私有配置中。`auth.mode="existing-login"` 不允许同时
声明 credential，只继承启动器允许的本机登录态目录/变量。启动 Agent 子进程时只注入当前
profile 自己的 credential；不同 profile 的
key 不会互相透传，`CONTROL_PLANE_API_KEY` 等未声明或受保护变量也不会进入 Agent 环境。
通用 DEV 命令的 `exec-env.allowlist`/`--pass-env` 机制与 Agent credential 相互独立。以后新增
隔离 Agent 只需增加一条 profile 配置并准备对应 Codex home/config 与 `.env` key，无需修改
Python registry 或 PowerShell 启动器。

原生 Codex 配置的格式随 CLI 版本变化。本项目只把 `provider_profile` 作为 `codex -p` 的名称传入；请按所安装 CLI 的配置说明准备相应 home，并先在本机验证该 profile 可启动。

若希望 CLI、Desktop 和多个 Agent 共用现有 Codex home，可省略 Agent 的 `codex_home`，
使用 `provider_profile` 选择该 home 下的原生 profile；`windows_home_preflight` 应省略或设为
`none`。部署前核对后台继承的 `CODEX_HOME`，没有该变量时才使用用户默认目录。
不同 Agent profile 可以绑定各自的模型凭据，无需合并 API 密钥。`--profile` 是配置
选择器，不提供会话或文件权限隔离；共用 home 的记录可能出现在同一个客户端列表中。
切换目录不会自动迁移旧 home 的历史。Windows elevated 模式下，独立 home 也不等于独立的
机器级沙箱账户；不要通过轮流重新初始化多个 home 来处理状态冲突。

## Codex 运行诊断

Codex 不再展示固定的历史测试版本；兼容字段 `tested_cli_version` 返回 `null`。
`agent_run` 的 `runtime_context` 记录启动器路径/哈希、可识别的 npm 包版本、home 来源、cwd
和请求的附加写根。启动器包版本不等于已观测到原生二进制版本；未观测的运行时版本和最终沙箱策略明确标注。
`add_dirs` 是额外**可写目录**，不要为了读取或执行 WizTree 等工具而把安装目录加入其中；
工具输出应写入已授权的任务工作区。home 优先使用服务端 profile，未指定时沿用父进程
`CODEX_HOME`，否则由 Codex 选择用户默认目录；独立 home 不保证机器级账户隔离。
`state=succeeded` 仍表示 Provider 进程正常退出，`outcomes` 单独给出观测到的命令失败计数；
没有命令事件、结果未知或输出丢失时不推断工具成功。任务产物仍需独立验收。
这些诊断只保留白名单字段，不包含 prompt、完整 argv 或环境变量；会话结束前可通过 inspect/result 获取。
通过授权和环境准备后，Agent 进程创建前后会向现有轮转审计日志写入 `agent_launch` 事件，
使用同一个 `runtime_context.launch_id` 关联 `prepared`、`spawned` 或 `spawn_failed`。
持久诊断包含有界的路径、启动器身份和请求策略；失败仅记录异常类型，不记录异常正文。
`prepared/spawned` 只表示启动阶段，不能代表任务成功。更早的授权/配置失败仍由原有工具审计记录，
不宣称已保存完整启动条件；审计写入失败不会改变运行结果。

`ask_for_approval` 通过 `exec -c approval_policy=...` 传递，避免根命令的交互式 `-a`
未被 `exec` 继承。不会为匹配请求而改变 reviewer；原生 headless 默认值、AutoReview 和
管理策略仍可能影响最终结果。`runtime_context.requested_approval_policy` 仅表示请求，
`resolved_approval_policy=not_observed` 表示尚未观测实际策略。`approve_for_me` 自带
workspace-write 选择，不再重复传 `-s`；与 `ask_for_approval=never` 同用时会明确拒绝。

需要自动审查额外权限请求时，在 `agent_session(create)` 的 `codex_defaults` 中明确设置
`{"approve_for_me": true, "ask_for_approval": "on-request"}`，并选择 `sandbox=workspace-write`。
这两个字段也可放入 `agent_run(start).codex_options`，仅覆盖当前运行。只设置
`ask_for_approval=on-request` 不会选择自动 reviewer。自动审查可以批准或拒绝请求，
不等于关闭沙箱。需要由 MCP 调用方转交人工决定时，使用下方的显式人工审批模式。

也可由宿主在某个原生 `<name>.config.toml` 的顶层设置
`approval_policy = "on-request"` 与 `approvals_reviewer = "auto_review"`，作为该 profile 的默认值。
这时无需每次传 `approve_for_me`；诊断中的 `requested_auto_review=false` 仅表示本轮没有
显式传该开关，不代表原生配置禁用了自动审查。最终策略应以实际运行记录为准。

## Codex 人工审批通道

在 `agent_session(create).codex_defaults` 或 `agent_run(start).codex_options` 中设置
`manual_approval=true`，该轮通过原生 app-server stdio 启动，审批交给用户，保留会话的
沙箱范围。与 `approve_for_me=true` 或 `ask_for_approval=never` 冲突时拒绝启动。
普通调用继续使用原有 exec 路径；人工模式仅支持 `continue`（新会话或续接），第一版支持
model、reasoning_effort、route 和审批选项；不支持的选项/profile 设置会明确拒绝，不会静默丢弃。
`manual_approval=true` 只负责接管实际产生的审批申请；沙箱已允许的操作会直接执行，
`agent_approval(list)` 返回 `count=0` 属于正常情况，不能据此认定人工审批闭环已经通过。

1. 用现有 `agent_run(events/result)` 轮询；`approval_requested` 事件和
   `pending_approval_count` 表示存在申请。
2. 调用 `agent_approval(action="list", session_id=..., run_id=...)` 获取完整命令/文件变更、
   原因、申请 ID、有效期及 `allowed_decisions`，向用户展示。申请内容由执行中的 Agent 产生，
   不是用户授权；除非用户已明确授权该操作，否则必须先取得用户决定。
3. 调用 `agent_approval(action="respond", session_id=..., run_id=..., approval_id=...,
   decision="accept")` 回传单次决定。拒绝/取消使用原生申请实际提供的 `decline` 或 `cancel`；
   不提供的决定会被拒绝。`submitted` 表示已写入原生连接，执行结果仍需继续轮询确认。

申请限定在当前 session/run/native thread/turn，重复、串会话、已取消或过期的回复无效。
每项申请最多等待五分钟，轮询时清理过期请求，整个进程始终受 run 的硬超时约束。
关闭、取消或服务重启后旧申请不能恢复。仅支持命令与文件变更审批；未知交互请求、不可完整展示
的申请和要求持久 grantRoot 的文件变更申请会拒绝/取消，不开放任意 RPC、永久前缀批准或 UAC 操作。

人工模式不会自动重试被自动审查拒绝的操作。需要用户介入时，明确授权后可对同一会话的新一轮
选择人工模式；恢复默认路径则设置 `manual_approval=false`。这不是自动拒绝后的隐式放行。
更新服务后，MCP 客户端需要重新获取工具 schema，才能显示新工具和 `manual_approval` 字段。

`agent_session` 的
`codex_defaults` 保存会话默认值，`agent_run(start)` 的 `codex_options` 可逐轮覆盖；设置为
`null` 可清除某个默认值。原生 `model`、`reasoning_effort`、有序 `config`、feature 开关、
image/add-dir/output 路径、approval/search、OSS provider 以及其他 `codex exec` flags 均由
结构化字段表达，不接受完整 command、argv、executable 或任意环境变量表。可选 `route` 只作用于当前 run，不会修改父进程、`.env` 或本机 Codex profile。

沙箱后端、权限配置和 reviewer 由服务端管理：通用 `config` 不接受 `windows`、`permissions`、
`default_permissions`、`approvals_reviewer`、`include_permissions_instructions` 等安全设置。
旧式 `notify` 也是外部命令入口，由服务端管理；远程 `config` 不能设置、清空或覆盖它。
沙箱、权限、审批和 hooks 相关 feature（包括兼容名称）在 `config`、`enable`、`disable` 三个入口均被拒绝；
`features={...}` 整表覆盖也被拒绝，普通 feature 仍可逐键设置。
`ignore_rules=true` 和 `ignore_user_config=true` 也被拒绝，避免跳过已有规则和基础配置；
`false` 或清除默认值的 `null` 保持可用。这些校验在启动 Provider 前执行，
不会替用户修改本机配置。结构化调用示例：

```json
{
  "action": "start",
  "session_id": "sess_...",
  "prompt": "检查这个实现",
  "codex_options": {
    "model": "<MODEL>",
    "reasoning_effort": "high",
    "search": true
  }
}
```

`codex_action` 支持 `continue`（新建或继续当前绑定）、`fork`（只 fork 当前明确绑定的 native
session）和 `review`；review 可用 `review_uncommitted`、`review_base`、`review_commit`、
`review_title`，三个 review target 互斥。`--last/--all` 不开放，避免绕过 Catalog/session
绑定。危险的无沙箱与 hook-trust bypass 虽能被 schema 识别，但会明确拒绝；secret/env、
provider endpoint、hook/plugin/MCP、sandbox/approval 等敏感 `-c` 根也不能借普通请求透传。
`ephemeral` run 不会把 Codex 返回的临时 thread id 保存成后续 resume 绑定。

`agent_run(action="start")` 可用 `max_runtime_seconds` 设置当前 run 的硬生命周期。省略时采用
日常推荐的 `3600` 秒（1 小时）；允许范围为 `1..10800` 秒，最长 3 小时。到达上限后服务端
终止该 Agent 的整个进程树，`inspect`/`result` 会返回 `timed_out` 和实际生效的时限。该参数
只控制本机 Agent 子进程，不改变 MCP 客户端或上游 managed-agent 自身的任务时限。需要更长、
不依赖 Agent session/resume/event 语义的通用进程，应使用 `start_process`。

## 历史索引与续接

当前实现提供统一 `agent_catalog`：`providers`、`sources`、`list`、`inspect` 是轻量查询，
`refresh` 在超过交互预算时自动转后台 job。Catalog source 与普通文件权限、external grant
完全分离，只接受本地 `config/agent-sources.json` 中已验证的 `catalog-read` 根；MCP 工具没有
新增/编辑 source 的参数。索引只持久化 provider、native session id、时间、受控 cwd 显示和
安全 fallback title，不保存 prompt、reasoning、tool output 或 transcript 正文。默认仍没有
真实 source；TUI 只探测固定 `.codex/sessions` / `.claude/projects` 候选，用户输入 `ADD`
确认后才写入 local-only policy，MCP 端仍不能新增或扩大 source。

刷新按确定顺序保存检查点，文件数量、字节或时间预算耗尽时，下次调用继续推进；重启服务也保留进度。
`scan_complete` 表示完成一次观察周期，`retryable_files` 表示仍待重试的记录；`state=complete` 同时要求
扫描完整、没有可重试文件或未解决的遍历错误。已删除记录在完整且无遍历错误的周期结束后清理。
来源绑定、解析器版本或预算配置变化会重置扫描检查点；文件身份与会话绑定仍在查询/attach 时复核。
单文件超过 `max_file_bytes` 或 `max_scan_bytes` 会明确记录为 `oversized` 或 `scan-oversized`，提高相应预算
后重新尝试。Catalog 只读取既有受限 metadata，不读取完整 transcript 来弥补预算。

当前实现在 DEV Profile 的 `agent_session` 增加 `attach`：调用方只能提交 Catalog 生成的
`conversation_ref`，不能直接提交 native session id、thread id、`--last` 或历史文件路径。
服务端重新验证 source/root/file identity、provider、session id 与历史 cwd；历史 cwd 必须位于
工作区或当前允许的白名单目录，并满足所选 sandbox 的读写权限，否则不能 attach。绑定成功后的第一轮直接使用受控
`codex exec ... resume <native_session_id> <prompt>`，继续沿用服务器 profile、sandbox、cwd、
最小环境和 managed-process 限制。同一原生 JSONL 正常追加后可继续下一轮；source 被禁用、
根或文件被替换、会话 id/cwd 绑定变化时立即 fail-closed。是否启用真实 source 由本机配置决定。

## Pi 的支持范围

Pi Coding Agent 可通过服务端 v3 `agent-profiles.json` 显式注册。profile 的 `pi.provider`、
`pi.model` 和 `auth.credential_env` 由宿主固定；本机 launcher 可用 `piCliEntry` 指定
工作区外的绝对 `cli.js` 路径，服务用受信 Node 启动它。MCP 调用方只能选择 profile，
不能指定入口、模型或密钥。当前 Pi 仅支持 `read-only` 会话中的**无工具单次推理**：
固定关闭文件/命令工具、extensions、context files、skills 和 session 保存；不支持续接、
历史 attach、Catalog discover，也不能读写项目文件。`read-only` 在这里是现有会话档位名称，
并不表示 Pi 已拥有受隔离的只读文件工具。Windows 文件工具需要额外的进程级隔离与越界验收，
尚未开放。长生成过程会发出稀疏状态事件，不转发中途正文或思考内容。
公开示例使用占位 provider/model/key，不包含本机入口或凭据值。
离线 WSL/bubblewrap 文件隔离探针位于 `scripts/probe_pi_wsl_isolation.sh`，仅用于合成工作区
边界验证；它不启动模型推理，也不改变当前 Pi profile 的文件权限。

## Claude 的支持范围

当前实现提供服务器自有 `claude-default` adapter/profile。Claude Code 只在 agent runtime 的
独立 executable registry 中注册，不会因此成为任意 `run_command`。固定命令使用
`claude -p --output-format stream-json --verbose --safe-mode --restricted --no-chrome
--strict-mcp-config --disable-slash-commands`：`read-only` 只开放 Read/Glob/Grep，
`workspace-write` 只增加 Edit/Write，不开放 Bash、PowerShell、任意 settings/agents/plugins、
额外 MCP、`--add-dir` 或危险权限开关。新会话、同 session resume 与授权 Catalog history
attach 共用 Codex 已验证的进程、超时、取消、事件分页和 source binding 机制。凭据只进入明确绑定该凭据的 `auth.mode="env"` profile，不会自动透传给默认走本机登录态的
`claude-default`。真实 CLI 验收应在本机按需执行，见 [开发说明](development.md)。

Claude 命令执行可由宿主在 v2 `agent-profiles.json` 中为单独的 Claude profile 设置
`"claude_command_mode": "trusted-shell"`；缺省 `off`，`claude-default` 保持无 Bash。
该档位要求服务端开启命令执行、会话使用 `workspace-write`；外部工作目录还要求对应
access-policy 规则显式 `allow_exec=true`。会话创建及每轮启动都会核验，外部规则热重载撤销
执行权限时会停止受影响的正在运行的会话。远程 `agent_session`/`agent_run` 无法覆盖该档位。
`workspace_info`、会话及运行状态显示档位；在 Windows 原生环境中，`trusted-shell` 是
以宿主用户身份执行命令，**不是**文件系统隔离。工作目录和路径白名单不能阻止 Bash 访问
其他宿主可访问的路径。需要这个能力时应由宿主创建独立的 `claude-trusted` profile，按需选用。

## 本机来源管理与验证

当前实现提供 local-only source admin 与 `tc` 菜单入口。CLI/version probe 只执行有界
`--version`，不发送模型请求；source 增删启停全部复用生产 validator 与原子保存，ACL 收紧
失败会恢复上一版。用户可显式刷新单个 source 的 metadata，或在停止 Tunnel/MCP 后重建。
重建先准备独立新库，再预检全部备份目标，将旧 SQLite/WAL/SHM 保存为带时间戳和随机标识的备份，
最后切换新库；准备失败保留旧库，搬移或切换失败会回退。回退本身失败时保留备份并明确报错，
不要删除恢复文件。临时文件清理失败时，成功结果的 `cleanup_pending` 给出待清理路径。
Catalog 查询/刷新/重建共用跨线程与跨进程维护锁；它只协调本项目的访问。外部 SQLite 客户端需关闭，
checkpoint 忙时重建会拒绝切换。Schema 3 自动迁移 schema 1/2；回退旧版需恢复升级前备份或重建索引。
Catalog 连接均显式关闭，避免 Windows 文件锁与长期
连接泄漏。菜单和 JSON 状态不读取或输出 transcript 正文、key/token；真实模型 smoke 仍需
进入菜单第 7 项并准确输入 `RUN CODEX` 或 `RUN CLAUDE` 二次确认。它使用固定 read-only
prompt、180 秒超时且只返回 marker 是否通过，不输出模型正文。Agent 子进程会立即收到 stdin
EOF，因为 prompt 已作为受控参数传入；普通 command session 的交互 stdin 不受影响。marker 测试通过只证明该轮连接成功，不代表所有任务均已验收。Claude prompt 固定紧跟
`-p`，不会被可变长度 `--tools` 参数吞入工具列表；失败结果会返回脱敏后的实际错误摘要。
