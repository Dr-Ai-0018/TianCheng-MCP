# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)：

- `0.x` 开发阶段：补丁版本只修复向后兼容的 bug，或更新文档/测试；次版本可以增加能力，也可以包含明确记录的工具或 API 调用契约变更。
- 每项不兼容变更都须在本文件中说明影响和迁移方法；版本升级不能削弱现有安全边界。
- 从 `1.0.0` 起：不兼容的公共 API 变更或默认安全边界变更升主版本，向后兼容的新增能力升次版本。

## [Unreleased]

## [0.16.0] - 2026-10-09

### Added

- 主菜单 Doctor 和完整状态接入分页结果页；Doctor 保留诊断、真实退出码及 Supervisor 检查顺序，完整状态的 Git/gh 查询使用限时探测。

- TUI 帮助和缓存详情支持滚动、翻页、首尾跳转和长路径换行；终端异常或缩小时可降级。返回独立恢复光标与颜色，清理当前显示区域时保留终端历史。

- TUI 只读子进程探测共用 8 秒预算并限制输出；失败显示状态未知或带时间的旧快照，配置读取失败仍可退出和重试。正式服务启停保持原路径。

- TUI 扩展到 Profile、Key、命令规则、路径权限和 Agent 来源；七个菜单与文本降级共用选项及帮助定义。长状态摘要保留 V 详情入口，H/V 返回不重复探测，原输入和确认保持。

- 取消命令策略恢复或输入无效选项时，不再误报“策略已保存”。

- Windows 主菜单和出站代理页采用分组 TUI，支持方向键、上下文灰色提示、滚动、选中位置恢复与 R 刷新；不支持交互控制台时回退文本菜单。菜单移动不重复读取业务状态，CLI 输出和业务确认保留。

- Windows 首次使用整合为单页完整教程，包含快捷命令安装、Tunnel/Key 截图、启动验收及全部主菜单用途。主菜单和六个子菜单新增底部提示与 H 只读选项帮助。
  主菜单与路径策略菜单在输出被重定向时跳过清屏，避免 CursorPosition 异常。

- README 改为产品入口，新增 manual/ 使用手册与 Windows 首次连接指南；按配置、权限、运行维护、Agent 和开发任务组织内容，清理历史阶段描述与代理优先级矛盾。

- 命令执行拒绝区分执行开关、本机禁用、白名单、程序不可用和参数模板不匹配，提供稳定原因代码与处理提示。
  控制台与运行实例明确区分 next_start / runtime_snapshot，以 policy_revision 比较有效规则配置；
  管理状态 execution_enabled=null 表示无法获知运行开关，运行实例仍返回实际布尔值。

- 命令预设增加 elevated（显式平台 Shell）和 unrestricted（PATH 程序名/绝对程序路径）。
  默认仍 balanced；SAFE/DEV、环境透传、路径授权及进程限制保持独立，不提权或提供 OS sandbox。
  unrestricted 的直接调用禁用按别名/文件名生效，配置规则仍优先；状态明确命令列表不完整。

- 普通命令执行支持版本化 command-policy 默认预设及本机覆盖：minimal 仅开放 Git/rg 的精确版本查询，
  balanced 保持现有 DEV 工具及参数范围；本机添加与禁用合并，禁用优先，新增自定义可信 argv 前缀。
  SAFE/DEV 总开关、固定模板 Agent、专用 Git、外部路径授权和环境透传独立；这不是 OS sandbox。
- TUI 新增 C / commands 管理入口，支持预设切换、添加、禁用、恢复、移除和恢复默认；
  配置先验证再原子保存，修改后重启生效，不提供聊天写配置或热重载。
  CLI --command-policy 与 launcher.commandPolicyPath 支持所选本机配置路径；workspace_info 展示实际策略。

### Fixed

- 出站代理菜单保存的本机配置优先于继承的进程环境，修复修改/清除代理后仍使用旧地址的问题。
  本机空值禁用对应代理；删除该字段可恢复继承环境。原来依赖环境覆盖本机配置的启动方式须先删除对应本机字段。
  设置、清除、待保存及无改动返回均有反馈；保存后下次检测/启动读取新配置，已运行的 MCP/Tunnel 需重启，无需重开 PowerShell。
  菜单选 `8` 会先保存当前修改再检测连通性与出口 IP，无需退出后重新进入。

## [0.15.0] - 2026-10-01

### Fixed

- rg 和 Python 回退共享隐藏目录、内部目录、局部忽略规则及候选扫描预算；
  没有 rg 时过滤参数和超时仍生效，Python 保留 UTF-16 内容解码。
  忽略规则仅从已授权 workspace 内读取，不读取外部父目录或用户全局规则。
- 幂等请求先查已有任务再回收容量；入队及失败撤销在同一临界区，
  避免容量重试重复副作用和队列失败时返回未入队任务。
- 后台 handoff、cancel 与普通审计统一隔离日志故障；workspace_info 的 audit_health
  返回当前实例的失败计数和异常类型，业务结果与任务句柄保持可用。
- Tunnel 重启预算按连续 ready 时长重置，ready 中断重新计时；
  长期存活但未 ready 不再误标 healthy 或清除预算，恢复信号不被健康重置抵消。
- 策略热更新验收用继承权限的唯一目录准备合成 Git 工作区，修复重复 mkdir；
  准备失败回收本次目录，支持 --prepare-only、--workspace 和 --test-root，
  常规测试验证准备阶段而不调用真实模型。

- Catalog 持久化扫描检查点，文件/字节预算耗尽后继续推进；deferred、active-writing 和临时读取失败会重试，
  不再把未处理文件当 unchanged。完整扫描且没有可重试项时才报告 complete；完整观察周期后才清理删除记录。
- Catalog 查询、刷新和本地重建共享线程/进程维护锁。重建先准备独立新库，再预检并备份 SQLite/WAL/SHM，
  名称加入随机标识避免同秒碰撞；搬移或切换失败回退，临时文件清理失败返回 cleanup_pending。
  活跃 SQLite 读写导致 checkpoint 忙时拒绝切换，保留旧库；损坏库恢复也按数据库组备份。

- 受管进程与 Agent 在准备/启动之前原子预留共享槽位，失败和退出释放；shutdown 阻止后续启动。
  退出后关闭管道、进程组对象和系统句柄，保留可回读结果；清理异常记录在状态中，并继续清理其余资源。
  普通进程历史限制为 1 小时、128 条、64 MiB 总输出，先触及的限制生效；Agent 保留事件和结果而释放原始输出。
- Codex/Claude/Pi/人工审批协议统一增量 UTF-8 与 JSONL 分帧，中文和 emoji 跨块不损坏，
  退出时处理没有换行的最后一帧；无效编码或超过 256 KiB 的单行明确失败，不无限积累半帧。
- Windows TUI、三种 MCP 启动器和 Python CLI 共用配置模型，所选配置显式传递到 stdio；
  自定义 workspace/解释器/策略/来源/Catalog/profiles/env/audit/超时不再被默认值替换。
  新窗口启动保留配置；旧自定义 profile 须通过原有 set-mode/edit-profile 更新后启动。
  路径按项目根规范化但不提前解引用 workspace 链接，保留 jail 检查；无效配置明确失败。
  白名单环境变量校验先于 dotenv 值读取，控制面 key 不透传，JSON 不隐式启用执行或授权。
- 文件修改共享按规范化路径协调的临界区，覆盖直接、scoped 和重叠 workspace 入口；
  SHA-256 校验与提交一体执行，copy/move/delete/restore/purge 的子树冲突也会协调。
  追加改为原子替换，避免失败留下半截追加；等待冲突期间支持取消。
- 回收站先写恢复记录再移动数据，元数据失败保留原文件；恢复后的清理失败通过
  `metadata_cleanup_pending` 返回实际状态。禁止用普通 delete 再回收回收站内部条目。
- Git 配置安全检查改用 Git 自身的无 include 解析，覆盖同行设置、大小写、引号和重复键；
  所有文件型 Git 工具统一关闭 fsmonitor、hooks、自动维护、自动签名和 ext 协议。
  SAFE 保留用户 Git 身份，但禁用全局外部过滤器；DEV 保留服务端受信任过滤器与认证配置。
- 外部授权操作保留当前静态策略，搜索、列目录与 glob 不泄露 deny 子目录；copy/move/delete
  在修改前检查整个源树及复制/移动的目标树。临时 grant 不能绕过新的 deny 规则。
- grant 批准时重新检查目录、当前策略及三项活动授权上限；待批准请求最多16项，取消或过期释放容量。
  已被策略禁止的 grant 根会失效，并取消关联后台任务、隐藏旧结果。
- 常规测试默认使用临时访问策略和 Agent 环境文件，避免读取开发机器的私有配置。

### Notes

- Catalog schema 升至 3，自动迁移 schema 1/2 并保留既有记录。回退旧代码须恢复升级前备份或重新建库，
  旧代码不支持直接读取 schema 3。单文件超过总扫描预算时记录 scan-oversized；提高预算后会重新尝试。

## [0.14.0] - 2026-09-29

### Added

- 原生 Linux stdio 启动器 `run-mcp.sh`，以及基于官方受管 runtime 的 Linux Tunnel
  connect/status/stop 脚本与占位示例配置；默认 SAFE，不自动启用执行能力或开机自启。
- GitHub CI 新增 Ubuntu / Python 3.13 测试与 wheel 构建；保留 Windows 3.12/3.13 回归。
- Pi Coding Agent 可通过服务端 v3 profile 显式注册。当前仅支持 read-only 会话中的
  无工具单次推理与流式进度；不提供文件或命令工具，不代表 Pi 文件沙箱已验收。
- Linux 真实 Codex Agent 只读验收入口：在独立合成 Git 工作区经 MCP/app-server
  串行验证实际拒写与可写对照，要求命令退出码、随机标记和文件字节一致；
  意外审批申请一律取消，缺少事件或仅有模型文字报告时不会标为通过。

### Fixed

- 授权过期取消测试先确认任务进入后台运行，再推进独立授权时钟，消除授权 TTL
  与交互等待同为一秒时的 CI 竞态；仍验证真实回收线程取消任务并拒绝过期访问。
- 工作区外路径策略、临时授权和 Agent 来源校验按宿主平台处理绝对路径；Linux
  拒绝外来 Windows 路径、父目录跳转和链接越界，保留 Windows 的原有检查。
- POSIX managed process 与 job 取消回收整个受管进程组；已用真实 Linux stdio
  验证子进程树收尾和 job 取消，Windows 保留现有进程管理路径。
- Linux 默认使用 XDG 配置与状态目录，并检查服务私有目录的所有者与 0700 权限；
  显式配置路径仍可使用，工作区自身权限由宿主系统管理。

### Notes

- Linux 提供命令行与脚本化运维入口；Windows PowerShell 交互式 TUI 尚未移植。
- Codex app-server 的真实只读拒写、写入/续跑/取消、人工审批与关闭已在 Linux 验证；
  执行工具按宿主账户运行，文件工作区策略不等同于操作系统隔离。

## [0.13.0] - 2026-09-27

### Added

- Claude Code profile 新增宿主配置的 `claude_command_mode=off|trusted-shell`；默认 `off`。
  显式可信档位可在 `workspace-write` 会话中运行 Bash，外部工作目录还需 `allow_exec`。
  权限在创建及每轮启动时检查，热重载撤销外部执行规则会停止正在运行的会话。
  Windows 原生 Bash 按宿主用户权限执行，不提供项目路径隔离。

## [0.12.0] - 2026-09-26

### Added

- Codex Agent 可显式设置 `codex_options.manual_approval=true`，通过 `agent_approval(list/respond)`
  处理原生运行时实际提出的审批申请。调用方须把申请交给用户决定；安全策略已允许的操作不会
  强制产生申请。切换人工模式不代表自动拒绝后转交人工，也不代替 Windows UAC。

### Fixed

- 静态白名单文件工具创建的临时服务不再加载无关的本机 Agent Catalog 配置，避免该配置不可读时误使外部文件操作失败。

### Security / Changed

- 通用 Codex `config` 现在也拒绝旧式 `notify` 外部命令配置，包括清空或子键覆盖。
  该入口与 hooks 一样保留给服务端，不能由远程请求设置可执行命令；需要通知的部署应使用
  宿主受控配置。这项兼容性收紧随次版本发布，不更改既有宿主通知配置。
- 服务端 v1/v2 profile 新增 `windows_home_preflight=none|require-existing`，省略默认 none。
  原来对所有 Windows 显式 home 的检查收窄为宿主明确选择 require-existing；该值只适用于
  Codex 显式 home，远程请求不可覆盖，不推断/改变原生 backend。依赖原自动检查的部署需
  停服后为目标 profile 加入 require-existing，再启动新代码。回退也须停服并恢复匹配的
  本机配置，旧 loader 不接受新字段；单独回退 Git 不足以撤销迁移。此契约变更随次版本发布。
  workspace_info 的 profile 元数据及持久启动诊断分别显示策略和执行结果。
- Windows 选择 require-existing 的 Agent 在启动前拒绝缺失、不可读取或明显损坏的既有沙箱状态，
  持久记录 `preflight_blocked` 和固定原因码；不启动 Provider、读取凭据或自动 setup。
  此策略下新 home 需由宿主先审查初始化方案；none 不增加此限制，非 Windows 不执行检查。
  无明显异常仍标为 `not_verified`，不代表凭据有效或多 home 安全；默认 home 行为保持。
- 收紧远程 Codex 调用的配置契约：拒绝通用 `config` 覆盖 `windows`、`permissions`、
  `default_permissions`、`approvals_reviewer`、`include_permissions_instructions`；拒绝沙箱、
  权限、审批及 hooks 相关 feature 的配置/启停，以及 `features={...}` 整表赋值。
- `ignore_rules=true`、`ignore_user_config=true` 现在被拒绝，避免调用方跳过宿主规则和基础配置。
  这是有意的兼容性收紧，须随次版本发布；依赖这些选项的调用应移除对应远程覆盖，并由宿主
  在受控配置中确定安全策略。普通 feature 使用 `features.<name>=...` 逐键配置。
  结构化 `ask_for_approval`/`approve_for_me` 接口和普通模型、推理选项仍按现有规则工作。

## [0.11.0] - 2026-09-24

### Added

- 出站代理 URL 现支持 `socks5://` 和 `socks5h://`；项目安装包含 SOCKS 所需的
  `socksio`，HTTP 与 HTTPS 目标可分别配置 HTTP 或 SOCKS5 代理。
- 可选出站代理配置：启动进程的 HTTP_PROXY/HTTPS_PROXY/NO_PROXY（含小写）优先于
  launcher 配置；loopback 自动直连。Agent 默认不继承代理；`proxy.agent` 支持
  `off`、`selective`、`always`，且不覆盖子进程已有变量。旧值 `inherit` 继续按
  `always` 解释。
- `selective` 模式下，`agent_session(create|attach)` 可用 `use_proxy=true` 选择本次
  session 的代理注入；其他模式不暴露此参数，服务端固定应用全局策略。session 返回
  `proxy_enabled`，`workspace_info` 返回代理模式与是否配置的非敏感状态。
- `tc` 出站代理菜单显示有效代理的协议、主机、端口、认证标记和来源；手动检测时经
  对应代理访问 `api.ipify.org` 并显示出口 IP 或错误类别，不在打开菜单时自动请求。

### Fixed

- 去除代理菜单重复状态行；将出口 IP 检测直接使用的 `httpx2` 列为显式依赖。

## [0.10.1] - 2026-09-23

### Changed

- `tc` 启动与新窗口启动前检查当前 Profile 和健康监听端口；端口已占用时提示排查进程，
  不再先运行 Doctor 后抛出泛化启动失败。菜单在进程识别不到但端口已占用时显示冲突，
  不自动结束进程或清理 ChatGPT 连接侧的工具快照。
- 修正 `agent_run` 工具描述：`start` 只承诺返回 `run_id`，不再错误提示会暴露内部
  `process_id`。
### Verified

- 私有版全量测试 `224 passed, 2 skipped`；公开版 56 文件隔离快照全量测试
  `213 passed, 3 skipped`，导出内容逐字节一致，内部名称、机器路径和常见密钥形态扫描均无命中。
  跳过项是当前 Windows 账号无符号链接创建权限，以及公开预演副本无 `tunnel-client`。

## [0.10.0] - 2026-09-23

### Added

- 新增确定性的公开版同步器：只导出 Git 跟踪文件，执行私有版与公开版的命名映射，
  拒绝内部名称、已知机器路径和 local-only Agent profile 残留，并要求源与目标仓库状态可核验。
- 新增可移植的 launcher/Agent profile 示例；真实工作区、工具路径、Agent runtime home、
  credential 变量名、访问策略、Catalog、状态和日志全部进入 Git 忽略的本机配置层。
- 内置 Codex profile 统一为不绑定私人 provider 或 credential 的 `codex-default`；额外 Codex
  profile 继续通过 local-only `config/agent-profiles.json` 叠加。
- 新增 Windows Tunnel Supervisor：按 profile 单实例托管 `tunnel-client + stdio MCP` 完整
  进程树，支持内部 502/连续 upstream failure/进程退出检测、指数退避、稳定窗口重置、
  10 分钟重启预算、熔断和原子状态快照；`tc start/start-new/stop/restart` 已接入该生命周期。
- 启动器配置新增 `supervisor` 段，默认显式使用 `mcpConnectionMaxTtl=24h`；设置界面可开关
  Supervisor 和修改 transport TTL，并明确它不是 Agent 或后台进程运行上限。
- Agent profile 配置升级为 v2 overlay：`inherit_defaults=true` 保留内置 provider profile，
  支持同名覆盖、追加与 `enabled=false` 显式禁用；认证只能选择单一 `credential_env` 或
  `existing-login`。配置缺失回退内置默认，存在但无效仍 fail-closed，避免自定义 Codex profile
  意外移除 `claude-default`。
- `agent_run(start)` 新增逐 run `max_runtime_seconds`；省略时采用推荐的 1 小时默认值，调用方可
  按任务调整，服务端硬限制为 `1..10800` 秒（最长 3 小时），run payload 回显实际生效值。
- 新增配置驱动的 MCP Agent Profile registry 与 `isolated-agent` profile；由服务端冻结独立
  `<ABSOLUTE_CODEX_HOME>` runtime home；Codex spawn 前通过专用通道设置
  `CODEX_HOME`，MCP 调用方不能传入或覆盖任意环境变量/runtime home。
- Agent profile 可绑定单个 `credential_env`。CLI 只从 `.env` 读取 profile 声明的名称，运行时
  只向当前 profile 注入自己的 credential；Agent key 不再依赖全局 `exec-env.allowlist`，新增
  profile 无需修改 Python registry 或 PowerShell 启动器。
- `workspace_info.agent_profile_metadata` 与 session inspect 新增非敏感的
  `runtime_home_isolated` 标记；Codex CLI 的 `-p` 语义在代码中明确为
  `codex_config_profile`，与 MCP profile 名称分离。
- Codex Agent session/run 新增结构化 `codex_defaults` 与逐 run `codex_options`，覆盖已测试
  `codex-cli 0.153.0` 的 `exec` 运行参数面；model、reasoning effort、route、重复 config/
  feature/path flags 均保持原生 argv 语义，不再需要为每个模型和路由复制 profile。
- `agent_run(start)` 新增 `codex_action=continue|fork|review`；fork 只使用当前服务端绑定的
  native session id，review target 参数在 spawn 前执行互斥校验。

### Changed

- `external_search_text` 新增 `respect_gitignore` 与 `include_internal` 参数，默认值分别为
  `false`、`true`，保持既有外部扫描范围；使用 ripgrep 时可缩小搜索范围，Python 回退不保证
  同样的筛选行为。`mkdir` 与 `external_mkdir` 统一标为非幂等写入，反映 `exist_ok=false` 的调用。
- `access_policy_reload` 说明现明确其冷重载用途：读取本地修改的策略文件并更新内存快照，
  不编辑文件，也不代替需要审批的授权流程。
- 两套授权流程统一使用 request/approve/cancel 动作后缀：临时授权现在使用
  `external_access_request/approve/cancel`，持久策略使用
  `access_policy_change_request/approve/cancel`。临时 grant 只在当前 MCP 进程内有效、最多
  10 分钟；策略批准会持久写入 `access-policy.json` 并立即生效。
- Agent session/run 的本地返回与归一化事件统一使用 `native_session_id`；Codex 原生 JSONL
  输入中的 `thread_id` 仍按上游格式解析。
- Agent run 只能通过 `agent_run` 的 inspect/events/result/cancel 查看和取消；通用
  `process_status`、`process_output`、`process_input`、`stop_process` 拒绝 Agent 进程，
  `list_processes` 不再列出它们。调用方应改用 Agent 的 `session_id` 与 `run_id`。
- 带 `grant_id` 的 `external_*` 工具现在接受授权根内的绝对路径，原有的授权根相对路径仍可用；
  不带 `grant_id` 时仍要求静态策略覆盖的绝对路径，越界与重解析点仍被拒绝。
- `tc status` 不再把 `/readyz` 当作 MCP 已通过端到端探测；现在分别报告 Supervisor、Tunnel
  readiness、`MCP Inferred` 与 `MCP Verified`。当前 stdio 无同 transport probe 时 verified
  明确显示 `not-available`，旧的裸 Tunnel 则显示 `unverified`。
- `agent_session(create|attach)` 的默认 sandbox 从 `read-only` 改为 `workspace-write`；需要只读
  行为时必须显式传入 `sandbox="read-only"`。调用方显式选择的 sandbox 仍保持不变。
- provider capabilities 现在返回 `tested_cli_version`，Codex 同时声明 fork/review 能力；
  session/run inspect 仅返回有界、非敏感的 effective options 摘要。
- Codex 的 root-level `--search` 与 `-a` 按 0.153.0 实际语法放在 `exec` 前；旧调用不传新
  options 时仍生成原有命令。

### Removed

- 移除无效的 `tc totp-setup`、未使用的 `totp_secret` 构造参数及
  `external_grant_status.totp_configured` 恒假字段。审批仍只依赖会话内的一次性 challenge
  与用户确认词，challenge 不构成独立的第二因子。
- 移除 `AgentProfile.agent/codex_profile`、`AgentRegistry.get_adapter`、
  `CodexJsonlParser.feed` 和 Tunnel 失败布尔包装等旧兼容入口；Python 调用方分别使用
  `provider/codex_config_profile`、`adapter_for_profile`、`feed_line` 和失败原因分类函数。
- 工具名 `request_external_access`、`approve_external_access`、
  `cancel_external_access_request`、`access_policy_change_confirm` 已移除；调用方分别改用
  `external_access_request`、`external_access_approve`、`external_access_cancel`、
  `access_policy_change_approve`。`agent_session` / `agent_run` 返回中的 `thread_id` 别名已移除，
  调用方改读 `native_session_id`。
- Agent run 的 start/inspect/result/cancel 返回不再暴露内部 `process_id`；调用方使用
  `agent_run` 的 `session_id` 与 `run_id` 管理生命周期。

### Security

- transport 恢复只重建连接和受控进程树，不自动重放任何结果未知的请求；写入、命令、Agent、
  Git 等副作用仍由现有 job/idempotency 契约负责。Supervisor 只接受本机 loopback health URL、
  结构化 argv 和受限 profile 名称，状态文件不保存 key、env、工具参数或正文。
- 独立 Codex home 必须预先存在，并在 session 创建和每次 run 前重新验证：要求绝对路径、
  无 reparse、命中无需审批的可写 access-policy，且拒绝系统目录、服务代码/策略/日志目录、
  filesystem root 与敏感名称路径。返回值不暴露 runtime home、凭据内容或完整子进程环境。
- Profile 配置采用严格字段白名单；credential 变量名受既有保护名单约束，`.env` 中未被 profile
  声明的变量不会加载，多个 profile 的 credential 不会进入同一个 Agent 子进程。
- Codex 路径参数逐项经过 workspace/access-policy 与 reparse 校验；route 只作为受控的
  单进程 `AWZ_ROUTE` overlay 注入且不回显值。任意 argv/env map、危险 sandbox/hook bypass、
  secret/env/provider endpoint/hook/plugin/MCP 等敏感 `-c` 根均明确拒绝。
- `--last/--all` 不能绕过明确 session 绑定；ephemeral run 不持久化临时 native thread id。

### Verified

- 定向回归 `tests/test_tunnel_supervisor.py + tests/test_launcher.py` 为 `31 passed`。
- `supervisor.enabled=false` 配合故意无效的 Python 路径仍成功进入旧的裸 Tunnel 路径；实际
  command line 只有 `run --profile tiancheng-local`，无 Supervisor、无 TTL override。
- `tc stop` 后 Supervisor、tunnel-client、stdio child 均为 0，`.lock/.stop` 不存在；持续观察
  60 秒没有复活。只强杀 Supervisor PID 后，Job Object 也将 Tunnel/stdio 孤儿数保持为 0。
- 实际终止 tunnel-client 后自动进入 generation 2；state/JSONL 已核对并由单测固定
  `generation/recovery_reason/restart_count/backoff_until` 四个诊断字段。
- `isolated-agent` 已迁移到 Codex CLI 当前的独立 `isolated-agent.config.toml` profile 文件格式。真实请求
  已通过自定义 endpoint 认证并精确返回预期 marker；stdio 验收通过 profile 注册、隔离标记、
  session、native resume、并发拒绝、cancel/close、read-only 拒写和清理链路，结果 `21/23`。
  未通过的两项均为 workspace-write 文件落盘验证：当前 Codex 内嵌 Codex 的测试宿主把直接调用
  和 MCP 子进程都约束为 read-only，因此仍需从正常 Tunnel 宿主完成最终写入确认。

## [0.9.1] - 2026-09-01

### Fixed

- 修复 Agent 事件脱敏对裸 `sk-...` 令牌保留原文的问题，并覆盖 OpenAI、GitHub、GitLab、Slack 和通用键值凭据形态。
- 修复外部 grant 撤销与后台 job 完成同时发生时，已撤销结果可能重新变为可读取的竞态；撤销标记与 worker 终态提交现在原子串行化。
- 审计日志写入失败不再把已经完成的副作用伪装成工具失败，也不会覆盖原始业务异常；只向 stderr 输出不含路径或异常正文的告警。
- 静态白名单 `external_*` 调用的审计标签统一为 `<external-policy>`，不再记录机器绝对路径，包含 move/copy 的双路径操作。
- 修正服务器 instructions：原来固定声明「所有路径都是工作区相对路径，绝对路径一律拒绝」，在 GRANTS 档下与 14 个只接受绝对路径的 `external_*` 工具直接冲突，会把调用方一路推向错误的工具。现在按实际注册的工具集生成说明：SAFE 档只讲工作区规则；启用外部授权后额外说明 `external_*` 走绝对路径、先用 `access_policy_explain` 判断覆盖、用 `workspace_info` 列出已授权目录，并点明 Git 工具仅限工作区、仓库在工作区外时改用 `external_run_command`。
- 修正两条会误导调用方的路径错误信息。`external_*` 会在授权根上新建一个受限 service，因此工作区 jail 的文案会原样出现在外部调用里，把「授权目录下没有这个文件」说成「Workspace path does not exist」，看起来像白名单失效。两条信息改为不再自称 workspace。

### Changed

- 移除从未接入审批校验的 TOTP 二维码脚本、环境变量加载和 `qrcode` 运行依赖；旧构造参数与 `tc -Action totp-setup` 保留兼容入口，但前者被明确忽略、后者返回迁移说明。聊天授权继续使用一次性 challenge 与明确确认，不把同通道值冒充独立第二因子。
- 新增 Windows GitHub Actions 测试矩阵，覆盖 Python 3.12/3.13、冻结锁文件、全量 pytest 与 wheel/sdist 构建。

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
- Agent 的工作目录不再写死 `<WORKSPACE>`，改为由 access policy 决定：白名单覆盖的目录都可以承载 Codex/Claude 会话，沙箱 `read-only`/`workspace-write` 分别要求规则的 `read`/`write` 能力。运行 agent 不算 `exec`，因为命令模板由服务端固定且限定在该目录，与任意 `external_run_command` 分开授权。
- Agent 会话每次 run 前重新校验工作目录授权，`attach` 的续接会话同样如此；规则被撤销、缩小或换根时下一轮立即 fail-closed，不会继续在原目录执行。
- Catalog 记录新增 `cwd_scope`，`attach` 现在接受工作目录位于白名单内的历史会话；原始绝对路径只用于服务端授权，不会出现在 MCP 返回值里。
- 新增热重载模式（`--allow-policy-hot-reload`，默认关闭，属高危档）。开启后注册四个独立工具：`access_policy_change_request` 暂存目录与模式并返回一次性 challenge，本身不授予任何权限；`access_policy_change_confirm` 只接受 `request_id`、`challenge` 与用户确认词，在类型层面就无法选择或扩大路径与权限——能力在暂存时即已冻结；另有 `cancel` 与只读 `status`。确认后原子写入 `access-policy.json`（保留 `.bak`）并立即生效，无需重启。冷重载行为完全不变。
- 热重载拒绝写入服务端自身目录（代码、策略、日志）、盘符根、Windows 系统目录、名称含 credential/secret/key 等敏感组件的路径，以及被显式 `deny` 规则覆盖的路径；批准时会重新校验一次，防止暂存后策略已变。
- CLI 新增 `--agent-sources` 与 `--agent-catalog`，可启动一个不读取本机历史 source 的隔离实例。

### Changed

- `workspace_info` 的 `access_policy` 现在逐条列出白名单规则（路径、模式、`allow_exec`、`require_approval`、启用状态、备注），不再只返回规则数量。此前调用方无法知道自己能用哪些目录，只能先猜路径再用 `access_policy_explain` 逐个验证；而 `explain` 本来就能问出同样的信息，所以隐藏列表不增加任何安全性，只增加使用摩擦。停用的规则同样列出，便于分辨"未授权"与"已停用"。
- 引入 provider-neutral `AgentAdapter`、capabilities 和 profile→adapter 绑定；Codex 的命令构造与 JSONL parser 已迁出通用 runtime。
- Catalog schema v2 将每条索引绑定到 source root 与候选文件 identity；list/inspect 会重新校验 identity、size 与 mtime，目录遍历错误会降级为 partial refresh 而不会误删未扫描记录。
- Agent session/run payload 新增 `provider` 与 `native_session_id`，同时保留 `thread_id` 兼容字段；`agent_session` 在 0.9c 增加 `attach`，`agent_run` action 不变。
- 生命周期事件使用 adapter display name，runtime 不再硬编码 Codex 文案；provider/profile 绑定不一致时 fail-closed。
- Attached session 每轮执行前重验授权；允许同一 identity 的原生历史正常追加，但 source disable、root/file replacement、native id/cwd 变化都会拒绝，绝不回退 `--last`。
- Claude `read-only` 仅开放 Read/Glob/Grep，`workspace-write` 仅增加 Edit/Write；不接受 Bash、任意 settings/agents/plugins/MCP/add-dir/model/effort 或危险权限参数。
- Catalog 将 Windows 无符号 64 位文件 identity 无损映射为 SQLite int64，避免部分卷上的 `st_dev/st_ino` 触发写入溢出并拖垮 refresh。
- Catalog 所有 SQLite 连接现在显式关闭，避免 Windows 文件锁和长期运行时的连接泄漏阻断 refresh/rebuild。
- Agent source 原子保存若在 ACL 收紧阶段失败，会恢复上一版（首次创建则撤销），不会留下“命令报错但宽权限新策略已生效”的半完成状态。
- Agent profile 显式声明是否接收已配置的业务环境变量；`codex-default` 保留既有透传，`claude-default` 默认不接收 `EXAMPLE_AGENT_KEY` 等变量。

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

- 使用不依赖 Codex JSONL 的 fake adapter 覆盖 create、resume、事件、结果与 provider binding。
- 0.9a 全量测试 `103 passed, 1 skipped`；用户随后在管理员 PowerShell 将原 symlink skip 定向补跑为 `1 passed`。
- 0.9b 提交前全量测试 `125 passed, 1 skipped`；唯一 skip 仍为当前普通 Windows 测试进程没有 symlink 创建权限，用户已在管理员 PowerShell 将同一用例定向补跑为 `1 passed`。
- 0.9c 提交前全量测试 `129 passed, 1 skipped`；合成 stdio smoke 实际完成 catalog list、session attach、run start/inspect/result，未读取真实 history 或调用真实模型。
- 0.9d 提交前全量测试 `134 passed, 1 skipped`；fake Claude 覆盖 create、resume、Catalog attach、result、agent-only executable 与 prepared-process fail-closed 边界。
- 0.9e source/TUI 批次全量测试 `140 passed, 1 skipped`；隔离 fixture 覆盖 discovery、ADD 确认、ACL rollback、增删启停、refresh、Windows 可恢复 rebuild 与 launcher JSON 状态。
- 0.9e smoke/env 批次全量测试 `141 passed, 1 skipped`；fake runtime 覆盖固定 smoke prompt/生命周期，fake secret 覆盖 Codex/Claude profile 环境隔离。
- stdin EOF 修复后，真实 `codex-default` smoke 使用 `gpt-5.6-sol` 成功验证 marker，Agent runtime 内部耗时 `13.483s`；同轮全量测试 `142 passed, 1 skipped`。
- Claude 参数顺序修复后，真实 `claude-default` smoke 使用 `claude-sonnet-5` 成功验证 marker，Agent runtime 内部耗时 `7.288s`。
- job record 回收批次全量测试 `145 passed, 2 skipped`。两个 skip 均为环境限制：当前测试进程没有 symlink 创建权限，且本机 `rg` 不在 PATH（`shutil.which('rg')` 为 None，`search_text` 回退到 Python 实现）。
- 0.5-A 真实 stdio 全链验收：经 `run-mcp-exec.ps1` 走真实 stdio JSON-RPC，Codex `codex-default` 与 Claude `claude-default` 各完成 create → 任意写入任务 → 同 session resume → events/result → cancel → close，合并 `40/40` 通过，耗时 `146.1s`。
- 0.9.0 收口前全量测试 `160 passed, 2 skipped`（两个 skip 仍为环境限制：无 symlink 创建权限、本机 `rg` 不在 PATH）。
- 0.9.0 经 ChatGPT 连接器真实端到端验收：`codex-default`（cwd `<WORKSPACE_A>`）与 `claude-default`（cwd `<WORKSPACE_B>`）各完成 create → start → events 游标轮询 → result → close，全部 `succeeded`、exit code `0`，首个 event 分别为 `5.57s` 与 `3.52s`，全程无工具调用被连接器拦截；两个 agent 的输出均包含各自仓库根目录的真实文件清单。
- 0.9.0 多 Agent 编排验收：`codex-default` 与 `claude-default` 两个 session 在 `<WORKSPACE>` 同时处于 `running`（B 启动后立即 inspect A 仍为 `running`，非串行伪并行），各自 cursor 独立且事件流互不串台（A 的事件不含对方 marker，反之亦然）；Claude 随后在同一 session 的第二个 run 中读取 Codex 写出的文件并追加到自己的产物，最终内容经 `read_text` 读回确认同时包含两个 marker，构成基于共享 workspace artifact 的真实交接；对同一 session 的重复 `start` 按预期拒绝（`Only one active run is allowed per agent session`），长任务 `cancel` 后终态为 `cancelled`，两个 session 均正常关闭。
- 上述验收的每一条“文件已创建”结论均由 MCP `read_text` 独立读回内容验证，不采信模型自报；resume 前后 `native_session_id` 一致；`read-only` session 的写入未产生文件；测试目录经 `delete` 进入 trash 且原路径消失；运行结束后无 Codex/Claude 子进程残留。
- 该验收在修复前实测复现了 job record 耗尽：单次运行产生 251 次 `agent_run_events`，随后 `mkdir`/`delete`/`trash_list` 全部拒绝服务，而 `stat` 仍可用；修复后同一链路轮询次数降至 1–7 次。
- 白名单/`browse`/热重载真实 stdio 验收 `26/26` 通过，使用一次性策略文件，未修改机器上真实的 `access-policy.json`。覆盖：批准前 Agent 与列目录均拒绝；`request` 暂存不授予任何权限；错误 confirmation 被拒；服务端自身目录、config 目录与盘符根被拒；`browse` 只返回一层且 `depth=5` 被夹回一层、拒绝文件内容、不能承载 Agent；提升为 `write` 后 Codex 与 Claude 各自在工作区外的目录真实写入文件，内容由 MCP 独立读回验证，且同一文件用工作区工具读不到；把规则收窄回 `browse` 后 Agent 与读取立即失效，全程未重启。
- **平台限制（非本项目缺陷）**：经 ChatGPT 连接器远端验收时，`access_policy_change_confirm` 始终无法到达 TianCheng 服务端，被 ChatGPT 侧的安全检查拦截；同一流程中的 `request`、`status`、`cancel` 均可正常到达。把单一多 action 工具拆分为四个独立工具、并把 confirm 的 schema 收窄到只接受 `request_id`/`challenge`/`confirmation` 之后，该行为没有变化，说明拦截针对的是"提交权限变更"这一动作本身，而非 schema 形态。审计日志确认拦截发生在服务端之外：被拦后 staged request 保持 pending，未产生半授权状态，`fail-closed` 行为正确。因此策略热重载的端到端能力目前只在本地 stdio 客户端验证通过，ChatGPT 侧不可用。未采取任何针对该安全检查的规避措施。
- 验收过程中确认两条真实环境约束：`external_*` 文件工具只在 external grants 开启时注册，因此热重载需配合 GRANTS Profile 才完整可用；Codex 拒绝在非 git 仓库目录运行，服务端不传 `--skip-git-repo-check`。
- 出厂默认 source count 仍为 0：不配置任何 source 就不会读取本机历史，需用户显式授权后才会扫描。
- 0.5-B 真实历史 attach/resume 已由用户授权后实测：本机 source 索引 752 个 transcript 文件（Codex 634、Claude 118），Codex 与 Claude 各自从 Catalog `conversation_ref` attach 到一段真实历史会话并原生 resume，各 `11/11` 通过；判定依据是被恢复的会话能答出仅存在于其自身上文中的事实（`RESUMED-OK <n>`），而非模型自报成功。验收脚本不打印任何 transcript、标题或提示词正文，只做有界 marker 校验。
- 0.5-C Tunnel/ChatGPT 远端全链已完成，结果记录于上方条目：两个 provider 端到端跑通，并另行验证了并行、事件流隔离、共享 artifact 交接与单 session run 锁。
- 剩余限制：策略热重载的 `confirm` 在 ChatGPT 连接器下仍不可用（见上方平台限制条目），该能力目前只在本地 stdio 客户端验证通过。

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
- `workspace_info` 增加当前策略规则摘要；默认仍只允许 `<WORKSPACE>`。
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
