[手册目录](README.md) · [Windows 首次使用](quickstart-windows.md)

# 后台任务与进程

## 自动后台兜底

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
Agent run 由 `agent_run` 的 `inspect`、`events`、`result` 和 `cancel` 管理；通用
`process_*` 工具与 `list_processes` 不访问 Agent 进程，Agent 返回值也不暴露内部
`process_id`。`job_*` 只管理超过交互预算的后台工具调用，与上述两种生命周期分开。

受管进程和 Agent 共享最多 32 个正在准备、启动或运行的进程槽位，Agent 自身的并发限制仍生效。
退出时关闭管道和系统句柄，输出和状态保留在有界历史中。普通进程历史最多保留 1 小时、128 条、
64 MiB 总输出，触及任一限制会先淘汰最早结束的记录；调用方需要长期结果时应及时回读并保存。
`list_processes` 返回共享槽位占用与历史上限；状态提供 `resources_released`、
`resource_cleanup_error`、`output_drain_incomplete` 和 `history_expires_at`。近期被淘汰的 ID
返回明确的 `history expired`，该标记本身也有界；超出标记保留范围后返回普通未找到错误。
Agent 的事件、错误摘要及最终结果仍按原有 session/run 上限保留，不保留终态的原始进程输出。

所有 Agent 协议按原始字节增量解码 UTF-8，再按 JSONL 分帧；支持中文、emoji 跨块与退出时无换行的
最后一行。单行上限为 256 KiB，无效编码或超长帧使 run 明确失败。输出缺口和未完成排空会记入结果；
Pi 与人工审批模式遇到这类不完整协议会失败。普通 provider 的进程成功仍不代表任务已经完成。
