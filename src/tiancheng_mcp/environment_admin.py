"""Local execution-environment explanation and transactional configuration edits."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from .command_policy import trusted_path, _unique_pairs
from .execution_environment import EnvironmentPolicy, validate_names, normalize_name
from .policy import _atomic_write_bytes
from .runtime_config import launcher_config

MAX_CONFIG_BYTES = 1024 * 1024


def read_local(path: Path, workspace: Path | None = None) -> dict:
    path = trusted_path(path, workspace, "Launcher config")
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("Launcher config must be a bounded regular file")
    payload = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_pairs)
    if not isinstance(payload, dict):
        raise ValueError("Launcher config must be an object")
    return payload


def legacy_names(project_root: Path) -> tuple[str, ...]:
    path = trusted_path(project_root / "exec-env.allowlist", None, "Environment allowlist")
    if not path.exists():
        return ()
    if not path.is_file() or path.stat().st_size > 32768:
        raise ValueError("Environment allowlist must be a bounded regular file")
    return validate_names([line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
                           if line.strip() and not line.lstrip().startswith("#")])


def explain(project_root: Path, local: Path) -> dict:
    read_local(local)
    config = launcher_config(project_root, local)
    policy = EnvironmentPolicy.parse(config["executionEnvironment"])
    legacy = legacy_names(project_root)
    document = policy.document()
    document["allow"] = list(validate_names([*policy.allow, *legacy]))
    effective = EnvironmentPolicy.parse(document)
    result = effective.summary()
    result.update({"configuration_role": "next_start", "runtime_mode": "unknown",
                   "pending_restart": None, "configured": policy.document(),
                   "legacy_allow": list(legacy), "legacy_scope": "Windows non-safe launcher",
                   "value_source": "current_parent_environment", "dotenv_names": [],
                   "dotenv_status": "not_configured"})
    # Only inspect whether a missing selected name could be filled at launch;
    # values never enter the explanation or error output.
    env_path = config.get("envFile")
    if env_path:
        path = trusted_path(Path(env_path), None, "Environment file")
        if path.is_file():
            if path.stat().st_size > MAX_CONFIG_BYTES:
                raise ValueError("Environment file exceeds the diagnostic limit")
            selected = {normalize_name(name) for name in result["missing"]}
            names = {}
            try:
                lines = path.read_text(encoding="utf-8-sig").splitlines()
            except OSError:
                result["dotenv_status"] = "unavailable"
                return result
            result["dotenv_status"] = "names_checked"
            for line in lines:
                match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_()]*)\s*=", line)
                if match and normalize_name(match[1]) in selected:
                    names.setdefault(normalize_name(match[1]), match[1])
            result["dotenv_names"] = list(validate_names(list(names.values())))
        else:
            result["dotenv_status"] = "absent"
    return result


def edit(project_root: Path, local: Path, action: str, name: str | None = None) -> dict:
    explain(project_root, local)
    config = launcher_config(project_root, local)
    workspace = Path(config["workspace"]) if config.get("workspace") else None
    current = read_local(local, workspace)
    policy = EnvironmentPolicy.parse(config["executionEnvironment"])
    document = policy.document()
    if action == "reset":
        document = {"mode": "basic", "allow": [], "deny": []}
    elif action == "mode":
        document["mode"] = name
    else:
        validated = validate_names([name])[0]
        field = "allow" if action in {"allow", "remove-allow"} else "deny"
        values = [n for n in document[field] if normalize_name(n) != normalize_name(validated)]
        if action in {"allow", "deny"}:
            values.append(validated)
        document[field] = values
    current["executionEnvironment"] = EnvironmentPolicy.parse(document).document()
    # Validate the merged legacy policy before changing any bytes.
    validate_names([*current["executionEnvironment"]["allow"], *legacy_names(project_root)])
    payload = (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_CONFIG_BYTES:
        raise ValueError("Launcher config exceeds the size limit")
    if local.exists():
        backup = trusted_path(Path(str(local) + ".environment.bak"), workspace, "Launcher backup")
        _atomic_write_bytes(backup, local.read_bytes())
    local.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(local, payload)
    return explain(project_root, local)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--local", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    commands.add_parser("reset")
    for action in ("mode", "allow", "deny", "remove-allow", "remove-deny"):
        commands.add_parser(action).add_argument("name")
    args = parser.parse_args()
    args.project_root = args.project_root.absolute()
    args.local = args.local.absolute()
    try:
        result = explain(args.project_root, args.local) if args.action == "status" else edit(
            args.project_root, args.local, args.action, getattr(args, "name", None))
        print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        # JSON/path errors may contain local data; report only a safe type.
        print(json.dumps({"error": type(exc).__name__, "reason": "环境配置校验或操作失败；未输出变量值。"}, ensure_ascii=True))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
