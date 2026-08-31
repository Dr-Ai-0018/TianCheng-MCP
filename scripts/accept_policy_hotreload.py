"""Real stdio acceptance for the browse tier, whitelisted agent cwd, and
chat-approved access-policy hot reload.

Runs against a throwaway access-policy file so the machine's real whitelist is
never modified, but everything else is real: real stdio JSON-RPC, real tool
routing, real Codex/Claude processes, and every artifact claim verified by an
independent MCP read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
WORKSPACE = None  # resolved lazily by _workspace()
RUN_POLL_TIMEOUT = 300.0


def _allowlisted_env_names() -> list[str]:
    path = REPO / "exec-env.allowlist"
    if not path.exists():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            names.append(value)
    return names


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
    return " | ".join(parts)[:400] or repr(result)[:400]


class Checks:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.items: list[dict] = []
        self.last_error: str | None = None

    def record(self, name: str, ok: bool, detail: object = None) -> bool:
        self.items.append({"check": name, "ok": bool(ok), "detail": detail})
        suffix = "" if detail is None else f" :: {detail}"
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}", flush=True)
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
            raise RuntimeError(f"{tool} failed: {self.last_error}")
        return payload

    async def wait_terminal(self, session_id: str, run_id: str) -> dict:
        deadline = time.monotonic() + RUN_POLL_TIMEOUT
        cursor = 0
        while time.monotonic() < deadline:
            page = await self.must(
                "agent_run",
                {
                    "action": "events",
                    "session_id": session_id,
                    "run_id": run_id,
                    "after_seq": cursor,
                    "limit": 50,
                    "max_bytes": 16384,
                    "wait_ms": 10000,
                },
            )
            cursor = max(cursor, int(page.get("next_seq") or 0))
            if page.get("state") not in {"queued", "running"}:
                break
        return await self.must(
            "agent_run", {"action": "inspect", "session_id": session_id, "run_id": run_id}
        )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--providers", default="claude-default,codex-default")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    # A plain directory on a normal drive, deliberately not tempfile.mkdtemp:
    # on Windows mkdtemp applies a restrictive owner-only DACL, and files the
    # agent then writes inherit it and become unreadable even to the same
    # user. That is an artifact of the test staging, not of the server or the
    # agent, and it would otherwise look like a lost write.
    import uuid as _uuid
    sandbox = Path(tempfile.gettempdir()) / f"tc-accept-hot-{_uuid.uuid4().hex[:8]}"
    sandbox.mkdir()
    # Keep the throwaway policy file in its own directory: the server refuses
    # to whitelist anything under the directory holding its policy, so a
    # project nested beside the policy file would be rejected by that guard
    # rather than exercising the flow.
    project = sandbox / "work" / "external-project"
    (project / "src").mkdir(parents=True)
    (project / "notes.md").write_text("existing content", encoding="utf-8")
    # Codex refuses to run outside a trusted directory unless
    # --skip-git-repo-check is passed, and the server deliberately does not
    # pass it. Real target directories are repositories, so make this one a
    # repository too rather than weakening the provider's own safety net.
    subprocess.run(
        ["git", "init", "--quiet"], cwd=str(project), check=True, capture_output=True
    )
    (sandbox / "policy").mkdir()
    policy_file = sandbox / "policy" / "access-policy.json"
    policy_file.write_text(
        json.dumps({"rules": [{"path": str(_workspace()), "mode": "full"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "tiancheng_mcp",
            "--workspace",
            str(_workspace()),
            "--audit-dir",
            str(REPO / "logs"),
            "--allow-exec",
            # The external_* file tools only register under this flag, so a
            # hot-reload profile needs it to actually use a newly whitelisted
            # directory for files. This mirrors the real GRANTS+EXEC profile.
            "--allow-external-grants",
            "--allow-policy-hot-reload",
            # run-mcp-*.ps1 loads these names from exec-env.allowlist; this
            # script bypasses the launcher, so pass them explicitly or the
            # codex-default profile starts without its configured key.
            *[arg for name in _allowlisted_env_names() for arg in ("--pass-env", name)],
            "--access-policy",
            str(policy_file),
            "--agent-sources",
            str(sandbox / "isolated-sources.json"),
            "--agent-catalog",
            str(sandbox / "isolated-catalog.sqlite3"),
        ],
        cwd=str(REPO),
        encoding="utf-8",
        # The SDK gives a child a minimal environment by default, so
        # --pass-env would have nothing to forward. run-mcp-*.ps1 loads the
        # allowlisted names into its own environment before starting the
        # server; mirror that here rather than reading .env separately.
        env=dict(os.environ),
    )

    started = time.monotonic()
    try:
        async with Client(parameters, mode="legacy", raise_exceptions=False) as client:
            c = Checks(client)
            names = {tool.name for tool in (await client.list_tools()).tools}

            print("=== mode surface ===", flush=True)
            c.record(
                "hot_reload_tools_registered",
                {
                    "access_policy_change_request",
                    "access_policy_change_confirm",
                    "access_policy_change_cancel",
                    "access_policy_change_status",
                }
                <= names,
                sorted(n for n in names if n.startswith("access_policy_change")),
            )
            info = await c.must("workspace_info", {})
            c.record(
                "reports_hot_mode", info.get("access_policy_reload_mode") == "hot",
                info.get("access_policy_reload_mode"),
            )

            print("\n=== before approval ===", flush=True)
            _, denied = await c.call(
                "agent_session",
                {
                    "action": "create",
                    "profile": "claude-default",
                    "cwd": str(project),
                    "sandbox": "workspace-write",
                },
            )
            c.record("agent_denied_before_whitelisting", denied, c.last_error)
            _, listed_denied = await c.call("external_list_dir", {"path": str(project)})
            c.record("listing_denied_before_whitelisting", listed_denied)

            print("\n=== self-escalation guards ===", flush=True)
            for label, target in (
                ("server_root", str(REPO)),
                ("policy_dir", str(REPO / "config")),
                ("drive_root", str(Path(sandbox.anchor))),
            ):
                _, refused = await c.call(
                    "access_policy_change_request",
                    {"paths": [target], "mode": "full"},
                )
                c.record(f"refuses_{label}", refused, c.last_error)

            print("\n=== browse tier ===", flush=True)
            staged = await c.must(
                "access_policy_change_request",
                {"paths": [str(project)], "mode": "browse"},
            )
            c.record("request_returns_challenge", bool(staged.get("challenge")))
            _, still_denied = await c.call("external_list_dir", {"path": str(project)})
            c.record("staging_grants_nothing", still_denied)

            _, wrong = await c.call(
                "access_policy_change_confirm",
                {
                    "request_id": staged["request_id"],
                    "challenge": staged["challenge"],
                    "confirmation": "yes",
                },
            )
            c.record("wrong_confirmation_refused", wrong)

            approved = await c.must(
                "access_policy_change_confirm",
                {
                    "request_id": staged["request_id"],
                    "challenge": staged["challenge"],
                    "confirmation": "批准",
                },
            )
            c.record("browse_effective_immediately", approved.get("effective_immediately") is True)

            top = await c.must("external_list_dir", {"path": str(project)})
            c.record(
                "browse_lists_one_level",
                {e["path"] for e in top["entries"]} == {".git", "notes.md", "src"},
                sorted(e["path"] for e in top["entries"]),
            )
            deep = await c.must("external_list_dir", {"path": str(project), "depth": 5})
            c.record(
                "browse_clamps_depth",
                {e["path"] for e in deep["entries"]} == {".git", "notes.md", "src"},
            )
            _, read_denied = await c.call(
                "external_read_text", {"path": str(project / "notes.md")}
            )
            c.record("browse_refuses_file_content", read_denied)
            _, agent_denied = await c.call(
                "agent_session",
                {
                    "action": "create",
                    "profile": "claude-default",
                    "cwd": str(project),
                    "sandbox": "read-only",
                },
            )
            c.record("browse_cannot_host_agent", agent_denied)

            print("\n=== promote to write ===", flush=True)
            staged = await c.must(
                "access_policy_change_request",
                {"paths": [str(project)], "mode": "write"},
            )
            await c.must(
                "access_policy_change_confirm",
                {
                    "request_id": staged["request_id"],
                    "challenge": staged["challenge"],
                    "confirmation": "批准",
                },
            )
            content = await c.must(
                "external_read_text", {"path": str(project / "notes.md")}
            )
            c.record(
                "write_rule_allows_reading",
                content.get("content", "").strip() == "existing content",
            )

            for profile in [p.strip() for p in args.providers.split(",") if p.strip()]:
                tag = "codex" if "codex" in profile else "claude"
                print(f"\n=== {profile} in whitelisted directory ===", flush=True)
                session = await c.must(
                    "agent_session",
                    {
                        "action": "create",
                        "profile": profile,
                        "cwd": str(project),
                        "sandbox": "workspace-write",
                    },
                )
                c.record(
                    f"{tag}.session_scope_is_policy",
                    session.get("cwd_scope") == "access-policy",
                    session.get("cwd_scope"),
                )
                token = f"TQ-HOT-{tag.upper()}"
                run = await c.must(
                    "agent_run",
                    {
                        "action": "start",
                        "session_id": session["session_id"],
                        "prompt": (
                            f"Create a file named {tag}-proof.txt in the current working "
                            f"directory whose entire content is exactly this one line: {token}\n"
                            "Reply DONE when the file exists."
                        ),
                    },
                )
                final = await c.wait_terminal(session["session_id"], run["run_id"])
                c.record(
                    f"{tag}.run_succeeded",
                    final.get("state") == "succeeded",
                    {"state": final.get("state"), "error": final.get("error")},
                )
                proof, proof_err = await c.call(
                    "external_read_text", {"path": str(project / f"{tag}-proof.txt")}
                )
                actual = None if proof_err else (proof or {}).get("content", "").strip()
                ok = c.record(
                    f"{tag}.file_written_outside_workspace_verified",
                    actual == token,
                    {"expected": token, "actual": actual},
                )
                if not ok:
                    said = await c.must(
                        "agent_run",
                        {
                            "action": "result",
                            "session_id": session["session_id"],
                            "run_id": run["run_id"],
                            "max_bytes": 2000,
                        },
                    )
                    print(f"    agent said: {said.get('result')!r}", flush=True)
                    listing, listing_error = await c.call(
                        "external_list_dir", {"path": str(project), "depth": 1}
                    )
                    if listing_error:
                        print(f"    listing failed: {c.last_error}", flush=True)
                    else:
                        print(
                            f"    dir now: {sorted(e['path'] for e in listing['entries'])}",
                            flush=True,
                        )
                    on_disk = sorted(x.name for x in project.iterdir())
                    print(f"    on disk (direct): {on_disk}", flush=True)
                # Prove it really is outside the jail: the workspace-relative
                # tool must not be able to reach it.
                _, jail_err = await c.call("read_text", {"path": f"{tag}-proof.txt"})
                c.record(f"{tag}.not_reachable_via_workspace_tool", jail_err)
                await c.must(
                    "agent_session", {"action": "close", "session_id": session["session_id"]}
                )

            print("\n=== narrowing revokes access ===", flush=True)
            staged = await c.must(
                "access_policy_change_request",
                {"paths": [str(project)], "mode": "browse"},
            )
            await c.must(
                "access_policy_change_confirm",
                {
                    "request_id": staged["request_id"],
                    "challenge": staged["challenge"],
                    "confirmation": "批准",
                },
            )
            _, revoked = await c.call(
                "agent_session",
                {
                    "action": "create",
                    "profile": "claude-default",
                    "cwd": str(project),
                    "sandbox": "workspace-write",
                },
            )
            c.record("narrowing_revokes_agent_immediately", revoked, c.last_error)
            _, revoked_read = await c.call(
                "external_read_text", {"path": str(project / "notes.md")}
            )
            c.record("narrowing_revokes_reads_immediately", revoked_read)

            total = len(c.items)
            failed = [i for i in c.items if not i["ok"]]
            print(
                f"\n=== summary === {total - len(failed)}/{total} passed in "
                f"{time.monotonic() - started:.1f}s",
                flush=True,
            )
            for item in failed:
                print(f"  FAILED: {item['check']} :: {item['detail']}", flush=True)
            print(f"\nthrowaway policy file used: {policy_file}", flush=True)
            if failed:
                raise SystemExit(1)
    finally:
        if args.keep:
            print(f'kept sandbox: {sandbox}', flush=True)
        else:
            shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
