from __future__ import annotations

import asyncio
import sys
import json
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from tiancheng_mcp import __version__
from tiancheng_mcp.server import _exception_group_message


def _structured(result: object) -> dict:
    value = getattr(result, "structured_content", None)
    assert isinstance(value, dict)
    return value


def test_exception_group_is_flattened_to_bounded_message() -> None:
    group = ExceptionGroup("outer", [ValueError("safe detail"), RuntimeError("ignored")])
    message = _exception_group_message(group)
    assert message == "Background operation failed: safe detail"
    assert "outer" not in message


@pytest.mark.asyncio
async def test_stdio_initialize_tools_list_and_file_smoke(
    workspace: Path, tmp_path: Path
) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "tiancheng_mcp",
            "--workspace",
            str(workspace),
            "--audit-dir",
            str(tmp_path / "stdio-audit"),
            # Keep this instance isolated from the developer's own configured
            # history sources, so the assertions below describe the shipped
            # defaults rather than one machine's local setup.
            "--agent-sources",
            str(tmp_path / "isolated-agent-sources.json"),
            "--agent-catalog",
            str(tmp_path / "isolated-catalog.sqlite3"),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        encoding="utf-8",
    )
    async with Client(parameters, mode="legacy", raise_exceptions=True) as client:
        assert client.server_info is not None
        assert client.server_info.name == "tiancheng-local-mcp"

        tools_result = await client.list_tools()
        tools = {tool.name: tool for tool in tools_result.tools}
        expected = {
            "workspace_info",
            "agent_catalog",
            "job_status",
            "job_result",
            "job_cancel",
            "job_list",
            "list_dir",
            "stat",
            "hash_file",
            "read_text",
            "read_text_chunk",
            "write_text",
            "edit_text",
            "append_text",
            "mkdir",
            "move",
            "copy",
            "delete",
            "trash_list",
            "trash_restore",
            "trash_purge",
            "glob",
            "search_text",
            "git_status",
            "git_diff",
            "git_log",
            "git_init",
            "git_add",
            "git_commit",
            "access_policy_explain",
            "access_policy_reload",
        }
        assert expected == set(tools)
        assert "run_command" not in tools
        assert tools["read_text"].annotations.read_only_hint is True
        assert tools["delete"].annotations.destructive_hint is True
        assert tools["agent_catalog"].annotations.read_only_hint is True

        info = _structured(await client.call_tool("workspace_info", {}))
        assert Path(info["workspace_root"]) == workspace.resolve()
        assert info["server_version"] == __version__
        assert info["command_execution_enabled"] is False
        assert info["access_policy"]["enabled_rule_count"] == 1
        # The server is launched against an isolated source config above, so
        # this asserts the shipped default of "no history sources" rather than
        # whatever the developer has configured locally.
        assert info["agent_sources"]["source_count"] == 0
        providers = _structured(
            await client.call_tool("agent_catalog", {"action": "providers"})
        )
        assert {item["provider"] for item in providers["providers"]} == {
            "codex",
            "claude-code",
        }
        sources = _structured(
            await client.call_tool("agent_catalog", {"action": "sources"})
        )
        assert sources == {"sources": [], "count": 0}
        explanation = _structured(
            await client.call_tool(
                "access_policy_explain",
                {"path": str(workspace / "notes.txt"), "operation": "write"},
            )
        )
        assert explanation["allowed"] is True
        assert explanation["mode"] == "full"
        reloaded = _structured(await client.call_tool("access_policy_reload", {}))
        assert reloaded["reloaded"] is True

        written = _structured(
            await client.call_tool(
                "write_text", {"path": "协议测试/hello.txt", "content": "你好 MCP"}
            )
        )
        assert written["bytes_written"] == len("你好 MCP".encode("utf-8"))
        read = _structured(
            await client.call_tool("read_text", {"path": "协议测试/hello.txt"})
        )
        assert read["content"] == "你好 MCP"
        deleted = _structured(
            await client.call_tool("delete", {"path": "协议测试/hello.txt"})
        )
        assert deleted["permanently_deleted"] is False
        assert (workspace / Path(deleted["trash_path"])).exists()

    audit_text = (tmp_path / "stdio-audit/tiancheng-mcp-audit.jsonl").read_text(
        encoding="utf-8"
    )
    assert "你好 MCP" not in audit_text
    assert '"tool":"write_text"' in audit_text


@pytest.mark.asyncio
async def test_exec_tool_is_registered_only_when_enabled(
    workspace: Path, tmp_path: Path
) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "tiancheng_mcp",
            "--workspace",
            str(workspace),
            "--audit-dir",
            str(tmp_path / "exec-audit"),
            "--allow-exec",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    async with Client(parameters, mode="legacy", raise_exceptions=True) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert "run_command" in tools
        assert {
            "git_remote_list",
            "git_remote_add",
            "git_remote_set_url",
            "git_remote_remove",
            "git_clone",
            "git_fetch",
            "git_pull",
            "git_push",
            "start_process",
            "process_status",
            "process_output",
            "process_input",
            "list_processes",
            "stop_process",
            "agent_session",
            "agent_run",
        } <= set(tools)
        annotations = tools["run_command"].annotations
        assert annotations.destructive_hint is True
        assert annotations.open_world_hint is True
        assert tools["git_push"].annotations.destructive_hint is True
        assert tools["git_push"].annotations.open_world_hint is True
        session_properties = tools["agent_session"].input_schema["properties"]
        assert "conversation_ref" in session_properties
        assert {
            "native_session_id",
            "thread_id",
            "history_path",
        }.isdisjoint(session_properties)

        denied_attach = await client.call_tool(
            "agent_session",
            {"action": "attach", "conversation_ref": "thr_direct_injection"},
        )
        assert denied_attach.is_error is True

        created = _structured(
            await client.call_tool(
                "agent_session",
                {"action": "create", "sandbox": "read-only"},
            )
        )
        inspected = _structured(
            await client.call_tool(
                "agent_session",
                {"action": "inspect", "session_id": created["session_id"]},
            )
        )
        assert inspected["session_id"] == created["session_id"]
        assert inspected["closed"] is False
        closed = _structured(
            await client.call_tool(
                "agent_session",
                {"action": "close", "session_id": created["session_id"]},
            )
        )
        assert closed["closed"] is True


@pytest.mark.asyncio
async def test_stdio_catalog_attach_and_resume_with_synthetic_codex_history(
    workspace: Path, tmp_path: Path
) -> None:
    codex_root = tmp_path / ".codex" / "sessions"
    rollout = codex_root / "2026" / "08" / "30" / "rollout-stdio-attach.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "thr_stdio_attach",
                    "cwd": str(workspace),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    fake_agent = tmp_path / "fake_stdio_attach_codex.py"
    fake_agent.write_text(
        "import json\n"
        "print(json.dumps({'type':'thread.started','thread_id':'thr_stdio_attach'}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'attached ok'}}), flush=True)\n",
        encoding="utf-8",
    )
    fixture_server = tmp_path / "fixture_attach_server.py"
    fixture_server.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "from tiancheng_mcp.agent_sources import AgentSourcePolicy\n"
        "from tiancheng_mcp.agents import AgentProfileRegistry\n"
        "from tiancheng_mcp.policy import AccessPolicy\n"
        "from tiancheng_mcp.server import create_server\n"
        "from tiancheng_mcp.service import TianChengService\n"
        "workspace = Path(sys.argv[1])\n"
        "codex_root = Path(sys.argv[2])\n"
        "fake_agent = Path(sys.argv[3])\n"
        "state = Path(sys.argv[4])\n"
        "policy = AgentSourcePolicy.from_payload({'schema_version': 1, 'sources': [{"
        "'source_id': 'src_stdio_attach', 'provider': 'codex', 'root': str(codex_root), "
        "'mode': 'catalog-read'}]})\n"
        "service = TianChengService(workspace, state / 'audit', allow_exec=True, "
        "access_policy=AccessPolicy.default(workspace), agent_source_policy=policy, "
        "agent_catalog_path=state / 'catalog.sqlite3')\n"
        "service._exec_commands['codex'] = [sys.executable, str(fake_agent)]\n"
        "service.agent_profiles = AgentProfileRegistry(['codex'])\n"
        "service.agent_catalog_refresh('src_stdio_attach')\n"
        "create_server(service).run(transport='stdio')\n",
        encoding="utf-8",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            str(fixture_server),
            str(workspace),
            str(codex_root),
            str(fake_agent),
            str(tmp_path / "fixture-state"),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    async with Client(parameters, mode="legacy", raise_exceptions=True) as client:
        page = _structured(
            await client.call_tool(
                "agent_catalog", {"action": "list", "provider": "codex"}
            )
        )
        assert page["count"] == 1
        assert page["conversations"][0]["attachable"] is True
        attached = _structured(
            await client.call_tool(
                "agent_session",
                {
                    "action": "attach",
                    "conversation_ref": page["conversations"][0][
                        "conversation_ref"
                    ],
                },
            )
        )
        assert attached["native_session_id"] == "thr_stdio_attach"
        started = _structured(
            await client.call_tool(
                "agent_run",
                {
                    "action": "start",
                    "session_id": attached["session_id"],
                    "prompt": "continue",
                },
            )
        )
        inspected: dict[str, object] = {}
        for _ in range(100):
            inspected = _structured(
                await client.call_tool(
                    "agent_run",
                    {
                        "action": "inspect",
                        "session_id": attached["session_id"],
                        "run_id": started["run_id"],
                    },
                )
            )
            if inspected["state"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.05)
        assert inspected["state"] == "succeeded"
        result = _structured(
            await client.call_tool(
                "agent_run",
                {
                    "action": "result",
                    "session_id": attached["session_id"],
                    "run_id": started["run_id"],
                },
            )
        )
        assert result["result"] == "attached ok"


@pytest.mark.asyncio
async def test_external_access_description_matches_challenge_flow(
    workspace: Path, tmp_path: Path
) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "tiancheng_mcp",
            "--workspace",
            str(workspace),
            "--audit-dir",
            str(tmp_path / "grants-audit"),
            "--allow-external-grants",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        encoding="utf-8",
    )
    async with Client(parameters, mode="legacy", raise_exceptions=True) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        description = tools["request_external_access"].description or ""
        assert "six-digit TOTP" not in description
        assert "one-time challenge" in description
        assert "confirmation='批准'" in description


@pytest.mark.asyncio
async def test_static_policy_external_tools_accept_absolute_paths(
    workspace: Path, tmp_path: Path
) -> None:
    external = tmp_path / "approved"
    external.mkdir()
    policy = tmp_path / "access-policy.json"
    policy.write_text(
        json.dumps(
            {
                "rules": [
                    {"path": str(workspace), "mode": "full"},
                    {"path": str(external), "mode": "write", "require_approval": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "tiancheng_mcp",
            "--workspace",
            str(workspace),
            "--audit-dir",
            str(tmp_path / "policy-audit"),
            "--allow-external-grants",
            "--access-policy",
            str(policy),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        encoding="utf-8",
    )
    async with Client(parameters, mode="legacy", raise_exceptions=True) as client:
        written = _structured(
            await client.call_tool(
                "external_write_text",
                {"path": str(external / "直接.txt"), "content": "白名单"},
            )
        )
        read = _structured(
            await client.call_tool("external_read_text", {"path": str(external / "直接.txt")})
        )
        assert written["path"] == "直接.txt"
        assert read["content"] == "白名单"


@pytest.mark.asyncio
async def test_static_policy_read_only_external_stdio_smoke(
    workspace: Path, tmp_path: Path
) -> None:
    external = tmp_path / "readonly"
    external.mkdir()
    (external / "notes.md").write_text("白名单搜索词\n", encoding="utf-8")
    policy = tmp_path / "access-policy.json"
    policy.write_text(
        json.dumps(
            {
                "rules": [
                    {"path": str(workspace), "mode": "full"},
                    {"path": str(external), "mode": "read", "require_approval": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "tiancheng_mcp", "--workspace", str(workspace),
            "--audit-dir", str(tmp_path / "readonly-audit"),
            "--allow-external-grants", "--access-policy", str(policy),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        encoding="utf-8",
    )
    async with Client(parameters, mode="legacy", raise_exceptions=True) as client:
        listed = _structured(await client.call_tool("external_list_dir", {"path": str(external)}))
        assert any(item["path"] == "notes.md" for item in listed["entries"])
        read = _structured(await client.call_tool("external_read_text", {"path": str(external / "notes.md")}))
        assert "白名单搜索词" in read["content"]
        found = _structured(
            await client.call_tool(
                "external_search_text",
                {"query": "白名单搜索词", "base_path": str(external)},
            )
        )
        assert found["results"]
        denied = await client.call_tool(
            "external_write_text", {"path": str(external / "blocked.txt"), "content": "x"}
        )
        assert denied.is_error is True
