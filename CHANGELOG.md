# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)：

- 补丁版本：只修复向后兼容的 bug，或更新文档/测试。
- 小版本：增加向后兼容的工具、可选参数或运行能力。
- 大版本：改变现有调用契约、默认安全边界，或需要用户迁移的变更。

## [Unreleased]

### Fixed

- 修正服务器 instructions：原来固定声明「所有路径都是工作区相对路径，绝对路径一律拒绝」，在 GRANTS 档下与 14 个只接受绝对路径的 `external_*` 工具直接冲突，会把调用方一路推向错误的工具。现在按实际注册的工具集生成说明：SAFE 档只讲工作区规则；启用外部授权后额外说明 `external_*` 走绝对路径、先用 `access_policy_explain` 判断覆盖、用 `workspace_info` 列出已授权目录，并点明 Git 工具仅限工作区、仓库在工作区外时改用 `external_run_command`。
- 修正两条会误导调用方的路径错误信息。`external_*` 会在授权根上新建一个受限 service，因此工作区 jail 的文案会原样出现在外部调用里，把「授权目录下没有这个文件」说成「Workspace path does not exist」，看起来像白名单失效。两条信息改为不再自称 workspace。

## [0.9.0] - 2026-08-31

### Added

- 新增严格 versioned `agent-sources.json` 后端：只接受 provider-bound `catalog-read` source，拒绝命令、环境变量、宽目录、敏感目录和 reparse root；支持原子保存、上一版 `.bak` 与本地 TUI ACL hardener hook。
- 新增统一只读 `agent_catalog` MCP 工具，提供 `providers/sources/refresh/list/inspect`；MCP 无法添加或扩大 source，也不能读取原始 transcript。
- 新增 metadata-only SQLite 增量索引：Codex/Claude fixture parser、文件 identity/mtime/fingerprint、parser/source binding、逐文件错误隔离、取消/部分刷新、损坏数据库保留恢复和有界 cursor/byte paging。
- 新增 Codex 历史 attach：DEV `agent_session` 只接受 Catalog `conversation_ref`，重新验证 source/file/provider/native id/cwd 后创建受控 session binding，首轮直接走原生 resume。
- 新增 `claude-default` Agent adapter/profile：固定 restricted/safe-mode/no-chrome stream-json 命令模板，支持受控 create/resume/Catalog attach，且 Claude executable 不进入任意 `run_command` allowlist。
- 新增 local-only Agent source admin 与 `tc` 主菜单 E：固定根/CLI version 探测、source 增删启停/验证、单源 metadata refresh，以及停止 Tunnel 后的可恢复 Catalog rebuild；这些能力不注册为 MCP tool。
- 新增 TUI 真实最小 Agent smoke 入口：必须停止 Tunnel 并输入 provider-specific 二次确认，固定 read-only prompt/180 秒超时，只报告 marker 验证结果而不输出模型正文。
- 白名单新增 `browse` 档：只允许列目录，且每次只返回一层。可逐级下钻（列授权根 → 进入子目录 → 再列一层），但读不到文件内容、不能写、不能执行，`browse` 与 `allow_exec` 组合直接拒绝。用于让调用方先看清目录结构，再决定把哪些目录提升进白名单。
- Agent 的工作目录不再写死为工作区，改为由 access policy 决定：白名单覆盖的目录都可以承载 Codex/Claude 会话，沙箱 `read-only`/`workspace-write` 分别要求规则的 `read`/`write` 能力。运行 agent 不算 `exec`，因为命令模板由服务端固定且限定在该目录，与任意 `external_run_command` 分开授权。
- Agent 会话每次 run 前重新校验工作目录授权，`attach` 的续接会话同样如此；规则被撤销、缩小或换根时下一轮立即 fail-closed，不会继续在原目录执行。
- Catalog 记录新增 `cwd_scope`，`attach` 现在接受工作目录位于白名单内的历史会话；原始绝对路径只用于服务端授权，不会出现在 MCP 返回值里。
- 新增热重载模式（`--allow-policy-hot-reload`，默认关闭，属高危档）。开启后注册四个独立工具：`access_policy_change_request` 暂存目录与模式并返回一次性 challenge，本身不授予任何权限；`access_policy_change_confirm` 只接受 `request_id`、`challenge` 与用户确认词，在类型层面就无法选择或扩大路径与权限——能力在暂存时即已冻结；另有 `cancel` 与只读 `status`。确认后原子写入 `access-policy.json`（保留 `.bak`）并立即生效，无需重启。冷重载行为完全不变。
- 热重载拒绝写入服务端自身目录（代码、策略、日志）、盘符根、Windows 系统目录、名称含 credential/secret/key 等敏感组件的路径，以及被显式 `deny` 规则覆盖的路径；批准时会重新校验一次，防止暂存后策略已变。
- CLI 新增 `--agent-sources` 与 `--agent-catalog`，可启动一个不读取本机历史 source 的隔离实例。

### Changed

- `workspace_info` 的 `access_policy` 现在逐条列出白名单规则（路径、模式、`allow_exec`、`require_approval`、启用状态、备注），不再只返回规则数量。此前调用方无法知道自己能用哪些目录，只能先猜路径再用 `access_policy_explain` 逐个验证；而 `explain` 本来就能问出同样的信息，所以隐藏列表不增加任何安全性，只增加使用摩擦。停用的规则同样列出，便于分辨"未授权"与"已停用"。
- 引入 provider-neutral `AgentAdapter`、capabilities 和 profile→adapter 绑定；Codex 的命令构造与 JSONL parser 已迁出通用 runtime。
- Catalog schema v2 将每条索引绑定到 source root 与候选文件 identity；list/inspect 会重新校验 identity、size 与 mtime，目录遍历错误会降级为 partial refresh 而不会误删未扫描记录。
- Agent session/run payload 新增 `provider` 与 `native_session_id`，同时保留 `thread_id` 兼容字段；`agent_session` 增加 `attach`，`agent_run` action 不变。
- 生命周期事件使用 adapter display name，runtime 不再硬编码 Codex 文案；provider/profile 绑定不一致时 fail-closed。
- Attached session 每轮执行前重验授权；允许同一 identity 的原生历史正常追加，但 source disable、root/file replacement、native id/cwd 变化都会拒绝，绝不回退 `--last`。
- Claude `read-only` 仅开放 Read/Glob/Grep，`workspace-write` 仅增加 Edit/Write；不接受 Bash、任意 settings/agents/plugins/MCP/add-dir/model/effort 或危险权限参数。
- Catalog 将 Windows 无符号 64 位文件 identity 无损映射为 SQLite int64，避免部分卷上的 `st_dev/st_ino` 触发写入溢出并拖垮 refresh。
- Catalog 所有 SQLite 连接现在显式关闭，避免 Windows 文件锁和长期运行时的连接泄漏阻断 refresh/rebuild。
- Agent source 原子保存若在 ACL 收紧阶段失败，会恢复上一版（首次创建则撤销），不会留下“命令报错但宽权限新策略已生效”的半完成状态。
- Agent profile 显式声明是否接收已配置的业务环境变量；`codex-default` 保留既有透传，`claude-default` 默认不接收 `EXAMPLE_SERVICE_KEY` 等变量。

### Fixed

- Agent run 现在以关闭的 stdin 启动 Codex/Claude。此前通用 managed-process 的可写 stdin 管道会让 `codex exec` 一直等待 additional input，真实请求直到 180 秒 smoke 超时都没有发出；普通 command session 仍保留 `process_input` 能力。
- Claude prompt 现在紧跟 `-p`，避免被可变长度 `--tools` 参数吞成工具名；smoke 非成功终态也会返回统一脱敏后的真实 runtime error，不再只显示泛化失败。
- managed process 与 Agent 的输出流终于是真正增量的。此前读取线程使用 `stream.read(65536)`，而它会阻塞到读满 64 KiB 或管道关闭；Agent 一次运行的 JSONL 事件通常只有几 KiB，因此运行期间 `events` 始终为空，全部事件要等进程结束或被取消时一次性吐出。改用 `read1()` 后，Codex 首个事件从 40–68 秒降至 0.97 秒，Claude 降至 2.8 秒，事件按产生时间陆续到达。`wait_ms` 长轮询与 cursor 分页此前机制正确但上游无数据可发，同一缺陷也影响 `process_output` 的 `after_bytes` 增量读取。
- `agent_catalog(refresh)` 缺少 `source_id` 时不再返回无从下手的 `source_id is invalid`，改为提示先调用 `action='sources'` 获取 id；工具描述也补充了这一前置条件。
- 修正 `agent_session` 的过时描述：它仍写着 cwd 只能位于 TianCheng 内，而实际已允许 access policy 覆盖的目录。
- `test_stdio` 不再断言开发机本地的 agent source 数量。该用例拉起真实服务器，此前把“本机没有配置 source”写进断言，用户一旦添加真实 source 就会失败；现在改用 `--agent-sources`/`--agent-catalog` 启动隔离实例，断言的是出厂默认而非某台机器的配置。
- 轮询 `agent_run` 的 `events/inspect/result/cancel` 与 managed process 的 `process_status/process_output/list_processes` 不再各自占用一条 job record。这些是 start 立即返回后唯一的观察入口，此前每次轮询都会入队，一次正常的长任务观察即可撑满 256 条记录上限。
- job record 表满时改为按完成时间回收最老的**已完成**记录，未完成记录永不驱逐。此前只回收「完成超过 retention 秒」的记录，表一旦撑满，所有走 job 的工具（写入、删除、Git、`run_command`）会持续拒绝服务直到 retention 窗口结束，而 `stat`/`read_text` 等直连工具仍可用，故障表现为「服务半死」。

### Verified

- 自动化测试覆盖 WorkspaceJail、reparse point 拒绝、access policy 求值、external grants、job 生命周期、Agent adapter/registry、metadata Catalog、Agent source 策略与 launcher 行为；Agent 相关用例使用不依赖真实 CLI 的 fake adapter。
- 合成 stdio 验收经真实 JSON-RPC 走完整启动路径，覆盖工具注册面、catalog list、session attach 与 run start/inspect/result，不读取任何真实会话历史，也不调用真实模型。
- 白名单、`browse` 档与策略热重载另有一份 stdio 验收，使用一次性策略文件，覆盖：批准前拒绝；暂存请求不授予任何权限；错误 confirmation 被拒；服务端自身目录、config 目录与盘符根被拒；`browse` 每次只返回一层且深度请求被夹回一层、拒绝文件内容、不能承载 Agent；权限提升后生效、收窄后立即失效，全程无需重启。
- 每一条"文件已创建"结论都由 MCP `read_text` 独立读回内容验证，不采信 Agent 自报；`read-only` 会话的写入不产生文件；运行结束后无 Agent 子进程残留。
- job record 回收路径经实测复现并修复：观察类工具此前会占满记录表并使所有走 job 的工具持续拒绝服务。
- managed process 与 Agent 的输出流为真正增量：修复前读取线程会阻塞到读满缓冲区或管道关闭，导致事件在进程结束时才一次性到达。
- 在授权的本地环境完成了额外的端到端验证，包括真实 Agent CLI 的启动、续接、并行、事件流隔离与取消。这些运行依赖本机安装与私有凭据，**不随本仓库提供，也无法在公开仓库中复现**。
- **平台限制（非本项目缺陷）**：经 ChatGPT 连接器验收时，`access_policy_change_confirm` 始终无法到达服务端，被 ChatGPT 侧的安全检查拦截；同一流程中的 `request`、`status`、`cancel` 均可正常到达。把单一多 action 工具拆分为四个独立工具、并把 confirm 的 schema 收窄到只接受 `request_id`/`challenge`/`confirmation` 之后，该行为没有变化，说明拦截针对的是"提交权限变更"这一动作本身，而非 schema 形态。审计日志确认拦截发生在服务端之外：被拦后 staged request 保持 pending，未产生半授权状态，`fail-closed` 行为正确。因此策略热重载的端到端能力目前只在本地 stdio 客户端验证通过，ChatGPT 侧不可用。未采取任何针对该安全检查的规避措施。
- 两条真实环境约束：`external_*` 文件工具只在开启 external grants 时注册，因此热重载需配合 GRANTS Profile 才完整可用；Codex 拒绝在非 git 仓库目录中运行，服务端不传 `--skip-git-repo-check`。
- 出厂默认 Agent source 数为 0：不配置任何 source 就不会读取本机历史，必须由用户显式授权后才会扫描。

## [0.8.1] - 2026-08-29

### Added

- `agent_run` 增加轻量 `inspect`、独立 `result`，以及最长 10 秒的 `events(wait_ms)` 等待窗口。
- Agent event 增加 UTC 时间、稳定 cursor gap 和 2,000 条硬 retention；session/run 也增加内存数量上限。

### Fixed

- Codex 后续 run 只使用当前 session 绑定的 `thread_id` resume，并验证实际参数顺序。
- 非零退出、超时、取消和 MCP shutdown 现在返回互不混淆的终态；shutdown 不再把已经完成但尚未查询的 run 误标成中止。
- stderr、失败事件、取消原因和结果统一执行有界截断与 secret 脱敏，末尾无换行的 JSONL 也会在终态收取。

### Verified

- fake-Codex 覆盖新会话、resume、结果截断、失败脱敏、等待、取消、超时和 shutdown。
- stdio smoke 实际调用 `agent_session create/inspect/close`；未发送真实 Codex 模型请求。
- 全量测试 `100 passed, 1 skipped`；唯一 skip 为当前 Windows 账户没有 symlink 创建权限。

## [0.8.0] - 2026-08-29

### Added

- 增加服务器自有的 `codex-default` profile registry，固定 Codex 参数模板并限制 sandbox、prompt 与 thread_id。
- 增加有界、脱敏的 Codex JSONL 事件归一化解析器，为后续 `agent_session`/`agent_run` 提供基础。
- 增加受控 `agent_session`/`agent_run` MVP，支持 Codex 新会话、自动 thread resume、分页事件和取消。

## [0.7.0] - 2026-08-29

### Added

- 命令 Session 前置层：managed process 现在返回稳定的 `session_id`，并支持 UTF-8 `process_input`。
- `process_output` 增加 `after_bytes` 增量游标，返回每个流的 offset、cursor gap 和截断信息。

### Fixed

- 使用默认机器级 access policy 启动临时/替代 workspace 时，若策略根不匹配则安全回退为仅工作区规则，避免把其他 workspace 的外部规则带入测试或开发进程；显式策略路径仍 fail-closed。

### Verified

- managed process stdin、session id、增量 output cursor 和 stdio tool registration 测试通过。

## [0.6.2] - 2026-08-28

### Fixed

- TUI 明确区分 GRANTS、DEV 与 GRANTS+Exec 的命令能力，并显示开启方式。

### Fixed

- GRANTS+Exec Profile 不再误显示为普通 DEV，避免误判外部授权状态。

## [0.6.1] - 2026-08-28

## [0.6.0] - 2026-08-28

### Added

- 静态白名单规则开始接入 external grant：非审批规则可直接签发 scope-limited grant，
  显式 `deny` 规则不可被临时 grant 覆盖。
- 补充静态策略与 external grant 集成测试。
- TUI 增加白名单规则查看、新增和删除入口。
- TUI 白名单入口增加规则编辑和启用/禁用。
- `external_*` 文件工具支持省略 `grant_id`，直接对 `require_approval=false` 白名单中的绝对路径操作。
- 新增只读 `access_policy_explain`，可在操作前查看命中的规则、权限和审批要求。
- 新增 `access_policy_reload`，全量校验后原子替换内存策略，失败时保留旧策略。
- 增加服务内策略 reload API，可在保存后刷新而无需重建 Tunnel。
- TUI 增加策略路径解释与完整策略验证入口；策略保存保留 `.bak` 快照、原子替换并尝试收紧 Windows ACL，
  验证失败自动恢复上一份有效策略。
- 将后台 `ExceptionGroup/TaskGroup` 失败转换为受控 MCP `ToolError`，避免单次异常终止 stdio dispatcher。
- 白名单跨规则 move/copy 拒绝信息现在包含来源/目标规则能力摘要，仍保持 fail-closed。
- 增加只读外部白名单的 stdio smoke，覆盖 list/read/search 与写入拒绝。
- Git remote/clone/fetch/pull/push 的 stdout/stderr 在返回前脱敏常见嵌入凭据和 token 形态。

### Verified

- `88 passed, 1 skipped`；跳过项仅因当前 Windows 账户没有创建 symlink 的权限。
- 真实 stdio 文件/trash smoke、后台 job/exec smoke 和 `uv lock --check` 均通过。

## [0.5.1] - 2026-08-27

### Added

- 落地静态 `AccessPolicy` 策略引擎 Phase A：规则模型、最长路径匹配、deny 优先、
  fail-closed 配置校验和 reparse-point 防护。
- `workspace_info` 增加当前策略规则摘要；默认仍只允许配置的工作区。
- 增加策略引擎单元测试，覆盖权限级别、冲突、缺失目录和越界场景。

### Note

- Phase A 只建立安全策略基础，不会自动开放外部目录；具体工具接入和 TUI 管理属于后续 Phase B/C。

## [0.5.0] - 2026-08-27

### Added

- 为副作用 job 增加可选 `idempotency_key`，覆盖文件、trash、Git remote、clone/fetch/pull/push、commit 和 `run_command`。
- 增加有界的 JobManager shutdown，服务器退出时取消运行中的 job、过期 queued job 并等待 worker 收尾。
- 增加 `scripts/smoke_jobs.py`，验证 stdio 后台转换、幂等重试、状态查询和取消。

### Changed

- README 和本地 async-job checklist 同步记录 75 秒交互预算、job 语义和 `start_process` 的 `process_id` 生命周期模型。

### Verified

- `72 passed, 1 skipped`；跳过项仅因为当前 Windows 账户没有创建 symlink 的权限。
- 基础 stdio smoke 和 exec/job smoke 均通过。

## [0.4.0]

- 历史版本；详见 Git 历史。
