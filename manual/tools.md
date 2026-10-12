[手册目录](README.md) · [Windows 首次使用](quickstart-windows.md)

# 工具参考

## 默认工具

工具是否注册取决于启动模式；以当前实例的工具列表和 `workspace_info` 为准。

| 分类 | 工具 | 说明 |
| --- | --- | --- |
| 信息 | `workspace_info` | 工作区、版本、能力、exec/Git 状态；不返回用户、环境变量或主机信息 |
| 文件读取 | `list_dir`, `stat`, `hash_file`, `read_text`, `read_text_chunk` | 有递归深度、读取量和二进制拒绝限制；大文件可用稳定字节游标续读；哈希有大小上限 |
| 图片读取 | `read_image`, `media_result` | 静态 PNG/JPEG/WebP 原生图片内容；后台图片用 `media_result` 获取，旧 `job_result` 只返回句柄及 metadata |
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

### 图片读取（0.18.0）

工作区图片使用相对路径，例如 `read_image(path="images/map.png")`。开启外部授权的实例另有
`external_read_image`：使用静态读取规则覆盖的绝对路径，或传入有效 `grant_id`。
仅允许读取图片，不会因此开启命令执行或网络下载。

| mode | 返回内容 |
| --- | --- |
| `preview`（默认） | 等比预览，不放大；按 EXIF 校正方向，保留透明背景，移除预览中的 EXIF/ICC |
| `original` | 校验后返回原文件字节，不改方向或元数据；原文件必须不超过 4 MiB |
| `metadata` | 完整解码校验后，只返回格式、尺寸、大小及 SHA-256，不返回像素 |

源文件最多 32 MiB、2,400 万像素；仅支持扩展名与实际格式一致的静态 PNG、JPEG、WebP。
拒绝动画、损坏图片、路径遍历、symlink/junction/reparse。预览最长边 1,600 像素、最多 1 MiB；
完整 MCP JSON 最多 6 MiB。这些是项目预算，并非 OpenAI/Tunnel 的官方额度。
每进程最多 2 个解码子进程，排队与解码共享 30 秒期限；取消或超时会终止并回收解码进程。

如果先返回 `job_id`，调用 `media_result(job_id="...")` 获取原生图片。`job_status` 和
`job_cancel` 仍用于观察或取消；`job_result` 保持 JSON 契约，不把 base64 图片混进文本。
结果仅在当前服务进程有效，15 分钟后不可获取；最多 64 条、合计 64 MiB，满后拒绝新结果。
取结果时会再次检查当前读取授权和源文件 SHA-256；文件已改动或外部 grant 撤销/过期时需重新读取。
服务关闭会清空缓存；过期条目在下一次缓存访问时回收。

私有 Tunnel/GPT 网页随机图片盲测已验证模型能识别工具返回的原生图片。
用户界面是否直接展示图片取决于客户端，本轮没有看到内联预览。原文件导出下载已独立验收，
聊天附件在 Windows Clash Verge 的 `clash_proxy` 模式下已用真实客户端验证落盘，
不能用 `read_text` 当作二进制传输入口。

### 原文件导出（0.18.0，默认关闭）

本机 `config/launcher.local.json` 中设置 `"media": {"exports": true}`，然后重启实例，
才会注册实验工具 `export_file`；外部授权实例另有 `external_export_file`。
`"images": false` 可独立关闭图片工具。配置只影响下一次启动；`workspace_info.media`
报告当前实例实际加载的配置。聊天工具不能修改这些开关。
控制台首页 **F · 图片与文件交换** 可分别设置这些开关；`tc media -Json` 只查看
下次启动配置，不启动服务、不查询网络。保存时保留其他字段并备份为 `.media.bak`。

导出返回随机资源 URI，不含主机路径；`resources/read` 返回原字节，PDF/ZIP/XLSX 不执行、
不解包、不转换。单文件最多 4 MiB，完整资源 JSON 最多 6 MiB；超额拒绝。
每实例最多 64 个链接，15 分钟到期，重启失效；列举资源不枚举本机文件。
每次读取重新核对当前读取权限和文件 SHA-256，撤权或文件变化即拒绝。
链接属于当前服务实例：没有聊天身份隔离保证，知道 URI 且可访问该实例的调用者可以请求读取。

私有 GPT/Tunnel 已验证资源实体化后的原字节及 SHA-256；用户点击下载后，本机文件的大小和
SHA-256 也与源文件一致。这是合成测试文件的验收结果，不代表所有客户端都支持导出。
`download_verified=false` 表示工具只准备链接，不能据此认定用户已经下载成功。
不支持下载的客户端仍需用户在本机取文件，不自动外传到对象存储。

### 聊天附件导入（0.18.0，默认关闭）

本机配置 `media.attachments` 的 `mode` 默认为 `off`，不注册导入工具。
选择 `direct` 或 `clash_proxy`，填写 `hosts` 的精确 DNS 主机列表后重启，才注册实验工具 `import_attachment`。
不允许 URL、端口、通配符或 IP 作为主机；只能在本机修改，聊天不能扩大允许列表。
私有 GPT/Tunnel 已完成合成 PNG 和 ZIP 的真实 FileRef 导入，回执、`hash_file` 及本机源文件
逐字节核对一致；同 key 重试复用回执，不同 key 写同一目标被拒绝。此结果限定本轮 Windows
Clash Verge/Edge 环境及 `clash_proxy` 模式，不代表所有文件类型、客户端或直连网络均已验收。

`file` 是客户端提供的 FileRef（`download_url`、`file_id`，可选 `mime_type`、`file_name`），
不是任意 URL 下载入口。需提供 1–128 字符 `idempotency_key`；相同 key/file_id/目标可查询
同一执行结果，不会重复落盘。失败执行要重新提交时用新 key；回执证明提交时的字节/hash，
不会自动恢复后来被删除或改动的文件。后台任务用普通 `job_result` 取回执。

目标只接受工作区相对路径、父目录必须存在；省略时在工作区根目录生成随机 `.bin` 文件。
附件原名和 MIME 不用于选择路径或启动解析器。最多 32 MiB，保持原字节，不执行、不解包。
同目录暂存后使用硬链接原子提交，已有目标永不覆盖；不支持硬链接的文件系统会失败，
不退回覆盖写入。成功后的暂存清理失败会返回 `staging_cleanup_pending=true`。

`direct` 明确表示直连，不继承菜单 P 的代理、父进程代理变量、Cookie 或认证头。
HTTPS 来源每次解析及最多 3 次重定向都检查全部 DNS 地址为公网；连接固定到已核验 IP，
TLS 仍校验证书和原主机名。拒绝私网地址、HTTP 降级及压缩响应。
代理软件的 Fake-IP 地址也会被拒绝，此时返回包含 `Fake-IP` 的固定诊断，不回显签名 URL。
这意味着当前下载路径不可用，不应通过放宽公网地址检查解决。
不要为了附件测试替换整套 DNS 配置或开启全局 DNS 覆写；这可能影响其他网站。
在 Windows Clash Verge 环境下，可以显式选择下面的附件专用代理模式。菜单 P 的设置不会自动
用于附件下载，`direct` 也不会遇到错误就自行改走代理。
最多 2 个下载子进程，连接等待 5 秒，排队到传输总期限 60 秒；超时/取消终止并回收子进程。
签名 URL 不进入 argv、日志或回执。过期文件需要重新上传。

#### Clash Verge 专用代理

控制台 **F → 3** 选择 `clash_proxy`，填写精确附件来源主机，再填写当前 Clash HTTP/Mixed
端口对应的代理 URL，例如 `http://127.0.0.1:7897`。端口以你自己的 Clash 设置为准。
只接受无用户名、密码的回环 HTTP 代理，不支持远程代理、SOCKS 或代理认证。
保存影响下一次启动；请在方便重启时应用，再核对 `workspace_info.media`。

等价本机配置示例（主机为占位符，需要替换；不要填写签名 URL 或 API Key）：

```json
{
  "media": {
    "attachments": {
      "mode": "clash_proxy",
      "hosts": ["files.example.com"],
      "proxy_url": "http://127.0.0.1:7897"
    }
  }
}
```

第一版仅适配 Windows Clash Verge 的生产命名管道：按当前用户配置目录读取控制接口密钥，
只向唯一的本机生产管道发 DNS 查询。密钥不进入命令行、云端摘要或附件服务器请求。
该模式显式信任本机 Clash；不提供 DNS 答案真实性保证。所有返回地址仍检查为公网，
每次跳转重新查询并校验，代理 CONNECT 到已验证数字 IP，TLS 校验原附件主机名。
控制接口或代理不可用、多个管道冲突、响应无效、TLS 失败时直接报错，不回退其他路径。

不要开启 DNS 覆写、替换订阅 DNS 或粘贴控制密钥来使用本功能。Clash 只需按原配置运行。
切回 `off` 或 `direct` 后不会读取 Clash 控制配置。当前已用正式代码完成公开小文件的本地
下载、无覆盖导入和已知 hash 验证；真实 GPT 的 PNG/ZIP FileRef 导入及防覆盖保护也已验证。

技术依据：[Mihomo DNS 查询实现](https://github.com/MetaCubeX/mihomo/blob/Meta/hub/route/dns.go)
和 [Python HTTP CONNECT](https://docs.python.org/3/library/http.client.html#http.client.HTTPConnection.set_tunnel)。

#### 三个使用例子

先确保 Tunnel 已连通并保持运行，再创建或按需刷新网页 MCP 工具；开启实验能力后需重启本机实例。
实例工具列表有变化时，在网页自定义插件列表刷新对应连接，再核对实际工具数。
本轮移除测试专用探针后 Local MCP 工具数由 36 变为 35；此数字是测试 Profile 的结果，
常用 Profile 的数量随权限模式和开关变化。若网页报告工具缺失而本机没有收到调用，
先确认刷新完成，并在该条消息中显式选择 Local MCP；不要把未发起的调用误判为下载失败。

- 看图：发送“调用 `read_image(path=\"images/map.png\", mode=\"preview\")`，说明图片中的主要内容”。
  如果先得到 `job_id`，再调用 `media_result`；模型能识别图片不代表界面一定显示预览。
- 取文件：开启导出后，发送“调用 `export_file(path=\"reports/result.zip\")`，提供原文件下载入口”。
  下载后可核对本机 hash；单文件最多 4 MiB，不会自动解包。
- 导附件：开启附件并配置真实来源主机后，在聊天中上传文件，再发送“用这个附件的真实 FileRef
  调用 `import_attachment`，目标 `received.bin`，`idempotency_key=\"upload-check-01\"`；
  不要展示签名 URL，完成后调用 `hash_file(path=\"received.bin\")`”。已有目标会拒绝，
  失败后重试使用新 key；不允许模型编造 FileRef 或用任意 URL 替代附件。

### 其他工具

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

开发者执行真实媒体验收时，使用[持续运行的隔离验收入口](media-acceptance.md)，
通过正式启动器管理测试 Profile，测试结束后明确停止。
