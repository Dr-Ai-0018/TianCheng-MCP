"""Local-only administration for provider discovery and agent source policy.

This module is invoked by ``tc.ps1``.  It is intentionally not registered as
an MCP tool: remote callers may query the resulting catalog but cannot create,
widen, or remove source policy entries.
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Callable

from .agent_adapters import redact_text
from .agent_catalog import AgentCatalog
from .agent_sources import AgentSourcePolicy
from .policy import AccessPolicy
from .service import TianChengService


_PROVIDER_DEFAULTS = {
    "codex": ("CODEX_HOME", ".codex", "sessions", "codex"),
    "claude-code": ("CLAUDE_CONFIG_DIR", ".claude", "projects", "claude"),
}
_SMOKE_PROFILES = frozenset({"codex-default", "claude-default"})
_SMOKE_MARKER = "TIANCHENG_SMOKE_OK"


class AgentSourceAdminError(ValueError):
    """Raised when a local-only source administration operation is invalid."""


def _probe_version(command_name: str) -> dict[str, Any]:
    executable = shutil.which(f"{command_name}.exe") or shutil.which(command_name)
    if not executable:
        return {"available": False, "version": None}
    try:
        completed = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            shell=False,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "version": None}
    line = next(
        (item.strip() for item in completed.stdout.splitlines() if item.strip()),
        "",
    )
    if completed.returncode != 0 or not line:
        return {"available": False, "version": None}
    encoded = line.encode("utf-8", errors="replace")[:200]
    return {
        "available": True,
        "version": encoded.decode("utf-8", errors="ignore"),
    }


def discover_local_agents(
    *,
    home: str | Path | None = None,
    environment: dict[str, str] | None = None,
    probe_versions: bool = True,
) -> dict[str, Any]:
    """Inspect only fixed provider roots; never scan transcript contents."""

    env = os.environ if environment is None else environment
    home_path = Path(home) if home is not None else Path.home()
    providers: list[dict[str, Any]] = []
    for provider, (env_name, default_dir, leaf, command) in _PROVIDER_DEFAULTS.items():
        configured_home = env.get(env_name)
        provider_home = (
            Path(configured_home).expanduser()
            if configured_home and configured_home.strip()
            else home_path / default_dir
        )
        source_root = provider_home / leaf
        probe = _probe_version(command) if probe_versions else {
            "available": bool(shutil.which(f"{command}.exe") or shutil.which(command)),
            "version": None,
        }
        providers.append(
            {
                "provider": provider,
                "display_name": "Claude Code" if provider == "claude-code" else "Codex",
                "cli_available": probe["available"],
                "cli_version": probe["version"],
                "suggested_root": str(source_root),
                "source_exists": source_root.is_dir(),
                "discovery": "fixed-provider-root",
            }
        )
    return {"providers": providers, "count": len(providers)}


def _policy_payload(config_path: Path) -> dict[str, Any]:
    return AgentSourcePolicy.load(config_path).to_payload()


def _save_payload(
    config_path: Path,
    payload: dict[str, Any],
    *,
    acl_hardener: Callable[[Path], None] | None,
) -> dict[str, Any]:
    policy = AgentSourcePolicy.from_payload(payload, config_path=config_path)
    return policy.save_atomic(config_path, acl_hardener=acl_hardener)


def add_source(
    config_path: str | Path,
    *,
    source_id: str,
    provider: str,
    root: str | Path,
    enabled: bool = True,
    acl_hardener: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    path = Path(config_path)
    payload = _policy_payload(path)
    payload["sources"].append(
        {
            "source_id": source_id,
            "provider": provider,
            "root": str(root),
            "enabled": enabled,
            "mode": "catalog-read",
        }
    )
    return _save_payload(path, payload, acl_hardener=acl_hardener)


def set_source_enabled(
    config_path: str | Path,
    *,
    source_id: str,
    enabled: bool,
    acl_hardener: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    path = Path(config_path)
    payload = _policy_payload(path)
    matched = False
    for source in payload["sources"]:
        if source.get("source_id") == source_id:
            source["enabled"] = enabled
            matched = True
            break
    if not matched:
        raise FileNotFoundError("Agent source was not found")
    return _save_payload(path, payload, acl_hardener=acl_hardener)


def remove_source(
    config_path: str | Path,
    *,
    source_id: str,
    acl_hardener: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    path = Path(config_path)
    payload = _policy_payload(path)
    remaining = [
        source for source in payload["sources"]
        if source.get("source_id") != source_id
    ]
    if len(remaining) == len(payload["sources"]):
        raise FileNotFoundError("Agent source was not found")
    payload["sources"] = remaining
    return _save_payload(path, payload, acl_hardener=acl_hardener)


def source_status(
    config_path: str | Path,
    catalog_path: str | Path,
    workspace_root: str | Path,
) -> dict[str, Any]:
    policy = AgentSourcePolicy.load(config_path)
    catalog = AgentCatalog(catalog_path, workspace_root)
    summaries = {
        item["source_id"]: item for item in catalog.source_summaries(policy)
    }
    sources: list[dict[str, Any]] = []
    for source in policy.sources:
        item = dict(summaries[source.source_id])
        item["root"] = str(source.root)
        sources.append(item)
    return {**policy.summary(), "sources": sources}


def refresh_source(
    config_path: str | Path,
    catalog_path: str | Path,
    workspace_root: str | Path,
    source_id: str,
) -> dict[str, Any]:
    policy = AgentSourcePolicy.load(config_path)
    return AgentCatalog(catalog_path, workspace_root).refresh(policy, source_id)


def rebuild_catalog(
    config_path: str | Path,
    catalog_path: str | Path,
    workspace_root: str | Path,
) -> dict[str, Any]:
    policy = AgentSourcePolicy.load(config_path)
    database = Path(catalog_path)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in ("", "-wal", "-shm"):
            current = Path(str(database) + suffix)
            if not current.exists():
                continue
            backup = current.with_name(f"{current.name}.backup-{stamp}")
            if backup.exists():
                raise AgentSourceAdminError("Catalog backup name already exists")
            current.replace(backup)
            moved.append((current, backup))
    except OSError:
        for current, backup in reversed(moved):
            if backup.exists() and not current.exists():
                backup.replace(current)
        raise
    catalog = AgentCatalog(database, workspace_root)
    refreshes = [
        catalog.refresh(policy, source.source_id)
        for source in policy.sources
        if source.enabled
    ]
    return {
        "rebuilt": True,
        "backup_files": [str(backup) for _, backup in moved],
        "refreshes": refreshes,
    }


def run_agent_smoke(
    workspace_root: str | Path,
    *,
    profile: str,
    passthrough_env: tuple[str, ...] = (),
    timeout_seconds: int = 180,
    service_factory: Callable[..., TianChengService] = TianChengService,
) -> dict[str, Any]:
    """Run one explicit, fixed, read-only agent request for local verification."""

    if profile not in _SMOKE_PROFILES:
        raise AgentSourceAdminError("Unknown smoke profile")
    if isinstance(timeout_seconds, bool) or not 10 <= timeout_seconds <= 300:
        raise AgentSourceAdminError("Smoke timeout must be between 10 and 300 seconds")
    workspace = Path(workspace_root).resolve(strict=True)
    service = service_factory(
        workspace,
        None,
        allow_exec=True,
        passthrough_env=passthrough_env,
        access_policy=AccessPolicy.default(workspace),
        enable_agent_catalog=False,
        enable_jobs=False,
    )
    started_at = time.monotonic()
    session_id: str | None = None
    run_id: str | None = None
    try:
        session = service.agent_session_create(
            profile=profile,
            cwd=".",
            sandbox="read-only",
        )
        session_id = session["session_id"]
        run = service.agent_run_start(
            session_id,
            "Do not use any tools. Reply with exactly TIANCHENG_SMOKE_OK and nothing else.",
        )
        run_id = run["run_id"]
        deadline = started_at + timeout_seconds
        while time.monotonic() < deadline:
            inspected = service.agent_run_inspect(session_id, run_id)
            if inspected["state"] not in {"queued", "running"}:
                break
            time.sleep(0.1)
        else:
            service.agent_run_cancel(session_id, run_id)
            raise TimeoutError("Agent smoke timed out and was cancelled")
        result = service.agent_run_result(session_id, run_id)
        if result["state"] != "succeeded":
            diagnostic, _ = redact_text(
                result.get("error") or f"state={result['state']}"
            )
            raise AgentSourceAdminError(
                f"Agent smoke failed ({result['state']}): {diagnostic}"
            )
        if str(result.get("result") or "").strip() != _SMOKE_MARKER:
            raise AgentSourceAdminError("Agent smoke response did not match the fixed marker")
        return {
            "profile": profile,
            "provider": session["provider"],
            "state": "succeeded",
            "marker_verified": True,
            "duration_seconds": round(time.monotonic() - started_at, 3),
        }
    finally:
        if session_id is not None:
            try:
                service.agent_session_close(session_id)
            except (FileNotFoundError, PermissionError, RuntimeError, ValueError):
                pass
        service.shutdown()


def harden_windows_acl(path: Path) -> None:
    if os.name != "nt":
        return
    identity = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
        creationflags=0x08000000,
    )
    try:
        row = next(csv.reader(identity.stdout.splitlines()))
        sid = row[1].strip()
    except (IndexError, StopIteration, csv.Error) as exc:
        raise AgentSourceAdminError("Could not resolve the current Windows SID") from exc
    if identity.returncode != 0 or not sid.startswith("S-"):
        raise AgentSourceAdminError("Could not resolve the current Windows SID")
    result = subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:(F)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
        creationflags=0x08000000,
    )
    if result.returncode != 0:
        raise AgentSourceAdminError("Agent source policy ACL hardening failed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local TianCheng agent source administration")
    parser.add_argument("--config", required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--workspace", required=True)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("discover")
    subparsers.add_parser("status")
    subparsers.add_parser("validate")
    add = subparsers.add_parser("add")
    add.add_argument("--source-id", required=True)
    add.add_argument("--provider", required=True, choices=sorted(_PROVIDER_DEFAULTS))
    add.add_argument("--root", required=True)
    enabled = subparsers.add_parser("set-enabled")
    enabled.add_argument("--source-id", required=True)
    enabled.add_argument("--enabled", required=True, choices=("true", "false"))
    remove = subparsers.add_parser("remove")
    remove.add_argument("--source-id", required=True)
    refresh = subparsers.add_parser("refresh")
    refresh.add_argument("--source-id", required=True)
    subparsers.add_parser("rebuild")
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--profile", required=True, choices=sorted(_SMOKE_PROFILES))
    smoke.add_argument("--pass-env", action="append", default=[])
    smoke.add_argument("--timeout-seconds", type=int, default=180)
    return parser


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    config = Path(arguments.config)
    if arguments.action == "discover":
        result = discover_local_agents()
    elif arguments.action == "status":
        result = source_status(config, arguments.catalog, arguments.workspace)
    elif arguments.action == "validate":
        result = AgentSourcePolicy.load(config).summary()
    elif arguments.action == "add":
        result = add_source(
            config,
            source_id=arguments.source_id,
            provider=arguments.provider,
            root=arguments.root,
            acl_hardener=harden_windows_acl,
        )
    elif arguments.action == "set-enabled":
        result = set_source_enabled(
            config,
            source_id=arguments.source_id,
            enabled=arguments.enabled == "true",
            acl_hardener=harden_windows_acl,
        )
    elif arguments.action == "remove":
        result = remove_source(
            config,
            source_id=arguments.source_id,
            acl_hardener=harden_windows_acl,
        )
    elif arguments.action == "refresh":
        result = refresh_source(
            config, arguments.catalog, arguments.workspace, arguments.source_id
        )
    elif arguments.action == "rebuild":
        result = rebuild_catalog(config, arguments.catalog, arguments.workspace)
    else:
        result = run_agent_smoke(
            arguments.workspace,
            profile=arguments.profile,
            passthrough_env=tuple(arguments.pass_env),
            timeout_seconds=arguments.timeout_seconds,
        )
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
