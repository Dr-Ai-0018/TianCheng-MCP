"""Command-line entry point. stdout is reserved exclusively for MCP stdio."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .server import create_server
from .service import TianChengService


WORKSPACE_ENV = "TIANCHENG_WORKSPACE"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TianCheng workspace-jailed MCP server")
    # The workspace is the security boundary, so it is never guessed.  There is
    # no built-in default: a wrong one would silently expose whichever directory
    # happened to match on this machine.
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
        default=str(Path(__file__).resolve().parents[2] / "logs"),
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
        default=None,
        help="Optional static access-policy.json path (defaults to project config)",
    )
    parser.add_argument(
        "--agent-sources",
        default=None,
        help=(
            "Optional agent-sources.json path (defaults to project config); use an "
            "isolated file to run an instance that sees no local history sources"
        ),
    )
    parser.add_argument(
        "--agent-catalog",
        default=None,
        help="Optional agent catalog database path (defaults to project state directory)",
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
    if not args.workspace:
        parser.error(
            "--workspace is required (or set the "
            f"{WORKSPACE_ENV} environment variable). It names the single "
            "directory this server may touch, and has no default."
        )
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
        allow_policy_hot_reload=args.allow_policy_hot_reload,
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
