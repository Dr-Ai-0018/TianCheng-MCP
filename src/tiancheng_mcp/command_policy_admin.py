"""Local-only policy editing; no MCP configuration mutation endpoint."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from .command_policy import CommandPolicy, CommandPolicyError, _unique_pairs, command_name, disabled_name, read_document, trusted_path
from .policy import _atomic_write_bytes
from .command_discovery import discover_exec_commands


def save_policy(path: Path, workspace: Path, document: dict) -> CommandPolicy:
    path = trusted_path(path, workspace, "Command policy")
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Validate exactly the bytes being saved, including executable availability,
    # without replacing the current policy on a malformed edit.
    handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".json")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        candidate = CommandPolicy.load(Path(temporary), workspace)
        candidate.resolve(discover_exec_commands(workspace), workspace)
        _atomic_write_bytes(path, payload)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return CommandPolicy.load(path, workspace)


def edit_policy(path: Path, workspace: Path, action: str, name: str | None = None, rule: dict | None = None) -> CommandPolicy:
    # Loading first also checks existing schema and fields. Reset is the one
    # action that deliberately recovers a broken existing configuration.
    if action == "reset":
        document = {"schema_version": 1}
    else:
        current = CommandPolicy.load(path, workspace)
        document = read_document(path, workspace, optional=True)
        if action == "preset":
            if name not in current.available_presets:
                raise CommandPolicyError("Unknown command preset")
            document["preset"] = name
        else:
            alias = disabled_name(name) if action in {"disable", "enable"} else command_name(name)
            if action == "add":
                document["add"] = {key: value for key, value in document.get("add", {}).items() if command_name(key) != alias}
                document["add"][alias] = rule
            elif action in {"disable", "enable"}:
                disabled = {disabled_name(item) for item in document.get("disable", [])}
                if action == "disable":
                    disabled.add(alias)
                else:
                    disabled.discard(alias)
                document["disable"] = sorted(disabled)
            elif action == "remove":
                # Normalize existing keys so uppercase/.exe aliases can be
                # removed through the same UI name used in the status table.
                document["add"] = {key: value for key, value in document.get("add", {}).items() if command_name(key) != alias}
            else:
                raise CommandPolicyError("Unknown edit action")
    return save_policy(path, workspace, document)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    commands.add_parser("reset")
    for action in ("preset", "disable", "enable", "remove", "add-rule"):
        commands.add_parser(action).add_argument("name")
    add = commands.add_parser("add-builtin")
    add.add_argument("name")
    add.add_argument("builtin")
    args = parser.parse_args()
    try:
        workspace = args.workspace.resolve(strict=True)
        if not workspace.is_dir():
            raise CommandPolicyError("Workspace must be a directory")
        if args.action == "status":
            policy = CommandPolicy.load(args.policy, workspace)
        elif args.action == "add-builtin":
            policy = edit_policy(args.policy, workspace, "add", args.name, {"builtin": args.builtin, "arguments": "any"})
        elif args.action == "add-rule":
            value = sys.stdin.read(1024 * 1024 + 1)
            if len(value) > 1024 * 1024:
                raise CommandPolicyError("Command rule input is too large")
            policy = edit_policy(args.policy, workspace, "add", args.name, json.loads(value, object_pairs_hook=_unique_pairs))
        else:
            policy = edit_policy(args.policy, workspace, args.action, getattr(args, "name", None))
        resolved = policy.resolve(discover_exec_commands(workspace), workspace)
        print(json.dumps(policy.summary(resolved, enabled=None, configuration_role="next_start"), ensure_ascii=False))
    except Exception as exc:
        # Parse/path errors can embed fixed argv or credentials. Never echo the
        # exception text or the input payload to the terminal.
        result = {"error": type(exc).__name__}
        if isinstance(exc, CommandPolicyError):
            # These validation messages are fixed strings; they never contain
            # the caller's rule, executable path, or argument values.
            result["reason"] = str(exc)
        print(json.dumps(result))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
