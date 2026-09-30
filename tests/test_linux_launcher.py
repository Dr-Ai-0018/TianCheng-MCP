from __future__ import annotations

import os
from pathlib import Path
import sys
import asyncio

import pytest
from mcp import Client, StdioServerParameters


@pytest.mark.skipif(os.name == "nt", reason="Linux shell launcher")
@pytest.mark.asyncio
async def test_linux_shell_launcher_serves_safe_stdio(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parameters = StdioServerParameters(
        command="/bin/bash",
        env={"TIANCHENG_PYTHON": sys.executable, "PATH": os.environ.get("PATH", "")},
        args=[
            str(root / "run-mcp.sh"),
            "--workspace", str(workspace),
            "--audit-dir", str(tmp_path / "audit"),
            "--access-policy", str(tmp_path / "access-policy.json"),
            "--agent-sources", str(tmp_path / "agent-sources.json"),
            "--agent-catalog", str(tmp_path / "catalog.sqlite3"),
            "--agent-profiles", str(tmp_path / "agent-profiles.json"),
            "--agent-env-file", str(tmp_path / "agent.env"),
            "--launcher-local-config", str(tmp_path / "launcher.json"),
        ],
        cwd=str(root),
        encoding="utf-8",
    )
    async with Client(parameters, mode="legacy", raise_exceptions=True) as client:
        assert client.server_info is not None
        assert client.server_info.name == "tiancheng-local-mcp"
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert "read_text" in tools
        assert "run_command" not in tools


@pytest.mark.skipif(os.name == "nt", reason="Linux shell launcher")
@pytest.mark.asyncio
async def test_linux_shell_launcher_executes_opt_in_dev_command(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parameters = StdioServerParameters(
        command="/bin/bash",
        env={"TIANCHENG_PYTHON": sys.executable, "PATH": os.environ.get("PATH", "")},
        args=[
            str(root / "run-mcp.sh"),
            "--workspace", str(workspace),
            "--audit-dir", str(tmp_path / "audit"),
            "--access-policy", str(tmp_path / "access-policy.json"),
            "--agent-sources", str(tmp_path / "agent-sources.json"),
            "--agent-catalog", str(tmp_path / "catalog.sqlite3"),
            "--agent-profiles", str(tmp_path / "agent-profiles.json"),
            "--agent-env-file", str(tmp_path / "agent.env"),
            "--launcher-local-config", str(tmp_path / "launcher.json"),
            "--allow-exec",
        ],
        cwd=str(root),
        encoding="utf-8",
    )
    async with Client(parameters, mode="legacy", raise_exceptions=True) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert "run_command" in tools
        result = await client.call_tool(
            "run_command",
            {"command": "python", "args": ["-c", "print('LINUX_DEV_STDIO_OK')"]},
        )
        assert result.is_error is False
        value = result.structured_content
        assert isinstance(value, dict)
        assert value["exit_code"] == 0
        assert "LINUX_DEV_STDIO_OK" in value["stdout"]


@pytest.mark.skipif(os.name == "nt", reason="Linux shell launcher")
@pytest.mark.asyncio
async def test_linux_stdio_stop_process_kills_child_tree(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parameters = StdioServerParameters(
        command="/bin/bash",
        env={"TIANCHENG_PYTHON": sys.executable, "PATH": os.environ.get("PATH", "")},
        args=[
            str(root / "run-mcp.sh"),
            "--workspace", str(workspace),
            "--audit-dir", str(tmp_path / "audit"),
            "--access-policy", str(tmp_path / "access-policy.json"),
            "--agent-sources", str(tmp_path / "agent-sources.json"),
            "--agent-catalog", str(tmp_path / "catalog.sqlite3"),
            "--agent-profiles", str(tmp_path / "agent-profiles.json"),
            "--agent-env-file", str(tmp_path / "agent.env"),
            "--launcher-local-config", str(tmp_path / "launcher.json"),
            "--allow-exec",
        ],
        cwd=str(root),
        encoding="utf-8",
    )
    child_code = (
        "import pathlib,time; time.sleep(2); "
        "pathlib.Path('child-survived.txt').write_text('bad', encoding='utf-8')"
    )
    parent_code = (
        "import pathlib,subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "pathlib.Path('parent-spawned.txt').write_text('ok', encoding='utf-8'); "
        "time.sleep(30)"
    )
    async with Client(parameters, mode="legacy", raise_exceptions=True) as client:
        started = await client.call_tool(
            "start_process",
            {"command": "python", "args": ["-c", parent_code], "max_runtime_seconds": 40},
        )
        assert started.is_error is False
        value = started.structured_content
        assert isinstance(value, dict)
        process_id = value["process_id"]
        for _ in range(40):
            if (workspace / "parent-spawned.txt").exists():
                break
            await asyncio.sleep(0.05)
        assert (workspace / "parent-spawned.txt").exists()
        stopped = await client.call_tool("stop_process", {"process_id": process_id, "force": True})
        assert stopped.is_error is False
        await asyncio.sleep(2.2)
        assert not (workspace / "child-survived.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="Linux shell launcher")
@pytest.mark.asyncio
async def test_linux_stdio_job_cancel_kills_child_tree(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parameters = StdioServerParameters(
        command="/bin/bash",
        env={"TIANCHENG_PYTHON": sys.executable, "PATH": os.environ.get("PATH", "")},
        args=[
            str(root / "run-mcp.sh"),
            "--workspace", str(workspace),
            "--audit-dir", str(tmp_path / "audit"),
            "--access-policy", str(tmp_path / "access-policy.json"),
            "--agent-sources", str(tmp_path / "agent-sources.json"),
            "--agent-catalog", str(tmp_path / "catalog.sqlite3"),
            "--agent-profiles", str(tmp_path / "agent-profiles.json"),
            "--agent-env-file", str(tmp_path / "agent.env"),
            "--launcher-local-config", str(tmp_path / "launcher.json"),
            "--allow-exec",
            "--interactive-timeout-seconds", "1",
        ],
        cwd=str(root),
        encoding="utf-8",
    )
    child_code = (
        "import pathlib,time; time.sleep(2); "
        "pathlib.Path('job-child-survived.txt').write_text('bad', encoding='utf-8')"
    )
    parent_code = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(30)"
    )
    async with Client(parameters, mode="legacy", raise_exceptions=True) as client:
        response = await client.call_tool(
            "run_command",
            {"command": "python", "args": ["-c", parent_code], "timeout_seconds": 40},
        )
        assert response.is_error is False
        value = response.structured_content
        assert isinstance(value, dict)
        assert value["execution"] == "background"
        job_id = value["job_id"]
        cancelled = await client.call_tool("job_cancel", {"job_id": job_id})
        assert cancelled.is_error is False
        state = ""
        for _ in range(40):
            status = await client.call_tool("job_status", {"job_id": job_id})
            assert status.is_error is False
            detail = status.structured_content
            assert isinstance(detail, dict)
            state = detail["state"]
            if state == "cancelled":
                break
            await asyncio.sleep(0.05)
        assert state == "cancelled"
        await asyncio.sleep(2.2)
        assert not (workspace / "job-child-survived.txt").exists()
