"""Real stdio acceptance for the local agent session/run workflow.

Drives run-mcp-exec.ps1 over real stdio JSON-RPC and verifies every agent
claim against an independent MCP read_text of the artifact it says it wrote.
No fake runtime, no marker-only assertions, no trust in the model's own
report of success.

This sends real prompts to the local Codex and Claude CLIs and therefore
consumes real model quota. It is deliberately not part of `pytest`; run it by
hand when the agent runtime changes:

    uv run python .\\scripts\\accept_agent_stdio.py
    uv run python .\\scripts\\accept_agent_stdio.py --providers codex-default
    uv run python .\\scripts\\accept_agent_stdio.py --providers "" --keep

Exits non-zero when any check fails. The JSON report names real session ids,
so it defaults to the git-ignored .tmp directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
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

REPO = Path(__file__).resolve().parents[1]
POWERSHELL = None  # resolved lazily by _powershell()
# .tmp/ is git-ignored; the report names real session ids and must not be
# committed alongside the harness.
DEFAULT_REPORT = REPO / ".tmp" / "acceptance-result.json"

RUN_POLL_TIMEOUT = 300.0


def structured(result: object) -> dict:
    value = getattr(result, "structured_content", None)
    if not isinstance(value, dict):
        raise RuntimeError(f"tool returned no structured content: {result!r}")
    return value


def error_text(result: object) -> str:
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
    return " | ".join(parts)[:500] or repr(result)[:500]


class Acceptance:
    def __init__(self, client: Client, base: str) -> None:
        self.client = client
        self.base = base
        self.checks: list[dict] = []
        self.last_error: str | None = None

    def record(self, name: str, ok: bool, detail: object = None) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        flag = "PASS" if ok else "FAIL"
        suffix = "" if detail is None else f" :: {detail}"
        print(f"  [{flag}] {name}{suffix}", flush=True)
        return bool(ok)

    async def call(self, tool: str, args: dict) -> tuple[dict | None, bool]:
        result = await self.client.call_tool(tool, args)
        if bool(getattr(result, "is_error", False)):
            self.last_error = error_text(result)
            return None, True
        self.last_error = None
        return structured(result), False

    async def must(self, tool: str, args: dict) -> dict:
        payload, is_error = await self.call(tool, args)
        if is_error or payload is None:
            raise RuntimeError(f"{tool} failed: {self.last_error} :: args={args}")
        return payload

    async def wait_terminal(self, session_id: str, run_id: str) -> dict:
        deadline = time.monotonic() + RUN_POLL_TIMEOUT
        cursor = 0
        polls = 0
        while time.monotonic() < deadline:
            page = await self.must(
                "agent_run",
                {
                    "action": "events",
                    "session_id": session_id,
                    "run_id": run_id,
                    # Advance the cursor: wait_ms only blocks when nothing is
                    # newer than after_seq, so a pinned cursor hot-loops.
                    "after_seq": cursor,
                    "limit": 50,
                    "max_bytes": 16384,
                    "wait_ms": 10000,
                },
            )
            polls += 1
            cursor = max(cursor, int(page.get("next_seq") or 0))
            if page.get("state") not in {"queued", "running"}:
                break
        self.record(f"poll_count.{run_id[:12]}", polls <= 60, {"polls": polls})
        return await self.must(
            "agent_run",
            {"action": "inspect", "session_id": session_id, "run_id": run_id},
        )

    async def read_marker(self, relative: str) -> str | None:
        payload, is_error = await self.call("read_text", {"path": relative})
        if is_error or payload is None:
            return None
        return (payload.get("content") or "").strip()

    async def provider_chain(self, profile: str, tag: str) -> None:
        print(f"\n=== {profile} workspace-write chain ===", flush=True)
        workdir = f"{self.base}/{tag}"
        await self.must("mkdir", {"path": workdir, "parents": True})

        session = await self.must(
            "agent_session",
            {
                "action": "create",
                "profile": profile,
                "cwd": workdir,
                "sandbox": "workspace-write",
            },
        )
        sid = session["session_id"]
        self.record(f"{tag}.session_created", bool(sid), session.get("provider"))

        token1 = f"TQ-{tag.upper()}-ONE"
        run1 = await self.must(
            "agent_run",
            {
                "action": "start",
                "session_id": sid,
                "prompt": (
                    "Create a file named step1.txt in the current working directory. "
                    f"Its entire content must be exactly this one line: {token1}\n"
                    "Create no other file. Reply with DONE when the file exists."
                ),
            },
        )
        self.record(
            f"{tag}.start_returns_handle",
            bool(run1.get("run_id") and run1.get("process_id")),
            {"state": run1.get("state"), "run_id": run1.get("run_id")},
        )

        busy_payload, busy_error = await self.call(
            "agent_run",
            {"action": "start", "session_id": sid, "prompt": "second concurrent run"},
        )
        self.record(
            f"{tag}.concurrent_run_rejected",
            busy_error,
            "rejected" if busy_error else busy_payload,
        )

        final1 = await self.wait_terminal(sid, run1["run_id"])
        self.record(
            f"{tag}.run1_terminal_succeeded",
            final1.get("state") == "succeeded",
            {
                "state": final1.get("state"),
                "seconds": final1.get("runtime_seconds"),
                "error": final1.get("error"),
            },
        )

        content1 = await self.read_marker(f"{workdir}/step1.txt")
        self.record(
            f"{tag}.run1_file_verified_independently",
            content1 == token1,
            {"expected": token1, "actual": content1},
        )

        native1 = final1.get("native_session_id")
        self.record(f"{tag}.native_session_bound", bool(native1), native1)

        token2 = f"TQ-{tag.upper()}-TWO"
        run2 = await self.must(
            "agent_run",
            {
                "action": "start",
                "session_id": sid,
                "prompt": (
                    "Create a second file named step2.txt in the same directory. "
                    f"Its entire content must be exactly this one line: {token2}\n"
                    "Reply with DONE when the file exists."
                ),
            },
        )
        final2 = await self.wait_terminal(sid, run2["run_id"])
        self.record(
            f"{tag}.run2_terminal_succeeded",
            final2.get("state") == "succeeded",
            {"state": final2.get("state"), "error": final2.get("error")},
        )
        self.record(
            f"{tag}.resume_same_native_session",
            bool(native1) and final2.get("native_session_id") == native1,
            {"run1": native1, "run2": final2.get("native_session_id")},
        )
        content2 = await self.read_marker(f"{workdir}/step2.txt")
        self.record(
            f"{tag}.run2_file_verified_independently",
            content2 == token2,
            {"expected": token2, "actual": content2},
        )

        result2 = await self.must(
            "agent_run",
            {
                "action": "result",
                "session_id": sid,
                "run_id": run2["run_id"],
                "max_bytes": 4096,
            },
        )
        self.record(
            f"{tag}.result_isolated_per_run",
            result2.get("run_id") == run2["run_id"] and run2["run_id"] != run1["run_id"],
            {"has_result": result2.get("has_result")},
        )

        run3 = await self.must(
            "agent_run",
            {
                "action": "start",
                "session_id": sid,
                "prompt": (
                    "Enumerate every prime number below 5000 and explain each primality "
                    "proof in full prose detail, one paragraph per prime."
                ),
            },
        )
        cancelled = await self.must(
            "agent_run",
            {
                "action": "cancel",
                "session_id": sid,
                "run_id": run3["run_id"],
                "reason": "acceptance cancel probe",
            },
        )
        self.record(
            f"{tag}.cancel_terminal",
            cancelled.get("state") == "cancelled" or cancelled.get("already_finished"),
            {
                "state": cancelled.get("state"),
                "already_finished": cancelled.get("already_finished"),
            },
        )

        closed = await self.must("agent_session", {"action": "close", "session_id": sid})
        self.record(
            f"{tag}.session_closed_no_active_runs",
            bool(closed.get("closed")) and closed.get("active_run_count") == 0,
            {"active": closed.get("active_run_count")},
        )

    async def readonly_probe(self, profile: str, tag: str) -> None:
        print(f"\n=== {profile} read-only probe ===", flush=True)
        workdir = f"{self.base}/{tag}-ro"
        await self.must("mkdir", {"path": workdir, "parents": True})
        session = await self.must(
            "agent_session",
            {
                "action": "create",
                "profile": profile,
                "cwd": workdir,
                "sandbox": "read-only",
            },
        )
        sid = session["session_id"]
        token = f"TQ-{tag.upper()}-RO"
        run = await self.must(
            "agent_run",
            {
                "action": "start",
                "session_id": sid,
                "prompt": (
                    f"Create a file named blocked.txt containing exactly: {token}\n"
                    "If you are not permitted to write files, reply REFUSED and stop."
                ),
            },
        )
        await self.wait_terminal(sid, run["run_id"])
        content = await self.read_marker(f"{workdir}/blocked.txt")
        self.record(f"{tag}.readonly_write_blocked", content is None, {"actual": content})
        info = await self.must("workspace_info", {})
        self.record(
            f"{tag}.server_responsive_after_refusal",
            info.get("server_version") is not None,
            info.get("server_version"),
        )
        await self.must("agent_session", {"action": "close", "session_id": sid})


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", default="codex-default,claude-default")
    parser.add_argument("--skip-readonly", action="store_true")
    parser.add_argument("--keep", action="store_true", help="skip trash cleanup")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    parameters = StdioServerParameters(
        command=str(_powershell()),
        args=[
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO / "run-mcp-exec.ps1"),
        ],
        cwd=str(REPO),
        encoding="utf-8",
        # The SDK gives the child a minimal environment, so the workspace
        # and any other TIANCHENG_* settings would not reach the server.
        env=dict(os.environ),
    )

    base = f"accept-{time.strftime('%Y%m%dT%H%M%S')}"
    started = time.monotonic()
    async with Client(parameters, mode="legacy", raise_exceptions=False) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        acc = Acceptance(client, base)
        print("=== tool surface ===", flush=True)
        acc.record(
            "unified_agent_tools_present",
            {"agent_session", "agent_run", "agent_catalog"} <= names,
            sorted(n for n in names if n.startswith("agent")),
        )
        acc.record(
            "no_per_vendor_agent_tools",
            not any(n.startswith(("codex_", "claude_")) for n in names),
            None,
        )
        info = structured(await client.call_tool("workspace_info", {}))
        profiles = info.get("available_agent_profiles", [])
        acc.record(
            "both_profiles_registered",
            {"codex-default", "claude-default"} <= set(profiles),
            profiles,
        )

        await acc.must("mkdir", {"path": base, "parents": True})
        for profile in [p.strip() for p in args.providers.split(",") if p.strip()]:
            tag = "codex" if "codex" in profile else "claude"
            try:
                await acc.provider_chain(profile, tag)
            except Exception as exc:
                acc.record(f"{tag}.chain_exception", False, repr(exc)[:400])
            if not args.skip_readonly:
                try:
                    await acc.readonly_probe(profile, tag)
                except Exception as exc:
                    acc.record(f"{tag}.readonly_exception", False, repr(exc)[:400])

        if not args.keep:
            print("\n=== cleanup ===", flush=True)
            deleted, del_err = await acc.call("delete", {"path": base})
            acc.record(
                "testdir_deleted_to_trash", not del_err, (deleted or {}).get("trash_path")
            )
            _, gone_err = await acc.call("stat", {"path": base})
            acc.record("testdir_gone_from_original_path", gone_err, None)
            trash = await acc.must("trash_list", {"max_results": 50})
            blob = json.dumps(trash, ensure_ascii=False)
            acc.record("trash_entry_present", base in blob, trash.get("count"))

        total = len(acc.checks)
        failed = [c for c in acc.checks if not c["ok"]]
        elapsed = time.monotonic() - started
        print(
            f"\n=== summary === {total - len(failed)}/{total} passed in {elapsed:.1f}s",
            flush=True,
        )
        for item in failed:
            print(f"  FAILED: {item['check']} :: {item['detail']}", flush=True)
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(acc.checks, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report: {out}", flush=True)
        if failed:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
