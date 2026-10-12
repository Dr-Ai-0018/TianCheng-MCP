# 媒体功能的隔离验收

这是开发验收入口，不是首次安装步骤。先准备 `.tmp/media-0.18.0/launcher.local.json`：
独立 Profile 为 `media-probe`，Profile 目录为 `.tmp/media-0.18.0/profiles`，工作区为
`.tmp/media-0.18.0/functional-workspace`，健康地址为 `http://127.0.0.1:18080`。
Profile 使用正式 SAFE 启动脚本及此配置，配置开启所需媒体能力和精确附件主机。
密钥沿用本机已有来源，不复制密钥值进测试配置；不更改常用 Profile、默认配置或 Clash DNS。

项目根目录下启动：

```powershell
.\scripts\media-acceptance.ps1 -Action start
```

它调用正式 `tc.ps1` 的启动流程，由已有 Supervisor 管理进程和状态。没有测试会话的
15 分钟自动停止计时；正常情况下窗口保持运行，按 Ctrl+C 或明确停止结束。
启动或运行故障沿用正式启动器的错误和状态报告，不把异常退出当成验收通过。
连接协议 TTL、附件下载超时、导出链接有效期是各自独立的机制，不是整个测试会话的时长。

另一个 PowerShell 窗口查看状态或结束测试：

```powershell
.\scripts\media-acceptance.ps1 -Action status -Json
.\scripts\media-acceptance.ps1 -Action stop
```

测试复用现有云端 Tunnel 时，不可同时运行常用实例。启动入口检查常用健康端口和测试端口，
即使端口已经监听但尚未 Ready，也拒绝启动；不会停止冲突实例。先解决冲突再启动。
文件下载仍有独立的 60 秒期限和取消回收，不能因整个测试持续运行就取消单次操作约束。

真实网页验收顺序：确认 Tunnel 连通 → 按需刷新现有 MCP 工具 → 核对 `workspace_info`
为测试工作区和 `clash_proxy` → 上传合成文件 → 用真实 FileRef 导入 → 对照本机原文件 hash。
已有目标拒绝覆盖；重复请求复用回执。测试后明确停止测试 Profile，核对停止状态，
再在需要时启动常用实例。没有真实 FileRef/hash 证据，不把本地测试计为真实附件验收。

本轮私有 GPT/Edge 实测已完成 PNG（169 bytes）和 ZIP（141 bytes）导入，源文件、
落盘文件、回执与 `hash_file` 一致；同 key 重试复用回执，新 key 写已存在目标被拒绝，
原文件修改时间和 hash 未变。ZIP 按原字节保存，没有解包。未验证的平台或格式仍需独立验收。
