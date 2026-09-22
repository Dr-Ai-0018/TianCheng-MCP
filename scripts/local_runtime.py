"""Resolve local-only launcher settings without baking machine paths into Git."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil


PROJECT_RELATIVE_KEYS = {
    "python",
    "mcpScript",
    "mcpExecScript",
    "mcpGrantsScript",
    "accessPolicyPath",
    "agentSourcesPath",
    "agentCatalogPath",
    "envFile",
}


def launcher_config(project_root: Path) -> dict[str, object]:
    merged: dict[str, object] = {}
    for path in (
        project_root / "config" / "launcher.defaults.json",
        project_root / "config" / "launcher.local.json",
    ):
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError(f"Launcher config must be an object: {path}")
            merged.update(value)
    for key in PROJECT_RELATIVE_KEYS:
        value = merged.get(key)
        if isinstance(value, str) and value and not Path(value).is_absolute():
            merged[key] = str((project_root / value).resolve())
    return merged


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
