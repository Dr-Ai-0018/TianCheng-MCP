"""Real stdio smoke against run-mcp.ps1 and the configured workspace."""

from __future__ import annotations

import asyncio
import json
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
POWERSHELL = None  # resolved lazily by _powershell()


def structured(result: object) -> dict:
    value = getattr(result, "structured_content", None)
    if not isinstance(value, dict):
        content = getattr(result, "content", None) or []
        details = []
        for item in content:
            text = getattr(item, "text", None)
            if text:
                details.append(str(text))
        suffix = ": " + " | ".join(details)[:500] if details else ""
        raise RuntimeError("MCP tool did not return structured content" + suffix)
    return value


async def completed(client: Client, result: object) -> dict:
    """Normalize an inline result or the automatic background-job fallback."""

    value = structured(result)
    if value.get("execution") != "background":
        return value
    job_id = value.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("Background response did not include a job_id")
    for _ in range(300):
        status = structured(await client.call_tool("job_status", {"job_id": job_id}))
        if status.get("state") in {"succeeded", "failed", "cancelled", "expired"}:
            result_payload = structured(
                await client.call_tool("job_result", {"job_id": job_id})
            )
            if result_payload.get("ready") and "result" in result_payload:
                nested = result_payload["result"]
                if isinstance(nested, dict):
                    return nested
            raise RuntimeError(f"Background job did not succeed: {result_payload}")
        await asyncio.sleep(0.1)
    raise RuntimeError(f"Background job did not finish: {job_id}")


async def smoke() -> None:
    name = f"mcp-smoke-{uuid.uuid4().hex[:8]}.txt"
    expected = "天成 MCP stdio smoke\n"
    parameters = StdioServerParameters(
        command=str(_powershell()),
        args=[
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PROJECT_ROOT / "run-mcp.ps1"),
        ],
        cwd=str(PROJECT_ROOT),
        encoding="utf-8",
        # The SDK gives the child a minimal environment, so the workspace
        # and any other TIANCHENG_* settings would not reach the server.
        env=dict(os.environ),
    )
    async with Client(parameters, mode="legacy", raise_exceptions=False) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        written = await completed(
            client, await client.call_tool("write_text", {"path": name, "content": expected})
        )
        read = await completed(client, await client.call_tool("read_text", {"path": name}))
        deleted = await completed(client, await client.call_tool("delete", {"path": name}))
        escape = await client.call_tool("read_text", {"path": r"..\outside.txt"})
        trash_path = _workspace() / Path(deleted["trash_path"])
        summary = {
            "initialize_server": client.server_info.name if client.server_info else None,
                "tool_count": len(names),
            "run_command_registered": "run_command" in names,
            "created_path": written["path"],
            "read_matches": read["content"] == expected,
            "trash_path": deleted["trash_path"],
            "trash_exists": trash_path.exists(),
            "escape_rejected": bool(getattr(escape, "is_error", False)),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        if not all(
            (
                summary["initialize_server"] == "tiancheng-local-mcp",
                summary["tool_count"] == 31,
                summary["run_command_registered"] is False,
                summary["read_matches"] is True,
                summary["trash_exists"] is True,
                summary["escape_rejected"] is True,
            )
        ):
            raise SystemExit("Smoke validation failed")


if __name__ == "__main__":
    asyncio.run(smoke())
