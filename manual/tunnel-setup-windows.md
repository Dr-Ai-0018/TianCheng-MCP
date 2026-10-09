# 创建 Tunnel、专用 Key 与安装官方客户端

[手册目录](README.md) · [接着完成首次连接](quickstart-windows.md)

第一次安装建议直接使用 [从零安装到输入 tc 的完整教程](quickstart-windows.md)，它已内联本页步骤，不需要来回跳转。

本页先完成三个准备：创建 Tunnel、创建只允许 Tunnel 的运行密钥、下载并放好 tunnel-client。然后回到首次使用指南配置本机 Profile。

## 1. 创建 Tunnel

打开 [OpenAI Platform → Organization → Tunnels](https://platform.openai.com/settings/organization/tunnels)，确认选中了准备使用的组织，再点 **Create tunnel**。

![创建 Tunnel 的真实表单](images/create-tunnel.jpg)

截图裁掉了已有 Tunnel 列表，并清空了未提交表单中的个人组织选择；实际创建时必须选择自己的组织。

| 字段 | 填什么 |
| --- | --- |
| Name | 便于辨认的名字，例如 `My Windows MCP`。必填。 |
| Description | 简短用途，例如 `Local workspace MCP on my Windows PC`。必填。 |
| Organizations | 选择拥有或管理这个 Tunnel 的 Platform 组织。可搜索准确的组织 ID。 |
| ChatGPT workspaces | 选择准备使用 MCP 的 ChatGPT 工作区。可按名称或准确的 workspace ID 搜索。 |

确认组织和工作区后点 **Create**，在列表中用 **Copy tunnel ID** 复制 `tunnel_...`，保存供稍后的本机 Profile 配置使用。名称不是 Tunnel ID。

创建/编辑 Tunnel 需要组织级 **Tunnels Read + Manage**；运行客户端与从 ChatGPT 选用 Tunnel 需要 **Read + Use**。这些组织权限与下面 Key 的限制分别生效，不能通过放宽 Key 绕过组织权限。详见 [官方 Tunnel 文档](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。

## 2. 创建只允许 Tunnel 的专用 API Key

打开 [Organization → API keys](https://platform.openai.com/settings/organization/api-keys)，点 **Create new secret key**。使用 API keys 页面，不要为这个客户端创建 Admin key。

先确认选中 **Restricted**：

![新建 Key 时选择 Restricted](images/create-tunnel-key.jpg)

按下面填写：

1. **Owned by**：个人电脑首次使用选 **You**。
2. **Name**：建议 `My Windows Tunnel`，用于辨认和后续撤销。
3. **Project**：选择你有权限使用的项目。Key 的项目选择与 Tunnel 的组织/工作区关联是不同设置。
4. **Expiration**：按页面提供的选项选择适合自己的有效期；到期后客户端需要换 Key。
5. **Permissions**：选 **Restricted**（自定义/受限权限），不要接受默认 **All**。
6. 其他能力全部保留 **None**；找到 **Tunnels**，展开后选择 **Use**。
7. 当前实测页面会自动同时选中 **Read** 与 **Use**，该行显示 **All selected**，底部显示 **2 selected permissions**。这里的 All selected 只指 Tunnels 这一行，不是整个 Key 的 All 权限。
8. 核对只有这两项权限，再点 **Create secret key**，将显示的密钥保存到自己的安全位置。

Tunnels 展开后应同时勾选 Read 与 Use，底部为 2 项权限：

![只开放 Tunnels Read 和 Use](images/tunnel-key-permissions.jpg)

<details>
<summary>查看其他能力均为 None 的完整权限概览</summary>

![Restricted 权限概览](images/tunnel-key-permissions-overview.jpg)

</details>

这些截图来自未提交的演示表单，没有填写个人项目或生成密钥；实际创建时须选择自己的项目。

权限核对表：

| 设置 | 专用运行 Key 的目标值 |
| --- | --- |
| Permissions | Restricted |
| Tunnels | Read + Use；页面中为 All selected |
| Models、Files、Agents 及其他能力 | None |
| 选中权限数量 | 2 |

这套表单字段和自动勾选行为已在真实 Edge 页面核对。运行文件/Git MCP 不需要给这个 Key 开模型、文件或 Agent API 权限；本地 Agent 的模型凭据另外配置。

不要将完整密钥写进截图、仓库、工作区或聊天。在本项目中通过控制台 **7. API Key 管理** 的隐藏输入保存即可，不要直接放进命令参数。本机 `.env` 保存是明文文件，程序会尝试收紧 ACL。

组织权限的最小授权依据见 [官方权限说明](https://developers.openai.com/api/docs/guides/rbac)。若提示权限不足，分别检查组织角色、Key 的 Tunnels 权限和项目选择。

## 3. 下载正确的 Windows 客户端

从 Tunnel 列表右上角 **Download tunnel-client**，或直接打开 [OpenAI 官方最新发布页](https://github.com/openai/tunnel-client/releases/latest)，展开 **Assets**。

| 电脑架构 | 下载的 ZIP 文件名格式 |
| --- | --- |
| 常见 Intel / AMD Windows 电脑 | `tunnel-client-v<版本>-windows-amd64.zip` |
| Windows ARM 电脑 | `tunnel-client-v<版本>-windows-arm64.zip` |

不确定架构时，在 Windows **设置 → 系统 → 关于 → 系统类型** 查看。这里使用标准 tunnel-client 包；不要下载 Source code，也不需要为本项目选择 runtime 或 runtime-cloudflared 包。链接跟随最新发布，文件名里的版本会变化。

解压整个 ZIP，保留附带文件。已检查官方标准 Windows 包：`tunnel-client.exe` 在解压目录根部。不要在 ZIP 预览窗口里直接运行。

## 4. 放在哪里、配置怎么填

建议将整个解压目录放在稳定位置，例如自己的：

```text
%LOCALAPPDATA%/Programs/OpenAI-Tunnel/
    tunnel-client.exe
    cloudflared.exe
    LICENSE
    NOTICE
    ...
```

这是建议位置，不是程序的强制目录。它必须在 MCP 工作区之外；不要放进临时下载目录后再删除或移动。

在 PowerShell 7 查看建议位置并验证程序：

```powershell
$tunnelExe = Join-Path $env:LOCALAPPDATA 'Programs/OpenAI-Tunnel/tunnel-client.exe'
$tunnelExe
Test-Path -LiteralPath $tunnelExe
& $tunnelExe help quickstart
```

`Test-Path` 应返回 `True`，最后一条应显示官方帮助。如果是 False，先查实际解压目录，常见原因是多套了一层文件夹。

首次使用不必修改 PATH。将上面打印的完整 exe 路径填写到 `config/launcher.local.json` 的 `tunnelClient` 字段即可，例如以下配置片段：

```json
{
  "tunnelClient": "C:/Users/<USER>/AppData/Local/Programs/OpenAI-Tunnel/tunnel-client.exe"
}
```

替换 `<USER>`，或直接使用你实际解压位置；JSON 中推荐正斜杠。若已把 exe 所在目录加入 PATH，可将 tunnelClient 留空。两种方式选一种即可。

## 5. 接下来按这个顺序

回到 [Windows 首次使用](quickstart-windows.md)，安装项目依赖、设置工作区、保存 Key、用 Tunnel ID 创建 SAFE Profile，然后启动 MCP + Tunnel。

**先让 Tunnel 工具连接并验证通过，保持客户端运行，再到 GPT 网页版创建自定义 MCP 插件。**只创建了 ID 或本机 Profile，还没有完成这一关。

插件创建后，再让 ChatGPT 调用 workspace_info 并实际写入、读回测试文件。不要把 Tunnel 就绪状态当作已经完成文件工具验收。
