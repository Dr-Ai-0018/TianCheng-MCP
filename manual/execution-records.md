# 执行记录：本机查询、持续查看与诊断导出

0.19.0 新增执行记录，保存工具、job、进程、Agent 和 Supervisor 已观察到的生命周期及安全结果元数据。默认使用 `summary`；详情须显式配置，不能从旧摘要还原输出。0.18.0 已发布包不包含这批新增实现。

## 从 tc 查看

Windows 主菜单 **O** 或 `tc -Action records` 打开执行记录。菜单提供状态、操作列表、筛选、详情、下一页、持续查看和导出。窄窗口或重定向终端自动使用文本菜单；结果页沿用上下键及 PgUp/PgDn。

`tc -Action records -Json` 只查询本机记录和下次启动配置，不连接模型、不启动业务服务。`available` 表示历史存储可读，**不代表服务在线**；`capture_config.scope=next_start` 不是当前实例配置。当前实例的实际模式和健康应查看 `workspace_info.execution_record_health`。

健康中的 `metadata_pending` 表示共享锁繁忙、元数据等待刷新，`metadata_deferred_count` 累计这种延后；已同步的事件不会因此重复写入或计作丢失。真正的写入错误仍计入 `write_failure_count`，历史故障与未关闭记录的质量提示会保留，重启不会清除旧证据。

列表只对当前有界扫描页排序，不保证全局最新优先；还有游标时使用“下一页”。分页详情的完整性只针对本页已找到的证据，不要把独立页面拼接成未经验证的全局成功结论。同值资源 ID 可能匹配多项操作，界面保留多重匹配提示。

## Windows / Linux CLI

在安装该功能源码的虚拟环境中运行：

```text
python -m tiancheng_mcp.observability_admin status
python -m tiancheng_mcp.observability_admin recent --limit 50 --text
python -m tiancheng_mcp.observability_admin recent --tool run_command --status failed --since 2026-10-01T00:00:00Z --text
python -m tiancheng_mcp.observability_admin show OPERATION_ID --text
python -m tiancheng_mcp.observability_admin show OPERATION_ID --detail --text
python -m tiancheng_mcp.observability_admin follow OPERATION_ID --text
```

默认输出 JSON；`--json` 显式选择同一格式，`--text` 输出经过终端控制符过滤的可读文本。`show`、`follow` 和 `export` 接受 operation ID，也接受已记录的 job/process/session/run 等关联 ID。`recent` 和 `show` 的 `--cursor` 须与原查询的筛选和详情模式一致，不能跨存储或查询类型复用。

需要指定本机配置或独立验证记录时，全局参数放在子命令前：

```text
python -m tiancheng_mcp.observability_admin --local /absolute/path/launcher.local.json status
python -m tiancheng_mcp.observability_admin --storage-root /absolute/private/records recent --text
```

Ctrl+C 只结束 follow 读取器，不向任务发送取消。任务完成后 follow 仍可等待关联的新事件；退出后可继续查询历史。缺口、淘汰、扫描未完成及捕获丢失会持续显示。

## 模式与存储

在本机 `launcher.local.json` 中配置，保存后对新实例生效：

```json
{
  "observability": {
    "mode": "summary",
    "summaryRetentionDays": 30,
    "summaryMaxMiB": 256,
    "detailRetentionDays": 7,
    "detailMaxMiB": 128,
    "detailWindowMinutes": 30
  }
}
```

- `summary` 保存生命周期与受控结果；不保存 prompt、完整 argv、环境值或文件正文。
- `detail` 为新任务额外捕获经脱敏的有限普通命令/进程输出；默认窗口 30 分钟，过期回到摘要捕获。配额只作用于额外观察副本，业务输出与执行权限保持原契约。Agent stdout/stderr 即使在 detail 下也不持久化，只保存受控 provider 元数据。
- `off` 关闭新增过程捕获，既有审计和业务权限仍生效。

Windows 默认记录位于项目 `state/execution-records`；Linux 位于 `$XDG_STATE_HOME/tiancheng-mcp/execution-records`，未设置时使用 `~/.local/state/tiancheng-mcp/execution-records`。可用绝对 `storageRoot` 指定独立私有目录；记录必须位于业务工作区之外。Windows 使用当前用户私有 ACL，POSIX 使用私有目录和文件权限。查询不会为了方便读取放宽权限。

## 预览与导出

```text
python -m tiancheng_mcp.observability_admin export OPERATION_ID --preview --text
python -m tiancheng_mcp.observability_admin export OPERATION_ID --output /absolute/private/export.json
```

默认只导出摘要；两次都加 `--detail` 才包括仍保留的有限详情。预览扫描与导出使用同一模型，显示 operation、详情选择、事件数、字节数、SHA-256 和质量信息，不写文件。活跃记录可能在预览后增长，实际回执才是最终文件的大小与哈希。

父目录必须已经存在并满足私有权限；文件提交不覆盖已有文件。诊断 JSON 包含 schema、产品版本、选定事件及质量信息，最大 16 MiB，并有扫描时间/字节预算。导出再次应用安全字段投影和输出脱敏，不包含本机配置、密钥、原始 provider 协议、完整会话或源码。导出仍可能包含业务信息，分享前需自行核对接收对象。

## 如何判断结果

`invocation_outcome=returned` 仅表示工具调用返回；命令 `exit_code=7` 应结合 `operation_outcome=failed` 查看。Agent 进程 exit 0 不证明模型任务实现正确；任务结果保留 `unverified`，需要独立文件或其他业务证据验收。审批提交、送达、实际执行和任务成功分别记录。

`record_complete=false`、`cursor_gap`、`scan_incomplete`、`capture_loss` 和未知终态都不能隐藏成“成功”。记录为有限、尽力捕获：压力、磁盘、退出或淘汰可能造成缺失；普通本机 JSONL 不提供防篡改、不可抵赖或永久保存保证。stdout 仍只用于 MCP 协议，记录查看属于独立的本机管理入口，不开放聊天读取日志目录。
