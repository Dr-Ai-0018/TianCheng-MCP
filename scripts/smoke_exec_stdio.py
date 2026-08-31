"""Real stdio smoke for the guarded Exec profile."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from mcp import Client, StdioServerParameters


def _powershell() -> Path:
    """Locate pwsh on PATH, or accept an explicit override.

    Nothing here may point at one particular machine's install: a clone has to
    work wherever PowerShell 7 happens to live.
    """

    override = os.environ.get("TIANCHENG_POWERSHELL")
    if override:
        return Path(override)
    found = shutil.which("pwsh")
    if not found:
        raise SystemExit(
            "pwsh (PowerShell 7) was not found on PATH. Install it, or set "
            "TIANCHENG_POWERSHELL to its full path."
        )
    return Path(found)


def _workspace() -> Path:
    """Return the workspace this smoke run may touch."""

    value = os.environ.get("TIANCHENG_WORKSPACE")
    if not value:
        raise SystemExit(
            "Set TIANCHENG_WORKSPACE to the directory this smoke run may use. "
            "It has no default: the workspace is the security boundary."
        )
    return Path(value)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = None  # resolved lazily by _powershell()


def structured(result: object) -> dict:
    value = getattr(result, "structured_content", None)
    if not isinstance(value, dict):
        raise RuntimeError("MCP tool did not return structured content")
    return value


async def smoke() -> None:
    parameters = StdioServerParameters(
        command=str(_powershell()),
        args=[
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROJECT_ROOT / "run-mcp-exec.ps1"),
        ],
        cwd=str(PROJECT_ROOT),
        encoding="utf-8",
        # The SDK gives the child a minimal environment, so the workspace
        # and any other TIANCHENG_* settings would not reach the server.
        env=dict(os.environ),
    )
    async with Client(parameters, mode="legacy", raise_exceptions=False) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        info = structured(await client.call_tool("workspace_info", {}))
        version = structured(
            await client.call_tool(
                "run_command",
                {
                    "command": "python",
                    "args": ["--version"],
                    "timeout_seconds": 10,
                    "max_output_bytes": 4096,
                },
            )
        )
        credential_command_blocked = None
        if "git" in info["available_exec_commands"]:
            credential_command_blocked = bool(
                getattr(
                    await client.call_tool(
                        "run_command", {"command": "git", "args": ["credential", "fill"]}
                    ),
                    "is_error",
                    False,
                )
            )
        summary = {
            "initialize_server": client.server_info.name if client.server_info else None,
            "tool_count": len(names),
            "run_command_registered": "run_command" in names,
            "exec_policy": info["command_execution_policy"],
            "python_version_exit_code": version["exit_code"],
            "credential_command_blocked": credential_command_blocked,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        if not all(
            (
                summary["initialize_server"] == "tiancheng-local-mcp",
                summary["tool_count"] >= 37,
                summary["run_command_registered"] is True,
                summary["exec_policy"] == "guarded-development",
                summary["python_version_exit_code"] == 0,
                summary["credential_command_blocked"] in {True, None},
            )
        ):
            raise SystemExit("Exec smoke validation failed")


if __name__ == "__main__":
    asyncio.run(smoke())
