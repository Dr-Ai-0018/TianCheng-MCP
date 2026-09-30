"""Manage one Linux tunnel-client runtime with a private key file.

The JSON configuration contains paths and a tunnel id, never the key itself.
Only a short, selected status is printed; tunnel-client logs stay on disk.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys


PATH_FIELDS = (
    "client", "python", "runtime_root", "key_file", "workspace", "audit_dir",
    "access_policy", "agent_sources", "agent_catalog", "agent_profiles",
    "agent_env_file", "launcher_local_config",
)
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _load_config(path: Path) -> dict[str, object]:
    if not path.is_absolute() or not path.is_file():
        raise ValueError("--config must name an existing absolute JSON file")
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    for field in PATH_FIELDS:
        value = config.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f"{field} must be an absolute path")
    for field in ("alias", "profile"):
        value = config.get(field)
        if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
            raise ValueError(f"{field} must be a simple name")
    tunnel_id = config.get("tunnel_id")
    if not isinstance(tunnel_id, str) or not re.fullmatch(r"tunnel_[A-Za-z0-9]+", tunnel_id):
        raise ValueError("tunnel_id is invalid")
    workspace = Path(str(config["workspace"])).resolve()
    for field in ("client", "python", "runtime_root", "key_file", "audit_dir",
                  "access_policy", "agent_sources", "agent_catalog", "agent_profiles",
                  "agent_env_file", "launcher_local_config"):
        if _within(Path(str(config[field])).resolve(), workspace):
            raise ValueError(f"{field} must be outside workspace")
    if _within(path.resolve(), workspace):
        raise ValueError("config must be outside workspace")
    return config


def _load_key(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("key_file must be a regular file owned by this user with mode 0600")
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("key_file must contain one CONTROL_PLANE_API_KEY assignment")
    line = lines[0].removeprefix("export ")
    name, sep, value = line.partition("=")
    if name != "CONTROL_PLANE_API_KEY" or not sep:
        raise ValueError("key_file must contain one CONTROL_PLANE_API_KEY assignment")
    if value[:1] in ("'", '"') and value[-1:] == value[:1]:
        value = value[1:-1]
    if not value or any(ch.isspace() for ch in value):
        raise ValueError("key_file value is empty or malformed")
    return value


def _make_env(root: Path, key: str | None) -> dict[str, str]:
    if root.is_symlink():
        raise ValueError("runtime_root must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077:
        raise ValueError("runtime_root must be owned by this user with mode 0700")
    env = os.environ.copy()
    for suffix, variable in (("config", "XDG_CONFIG_HOME"), ("state", "XDG_STATE_HOME"),
                             ("data", "XDG_DATA_HOME")):
        directory = root / suffix
        if directory.is_symlink():
            raise ValueError(f"runtime {suffix} directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.stat().st_uid != os.geteuid() or directory.stat().st_mode & 0o077:
            raise ValueError(f"runtime {suffix} directory must have mode 0700")
        env[variable] = str(directory)
    if key is not None:
        env["CONTROL_PLANE_API_KEY"] = key
    return env


def _command(config: dict[str, object], action: str) -> list[str]:
    command = [str(config["client"]), "runtimes", action]
    if action == "connect":
        mcp = [str(config["python"]), "-m", "tiancheng_mcp"]
        for option, field in (("workspace", "workspace"), ("audit-dir", "audit_dir"),
                              ("access-policy", "access_policy"),
                              ("agent-sources", "agent_sources"),
                              ("agent-catalog", "agent_catalog"),
                              ("agent-profiles", "agent_profiles"),
                              ("agent-env-file", "agent_env_file"),
                              ("launcher-local-config", "launcher_local_config")):
            mcp.extend(["--" + option, str(config[field])])
        command.extend(["--alias", str(config["alias"]),
                        "--tunnel-id", str(config["tunnel_id"]),
                        "--runtime-api-key", "env:CONTROL_PLANE_API_KEY",
                        "--profile", str(config["profile"]),
                        "--profile-dir", str(Path(str(config["runtime_root"])) / "profiles"),
                        "--mcp-command", shlex.join(mcp)])
    else:
        command.append(str(config["alias"]))
    command.append("--json")
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("connect", "status", "stop"))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("this launcher requires Linux/POSIX")
    try:
        config = _load_config(args.config)
        if not Path(str(config["client"])).is_file() or not os.access(str(config["client"]), os.X_OK):
            raise ValueError("client must be executable")
        if not Path(str(config["python"])).is_file() or not os.access(str(config["python"]), os.X_OK):
            raise ValueError("python must be executable")
        key = _load_key(Path(str(config["key_file"]))) if args.action != "stop" else None
        env = _make_env(Path(str(config["runtime_root"])), key)
        result = subprocess.run(_command(config, args.action), env=env, capture_output=True,
                                text=True, timeout=120, check=False)
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"tunnel-client {args.action} exited {result.returncode}; inspect its private runtime log", file=sys.stderr)
            return result.returncode or 1
        summary = {name: report[name] for name in
                   ("runtime_state", "running", "ready", "process_running", "pid", "started", "stopped")
                   if name in report}
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return result.returncode
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        print(f"Linux Tunnel runtime: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
