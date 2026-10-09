[手册目录](README.md) · [Windows 首次使用](quickstart-windows.md)

# 工具参考

## 默认工具

工具是否注册取决于启动模式；以当前实例的工具列表和 `workspace_info` 为准。

| 分类 | 工具 | 说明 |
| --- | --- | --- |
| 信息 | `workspace_info` | 工作区、版本、能力、exec/Git 状态；不返回用户、环境变量或主机信息 |
| 文件读取 | `list_dir`, `stat`, `hash_file`, `read_text`, `read_text_chunk` | 有递归深度、读取量和二进制拒绝限制；大文件可用稳定字节游标续读；哈希有大小上限 |
| 文件写入 | `write_text`, `edit_text`, `append_text`, `mkdir`, `move`, `copy` | UTF-8；覆盖/精确替换/追加采用同目录临时文件 + `os.replace`；修改支持 SHA-256 乐观锁 |
| 回收 | `delete`, `trash_list`, `trash_restore`, `trash_purge` | 默认可恢复；只有显式 purge 永久销毁并标成 destructive |
| 查找 | `glob`, `search_text` | `search_text` 优先使用 ripgrep，先枚举受 ignore 规则约束的候选文件并按 glob 过滤，再按总扫描字节、结果、输出和超时限制 |
| 本地 Git | `git_status`, `git_diff`, `git_log`, `git_init`, `git_add`, `git_commit` | 默认安全 Profile 只提供工作区内的本地仓库操作 |
| 长任务控制 | `job_status`, `job_result`, `job_cancel`, `job_list` | 任意工具超过交互预算会自动转为后台 job；通过 job_id 查询、分页读取或取消 |
| 本地 Agent Catalog | `agent_catalog` | 只查询已由本地配置授权的 Codex/Claude 会话 metadata；不能添加路径或读取原始 transcript |

`run_command` 默认根本不注册。只有使用 `run-mcp-exec.ps1` 或命令行
`--allow-exec` 时，才额外注册 `git_remote_list/add/set_url/remove`、
`git_clone/fetch/pull/push`、`run_command`，以及
`start_process/process_status/process_output/process_input/list_processes/stop_process`、
`agent_session/agent_run/agent_approval`。
Dev Profile 会复用当前
Windows 用户的 Git 配置、Git Credential Manager 和 GitHub CLI 登录；remote URL 不得
内嵌密码或 token，`git credential*` 与 `gh auth token` 这类直接输出凭据的入口会被拒绝。
当前 SDK annotations 已为
只读、写入、destructive 和 open-world 工具分别标注；`delete` 即使采用回收站也标为
destructive，`run_command` 同时标为 destructive/open-world。

## 文件与资源限制

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
| Agent run 生命周期 | 1 h（推荐默认） | 3 h |
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

同一服务进程中的文件修改共享路径协调器，包括外部授权的临时 service 和重叠 workspace。
锁覆盖读取、条件检查及提交；父目录/子树冲突也会串行，等待期间可取消。独立目录可并行。
该协调器不控制其他进程、编辑器或开放执行命令的直接文件修改。

回收站删除先原子写入原路径恢复记录，再移动数据；记录失败时保留原文件。
恢复不覆盖已有目标。若数据已恢复但元数据清理失败，仍返回 `restored=true`，并用
`metadata_cleanup_pending=true` 明确提示残留记录；不会把已成功恢复的数据误报为未恢复。

`hash_file` 只读取 workspace 内文件并返回 SHA-256；默认最多处理 256 MiB，避免对异常大文件
进行无界扫描。

`search_text` 检测到 `rg` 时使用 literal JSON 搜索：默认包含 `.github` 等 hidden 开发文件、
尊重各层 `.gitignore`，并排除 `.git`、`.tiancheng-trash`、`.tiancheng-tmp`、`node_modules`
与 `.venv`。可显式设置 `respect_gitignore=false` 或 `include_internal=true`；路径匹配仍由
服务端二次校验。机器没有 `rg` 时自动回退到有总扫描字节上限的 Python 实现。

`external_search_text` 默认扫描被 `.gitignore` 忽略的文件和内部目录，保持已有外部搜索行为；
可传 `respect_gitignore=true`、`include_internal=false` 缩小范围。两个筛选开关由 `rg` 实现；
Python 回退会在结果中报告 `respect_gitignore=false`，无法保证排除内部目录。
`mkdir` 与 `external_mkdir` 都标为普通写入，因为 `exist_ok=false` 时重复调用会失败。
