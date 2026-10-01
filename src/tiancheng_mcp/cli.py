"""Command-line entry point. stdout is reserved exclusively for MCP stdio."""

from __future__ import annotations

import argparse
import logging
import os
import stat
import sys
from pathlib import Path

from .proxy import ProxySettings
from .server import create_server
from .service import TianChengService
from .runtime_config import launcher_config, runtime_arguments


WORKSPACE_ENV = "TIANCHENG_WORKSPACE"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _runtime_directories() -> tuple[Path, Path]:
    if os.name == "nt":
        return PROJECT_ROOT / "config", PROJECT_ROOT / "state"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    if not config_home.is_absolute() or not state_home.is_absolute():
        raise ValueError("XDG_CONFIG_HOME and XDG_STATE_HOME must be absolute paths")
    return config_home / "tiancheng-mcp", state_home / "tiancheng-mcp"


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = path.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or details.st_mode & 0o077
    ):
        raise PermissionError(f"Service directory must be owned by this user and mode 0700: {path}")


def build_parser() -> argparse.ArgumentParser:
    config_dir, state_dir = _runtime_directories()
    parser = argparse.ArgumentParser(description="TianCheng workspace-jailed MCP server")
    parser.add_argument("--runtime-config", default=None, help="Resolve launcher settings before explicit CLI options")
    parser.add_argument("--runtime-project-root", default=str(PROJECT_ROOT), help="Checkout containing launcher defaults")
    parser.add_argument(
        "--workspace",
        default=os.environ.get(WORKSPACE_ENV) or None,
        help=(
            "Absolute path of the single directory this server may touch. "
            f"Required unless {WORKSPACE_ENV} is set."
        ),
    )
    parser.add_argument(
        "--audit-dir",
        default=str(PROJECT_ROOT / "logs" if os.name == "nt" else state_dir / "logs"),
    )
    parser.add_argument(
        "--allow-exec",
        action="store_true",
        help="Register the high-risk allowlisted run_command tool",
    )
    parser.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Explicitly pass one named parent environment variable to DEV child processes; "
            "repeatable, never accepts control-plane keys"
        ),
    )
    parser.add_argument(
        "--allow-external-grants",
        action="store_true",
        help="Enable chat-approved, time-limited access to external directories",
    )
    parser.add_argument(
        "--access-policy",
        default=None if os.name == "nt" else str(config_dir / "access-policy.json"),
        help="Optional static access-policy.json path (defaults to service config)",
    )
    parser.add_argument(
        "--agent-sources",
        default=None if os.name == "nt" else str(config_dir / "agent-sources.json"),
        help=(
            "Optional agent-sources.json path (defaults to service config); use an "
            "isolated file to run an instance that sees no local history sources"
        ),
    )
    parser.add_argument(
        "--agent-catalog",
        default=None if os.name == "nt" else str(state_dir / "agent-catalog.sqlite3"),
        help="Optional agent catalog database path (defaults to service state directory)",
    )
    parser.add_argument(
        "--agent-profiles",
        default=str(config_dir / "agent-profiles.json"),
        help="Server-owned Agent Profile definitions",
    )
    parser.add_argument(
        "--agent-env-file",
        default=str(PROJECT_ROOT / ".env" if os.name == "nt" else config_dir / "agent.env"),
        help="Optional dotenv source; only profile-declared credential names are read",
    )
    parser.add_argument(
        "--pi-cli-entry", default=None,
        help="Server-owned absolute entry to a locally built Pi CLI",
    )
    parser.add_argument(
        "--launcher-local-config",
        default=str(config_dir / "launcher.local.json"),
        help="Local launcher config used for optional outbound proxy settings",
    )
    parser.add_argument(
        "--allow-policy-hot-reload",
        action="store_true",
        help=(
            "High risk: let an approved chat request add directories to the access "
            "policy and take effect immediately without a restart"
        ),
    )
    parser.add_argument(
        "--interactive-timeout-seconds",
        type=int,
        default=75,
        help="Maximum time a tool call waits before returning a background job handle (1-90)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.runtime_config:
        local_path = Path(args.runtime_config).resolve()
        config = launcher_config(Path(args.runtime_project_root), local_path)
        # Explicit command-line options follow configuration and win. Enabling
        # execution or approvals is never inferred from configuration fields.
        args = parser.parse_args(runtime_arguments(config, local_path) + (
            list(sys.argv[1:]) if argv is None else argv
        ))
    if not args.workspace:
        parser.error(
            "--workspace is required (or set the "
            f"{WORKSPACE_ENV} environment variable). It names the single "
            "directory this server may touch, and has no default."
        )
    if os.name != "nt":
        config_dir, state_dir = _runtime_directories()
        workspace_root = Path(args.workspace).resolve(strict=False)
        selected = (
            args.access_policy,
            args.agent_sources,
            args.agent_profiles,
            args.agent_env_file,
            args.launcher_local_config,
        )
        def prepare(directory: Path) -> None:
            canonical = directory.resolve(strict=False)
            if canonical == workspace_root or workspace_root in canonical.parents:
                raise ValueError("Service config and state directories must be outside the workspace")
            _ensure_private_directory(directory)

        if any(Path(path).parent == config_dir for path in selected if path):
            prepare(config_dir)
        if Path(args.audit_dir).parent == state_dir or (
            args.agent_catalog and Path(args.agent_catalog).parent == state_dir
        ):
            prepare(state_dir)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    proxy = ProxySettings.load(
        PROJECT_ROOT / "config" / "launcher.defaults.json",
        Path(args.launcher_local_config),
    )
    proxy.apply_to_process()
    service = TianChengService(
        workspace=args.workspace,
        audit_directory=args.audit_dir,
        allow_exec=args.allow_exec,
        passthrough_env=args.pass_env,
        allow_external_grants=args.allow_external_grants,
        interactive_timeout_seconds=args.interactive_timeout_seconds,
        access_policy_path=args.access_policy,
        agent_source_policy_path=args.agent_sources,
        agent_catalog_path=args.agent_catalog,
        agent_profile_config_path=args.agent_profiles,
        agent_env_file=args.agent_env_file,
        pi_cli_entry=args.pi_cli_entry,
        allow_policy_hot_reload=args.allow_policy_hot_reload,
        agent_proxy_environment=proxy.agent_environment(),
        agent_proxy_mode=proxy.agent,
    )
    try:
        create_server(service).run(transport="stdio")
    except KeyboardInterrupt:
        # tunnel-client cancels stdio children during a normal Ctrl+C shutdown.
        # Do not turn that expected lifecycle event into a scary traceback.
        return
    finally:
        shutdown = getattr(service, "shutdown", None)
        if shutdown is not None:
            shutdown()
        else:  # Backward-compatible with lightweight test doubles.
            service.stop_all_processes()


if __name__ == "__main__":
    main()
