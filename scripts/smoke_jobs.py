"""Stdio smoke covering automatic job fallback, polling, cancellation, and cleanup."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
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
WORKSPACE = None  # resolved lazily by _workspace()


def structured(result: object) -> dict:
    value = getattr(result, "structured_content", None)
    if not isinstance(value, dict):
        raise RuntimeError(f"MCP tool did not return structured content: {result!r}")
    return value


async def smoke() -> None:
    python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    parameters = StdioServerParameters(
        command=str(python),
        args=[
            "-m",
            "tiancheng_mcp",
            "--workspace",
            str(_workspace()),
            "--audit-dir",
            str(PROJECT_ROOT / "logs"),
            "--allow-exec",
            "--interactive-timeout-seconds",
            "1",
        ],
        cwd=str(PROJECT_ROOT),
        encoding="utf-8",
        # The SDK gives the child a minimal environment, so the workspace
        # and any other TIANCHENG_* settings would not reach the server.
        env=dict(os.environ),
    )
    key = f"smoke-{uuid.uuid4().hex}"
    validation_failed = False
    async with Client(parameters, mode="legacy", raise_exceptions=False) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        first = structured(
            await client.call_tool(
                "run_command",
                {
                    "command": "python",
                    "args": ["-c", "import time; time.sleep(30)"],
                    "timeout_seconds": 60,
                    "idempotency_key": key,
                },
            )
        )
        second = structured(
            await client.call_tool(
                "run_command",
                {
                    "command": "python",
                    "args": ["-c", "import time; time.sleep(30)"],
                    "timeout_seconds": 60,
                    "idempotency_key": key,
                },
            )
        )
        if first.get("execution") != "background":
            raise RuntimeError(f"Expected background response, got {first}")
        if second.get("job_id") != first.get("job_id"):
            raise RuntimeError("Idempotency retry did not reuse the original job")
        job_id = str(first["job_id"])
        status = structured(await client.call_tool("job_status", {"job_id": job_id}))
        cancelled = structured(
            await client.call_tool(
                "job_cancel", {"job_id": job_id, "reason": "stdio smoke cleanup"}
            )
        )
        result = structured(await client.call_tool("job_result", {"job_id": job_id}))
        for _ in range(100):
            if result.get("ready") is True:
                break
            await asyncio.sleep(0.1)
            result = structured(await client.call_tool("job_result", {"job_id": job_id}))
        summary = {
            "initialize_server": client.server_info.name if client.server_info else None,
            "tool_count": len(names),
            "background": first.get("execution") == "background",
            "idempotent_job_reused": second.get("job_id") == job_id,
            "status_seen": status.get("state") in {"queued", "running"},
            "cancel_accepted": cancelled.get("accepted") is True,
            "result_ready": result.get("ready") is True,
            "result_state": result.get("state"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        validation_failed = not all(
            (
                summary["initialize_server"] == "tiancheng-local-mcp",
                summary["background"],
                summary["idempotent_job_reused"],
                summary["status_seen"],
                summary["cancel_accepted"],
                summary["result_ready"],
                summary["result_state"] == "cancelled",
            )
        )
    if validation_failed:
        raise SystemExit("Job smoke validation failed")


if __name__ == "__main__":
    # Ensure imports work when called as `python scripts/smoke_jobs.py`.
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    asyncio.run(smoke())
