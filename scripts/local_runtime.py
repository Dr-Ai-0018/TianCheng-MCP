"""Resolve local-only launcher settings without baking machine paths into Git."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from tiancheng_mcp.runtime_config import PROJECT_RELATIVE_KEYS, launcher_config


def workspace_path(project_root: Path) -> Path:
    value = os.environ.get("TIANCHENG_WORKSPACE") or launcher_config(project_root).get(
        "workspace"
    )
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            "No workspace is configured; set TIANCHENG_WORKSPACE or "
            "config/launcher.local.json:workspace"
        )
    return Path(value).resolve()


def powershell_path(project_root: Path) -> str:
    configured = launcher_config(project_root).get("powerShell")
    if isinstance(configured, str) and configured.strip():
        return configured
    discovered = shutil.which("pwsh")
    if discovered:
        return discovered
    raise RuntimeError("PowerShell 7 was not found; configure powerShell locally")
