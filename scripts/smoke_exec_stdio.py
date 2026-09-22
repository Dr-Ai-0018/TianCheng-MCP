"""Real stdio smoke for the guarded Exec profile."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from mcp import Client, StdioServerParameters

from local_runtime import powershell_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = powershell_path(PROJECT_ROOT)


def structured(result: object) -> dict:
    value = getattr(result, "structured_content", None)
    if not isinstance(value, dict):
        raise RuntimeError("MCP tool did not return structured content")
    return value


async def smoke() -> None:
    parameters = StdioServerParameters(
        command=str(POWERSHELL),
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
