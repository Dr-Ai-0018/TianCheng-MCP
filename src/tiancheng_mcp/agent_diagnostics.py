"""Allowlisted launch metadata; never retain prompts, argv or credentials."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


def _file_identity(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"path": str(path), "sha256": None}
    try:
        if path.stat().st_size > 256 * 1024 * 1024:
            result["unavailable"] = "file_too_large"
        else:
            with path.open("rb") as stream:
                result["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        result["unavailable"] = "file_not_readable"
    return result


def launch_metadata(
    prefix: list[str],
    environment: Mapping[str, str],
    *,
    cwd: Path,
    command_key: str,
    profile_home: str | None,
) -> dict[str, Any]:
    """Describe the registered launcher and the exact environment sent to Popen.

    The npm launcher may select another native binary. Its package version is
    labelled accordingly; no arbitrary provider is run with --version here.
    Metadata failures must not prevent an otherwise valid process from starting.
    """
    result: dict[str, Any] = {
        "cwd": str(cwd),
        "executable": _file_identity(Path(prefix[0])),
        "runtime_version": None,
        "runtime_version_source": "not_observed",
    }
    if command_key != "codex":
        return result
    raw_home = environment.get("CODEX_HOME")
    if raw_home:
        home = Path(raw_home)
        result["home"] = str(home if home.is_absolute() else cwd / home)
        result["home_source"] = "profile" if profile_home is not None else "parent_environment"
    else:
        user_home = environment.get("USERPROFILE") or environment.get("HOME")
        result["home"] = str(Path(user_home) / ".codex") if user_home else None
        result["home_source"] = "user_default_candidate" if user_home else "unknown"
    # Only the known npm entry point is treated as a launcher, never arbitrary
    # prefix arguments (which may include output paths or sensitive values).
    if len(prefix) > 1 and Path(prefix[1]).name == "codex.js":
        launcher = Path(prefix[1])
        result["launcher"] = _file_identity(launcher)
        manifest = launcher.parent.parent / "package.json"
        try:
            if manifest.stat().st_size <= 64 * 1024:
                package = json.loads(manifest.read_text(encoding="utf-8"))
                version = package.get("version") if isinstance(package, dict) else None
                if package.get("name") == "@openai/codex" and isinstance(version, str):
                    result["launcher_package_version"] = version[:128]
        except (OSError, ValueError, AttributeError):
            pass
    return result


def bounded_launch_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Persist only diagnostic fields, even if a caller adds sensitive metadata."""
    result: dict[str, Any] = {}
    for key in (
        "launch_id", "cwd", "home", "home_source", "profile", "requested_sandbox",
        "requested_approval_policy", "resolved_approval_policy", "resolved_sandbox_policy",
        "runtime_version", "runtime_version_source", "launcher_package_version",
        "windows_home_preflight", "windows_home_preflight_policy",
    ):
        value = context.get(key)
        if value is None or isinstance(value, str):
            result[key] = value[:1024] if isinstance(value, str) else None
    if isinstance(context.get("requested_auto_review"), bool):
        result["requested_auto_review"] = context["requested_auto_review"]
    roots = context.get("requested_additional_write_roots")
    if isinstance(roots, (list, tuple)):
        result["requested_additional_write_roots"] = [
            root[:1024] for root in roots[:32] if isinstance(root, str)
        ]
    for key in ("executable", "launcher"):
        value = context.get(key)
        if isinstance(value, Mapping):
            result[key] = {
                field: item[:1024] if isinstance(item, str) else None
                for field in ("path", "sha256", "unavailable")
                if (item := value.get(field)) is None or isinstance(item, str)
            }
    return result
