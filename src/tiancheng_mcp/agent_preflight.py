"""Read-only checks for explicit Windows Codex homes, never provisioning."""

from __future__ import annotations

import json
import stat
from pathlib import Path


_MAX_MARKER_BYTES = 64 * 1024


def validate_windows_home_preflight(
    policy: object, *, provider: str, codex_home: object,
) -> str:
    """Validate a server-owned choice, including disabled/unavailable profiles."""
    if not isinstance(policy, str) or policy not in {"none", "require-existing"}:
        raise ValueError("windows_home_preflight must be none or require-existing")
    if policy == "require-existing" and (
        provider != "codex" or not isinstance(codex_home, str) or not codex_home.strip()
    ):
        raise ValueError("windows_home_preflight=require-existing requires provider=codex and explicit codex_home")
    return policy


def _metadata_issue(path: Path, *, directory: bool = False) -> str | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing"
    except PermissionError:
        return "unreadable"
    except OSError:
        return "metadata_unavailable"
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        return "reparse_point"
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode):
        return "wrong_type"
    if not directory and metadata.st_size == 0:
        return "empty"
    return None


def inspect_windows_codex_home(home: Path) -> str:
    """Return a fixed reason code; no contents, paths or exception text escape.

    This is deliberately NOT a readiness check. We do not decrypt credentials,
    attempt logon, infer the effective backend, or compare against a hardcoded
    runtime/schema version. Even intact files may trigger native provisioning.
    The caller enforces this only for Windows profiles selecting require-existing.
    """
    for name, label in ((".sandbox", "sandbox_directory"), (".sandbox-secrets", "credentials_directory")):
        if issue := _metadata_issue(home / name, directory=True):
            return f"{label}_{issue}"
    marker = home / ".sandbox" / "setup_marker.json"
    if issue := _metadata_issue(marker):
        return f"marker_{issue}"
    try:
        with marker.open("rb") as stream:
            payload = stream.read(_MAX_MARKER_BYTES + 1)
    except FileNotFoundError:
        return "marker_missing"
    except PermissionError:
        return "marker_unreadable"
    except OSError:
        return "marker_read_failed"
    if len(payload) > _MAX_MARKER_BYTES:
        return "marker_too_large"
    try:
        marker_data = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        return "marker_invalid_json"
    if not isinstance(marker_data, dict) or not marker_data:
        return "marker_invalid_object"
    # Metadata only: opening this file would access encrypted credentials.
    credentials = home / ".sandbox-secrets" / "sandbox_users.json"
    if issue := _metadata_issue(credentials):
        return f"credentials_{issue}"
    return "not_verified"
