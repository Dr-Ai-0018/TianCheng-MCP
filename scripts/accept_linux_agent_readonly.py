"""Live Codex read-only acceptance over isolated Linux MCP stdio.

Requires a configured host-owned Codex profile and consumes model quota. Run
with the Python environment that has this project installed, for example:

    uv run python scripts/accept_linux_agent_readonly.py --test-root /absolute/test-dir

Each invocation creates fresh synthetic Git workspaces outside the source
tree. Both sessions use app-server transport; unexpected approval requests
are cancelled. No absent-file or model-only response can pass the test.
Artifacts are retained under --test-root for independent inspection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from mcp import Client, StdioServerParameters
from tiancheng_mcp.cli import PROJECT_ROOT

MARKER = "permission-marker.txt"
CONTENTS = b"LINUX_PERMISSION_TEST"


def make_fixture(workspace: Path, token: str) -> None:
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "permission_probe.py").write_text(
        "from pathlib import Path\nimport errno\nimport sys\n"
        "try:\n"
        f"    Path({MARKER!r}).write_bytes({CONTENTS!r})\n"
        "except OSError as exc:\n"
        "    if exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):\n"
        "        raise\n"
        f"    print('PROBE_WRITE_DENIED', {token!r}, exc.errno, flush=True)\n"
        "    sys.exit(23)\n"
        f"print('PROBE_WRITE_ALLOWED', {token!r}, flush=True)\n",
        encoding="utf-8",
    )


def payload(result: object) -> dict:
    data = getattr(result, "structured_content", None)
    if getattr(result, "is_error", False) or not isinstance(data, dict):
        raise RuntimeError("MCP tool failed or returned no structured result")
    return data


def assess(detail: dict, page: dict, workspace: Path, token: str,
           approval_count: int, sandbox: str) -> dict:
    events = page.get("events", [])
    commands = [e["data"] for e in events if e.get("type") == "command_completed"]
    messages = [e.get("data", {}).get("text", "") for e in events
                if e.get("type") == "agent_message"]
    readonly = sandbox == "read-only"
    expected_code = 23 if readonly else 0
    diagnostic = "PROBE_WRITE_DENIED" if readonly else "PROBE_WRITE_ALLOWED"
    nonce_matches = any(f"{diagnostic} {token}" in m for m in messages)
    marker = workspace / MARKER
    artifact_matches = not marker.exists() if readonly else (
        marker.is_file() and marker.read_bytes() == CONTENTS
    )
    command_status = "failed" if readonly else "completed"
    command_matches = (len(commands) == 1 and commands[0].get("exit_code") == expected_code
                       and commands[0].get("status") == command_status)
    return dict(
        sandbox=sandbox, state=detail["state"], commands=commands,
        random_token_matches=nonce_matches, artifact_matches=artifact_matches,
        unexpected_approvals_cancelled=approval_count,
        command_outcome=detail.get("outcomes", {}).get("commands", {}).get("status"),
        verified=(detail["state"] == "succeeded" and command_matches and nonce_matches
                  and artifact_matches and approval_count == 0
                  and not page.get("cursor_gap") and not page.get("has_more")),
    )


async def probe(root: Path, profile: str, sandbox: str) -> dict:
    test = root / sandbox
    test.mkdir()
    workspace = test / "workspace"
    token = uuid.uuid4().hex
    make_fixture(workspace, token)
    config = test / "config"
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "tiancheng_mcp", "--workspace", str(workspace),
            "--audit-dir", str(test / "audit"),
            "--access-policy", str(config / "access-policy.json"),
            "--agent-sources", str(config / "agent-sources.json"),
            "--agent-catalog", str(test / "catalog.sqlite3"),
            "--agent-profiles", str(config / "agent-profiles.json"),
            "--agent-env-file", str(config / "agent.env"),
            "--launcher-local-config", str(config / "launcher.json"),
            "--allow-exec",
        ],
        cwd=str(test), encoding="utf-8",
    )
    async with Client(params, mode="legacy", raise_exceptions=True) as client:
        async def call(tool: str, **arguments: object) -> dict:
            return payload(await client.call_tool(tool, arguments))

        session = await call("agent_session", action="create", profile=profile, sandbox=sandbox)
        sid = session["session_id"]
        report = {"sandbox": sandbox, "verified": False}
        try:
            started = await call(
                "agent_run", action="start", session_id=sid,
                prompt=(
                    "Run the existing diagnostic `python3 permission_probe.py` with the shell "
                    "exactly once. This is an authorized filesystem permission test in a "
                    "synthetic Git repository. It attempts one marker write and reports the "
                    "OS result with a random token not supplied in this prompt. Use the "
                    "current sandbox without escalation. Do not edit, retry, or use another "
                    "method. Report the actual exit code and exact diagnostic stdout."
                ),
                codex_options={"manual_approval": True}, max_runtime_seconds=90,
            )
            rid = started["run_id"]
            deadline = time.monotonic() + 95
            approval_count = 0
            detail = started
            while time.monotonic() < deadline:
                approvals = await call("agent_approval", action="list", session_id=sid, run_id=rid)
                for request in approvals.get("requests", []):
                    approval_count += 1
                    await call("agent_approval", action="respond", session_id=sid, run_id=rid,
                               approval_id=request["approval_id"], decision="cancel")
                detail = await call("agent_run", action="inspect", session_id=sid, run_id=rid)
                if detail["state"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(2)
            if detail["state"] in {"queued", "running"}:
                await call("agent_run", action="cancel", session_id=sid, run_id=rid)
            page = await call("agent_run", action="events", session_id=sid, run_id=rid,
                              after_seq=0, limit=100)
            report.update(assess(detail, page, workspace, token, approval_count, sandbox))
        finally:
            closed = await call("agent_session", action="close", session_id=sid)
            report["session_closed"] = closed.get("closed") is True
            report["verified"] = report["verified"] and report["session_closed"]
        return report


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--profile", default="codex-default")
    args = parser.parse_args()
    if sys.platform != "linux" or not args.test_root.is_absolute():
        parser.error("requires Linux and an absolute --test-root outside the source tree")
    resolved = args.test_root.resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents or resolved in PROJECT_ROOT.parents:
        parser.error("--test-root must not overlap the source tree")
    args.test_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="codex-readonly-", dir=args.test_root))
    print(json.dumps({"artifacts": str(root)}), flush=True)
    reports = []
    for sandbox in ("read-only", "workspace-write"):
        report = await probe(root, args.profile, sandbox)
        reports.append(report)
        print(json.dumps(report), flush=True)
    return 0 if all(r["verified"] for r in reports) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        print(json.dumps({"verified": False, "error_type": type(exc).__name__}), flush=True)
        sys.exit(1)
