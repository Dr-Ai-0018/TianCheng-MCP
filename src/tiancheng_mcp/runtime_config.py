"""One launcher configuration model for UI, scripts and the runtime CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
from collections.abc import Mapping

PROJECT_RELATIVE_KEYS = {
    "python", "mcpScript", "mcpExecScript", "mcpGrantsScript", "workspace",
    "accessPolicyPath", "agentSourcesPath", "agentCatalogPath", "envFile",
    "agentProfilesPath", "auditDir", "piCliEntry", "profileDir",
}
RUNTIME_OPTIONS = {
    "workspace": "--workspace", "accessPolicyPath": "--access-policy",
    "agentSourcesPath": "--agent-sources", "agentCatalogPath": "--agent-catalog",
    "agentProfilesPath": "--agent-profiles", "envFile": "--agent-env-file",
    "auditDir": "--audit-dir", "piCliEntry": "--pi-cli-entry",
}


def launcher_config(project_root: Path, local_path: Path | None = None,
                    environment: Mapping[str, str] | None = None) -> dict[str, object]:
    project_root = project_root.resolve()
    merged: dict[str, object] = {}
    for path in (project_root / "config/launcher.defaults.json",
                 local_path if local_path is not None else project_root / "config/launcher.local.json"):
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError(f"Launcher config must be an object: {path}")
            merged.update(value)
    source = os.environ if environment is None else environment
    if source.get("TIANCHENG_WORKSPACE"):
        merged["workspace"] = source["TIANCHENG_WORKSPACE"]
    for key in PROJECT_RELATIVE_KEYS:
        value = merged.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Launcher {key} must be a path string")
        if isinstance(value, str) and value:
            # Normalize lexically; the jail and source validators must still
            # see a configured link rather than an already dereferenced root.
            merged[key] = os.path.abspath(project_root / value)
    timeout = merged.get("interactiveTimeoutSeconds", 75)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 90:
        raise ValueError("interactiveTimeoutSeconds must be an integer from 1 to 90")
    merged["interactiveTimeoutSeconds"] = timeout
    return merged


def runtime_arguments(config: Mapping[str, object], local_path: Path) -> list[str]:
    arguments = ["--launcher-local-config", str(local_path)]
    for key, option in RUNTIME_OPTIONS.items():
        value = config.get(key)
        if value:
            arguments.extend((option, str(value)))
    arguments.extend(("--interactive-timeout-seconds", str(config["interactiveTimeoutSeconds"])))
    return arguments
