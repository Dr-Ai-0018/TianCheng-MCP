"""One launcher configuration model for UI, scripts and the runtime CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
from collections.abc import Mapping
from .execution_environment import EnvironmentPolicy
from .media_config import MediaSettings
from .execution_config import CaptureSettings

PROJECT_RELATIVE_KEYS = {
    "python", "mcpScript", "mcpExecScript", "mcpGrantsScript", "workspace",
    "accessPolicyPath", "agentSourcesPath", "agentCatalogPath", "envFile",
    "agentProfilesPath", "auditDir", "piCliEntry", "profileDir", "commandPolicyPath",
}
RUNTIME_OPTIONS = {
    "workspace": "--workspace", "accessPolicyPath": "--access-policy",
    "agentSourcesPath": "--agent-sources", "agentCatalogPath": "--agent-catalog",
    "agentProfilesPath": "--agent-profiles", "envFile": "--agent-env-file",
    "auditDir": "--audit-dir", "piCliEntry": "--pi-cli-entry",
    "commandPolicyPath": "--command-policy",
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
            if "executionEnvironment" in value:
                environment_policy = value["executionEnvironment"]
                if not isinstance(environment_policy, dict):
                    raise ValueError("executionEnvironment must be an object")
                EnvironmentPolicy.parse(environment_policy)
                value["executionEnvironment"] = {
                    **merged.get("executionEnvironment", {}), **environment_policy,
                }
            if 'media' in value:
                media = value['media']
                if not isinstance(media, dict):
                    raise ValueError('media must be an object')
                previous = merged.get('media', {})
                combined = {**previous, **media}
                if 'attachments' in media and isinstance(media['attachments'], dict):
                    combined['attachments'] = {**previous.get('attachments', {}), **media['attachments']}
                value['media'] = MediaSettings.parse(combined).document()
            if 'observability' in value:
                capture = value['observability']
                if not isinstance(capture, dict):
                    raise ValueError('observability must be an object')
                combined_capture = {**merged.get('observability', {}), **capture}
                value['observability'] = CaptureSettings.parse(combined_capture).document()
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
    merged["executionEnvironment"] = EnvironmentPolicy.parse(merged.get("executionEnvironment")).document()
    merged['media'] = MediaSettings.parse(merged.get('media')).document()
    merged['observability'] = CaptureSettings.parse(merged.get('observability')).document()
    return merged


def runtime_arguments(config: Mapping[str, object], local_path: Path) -> list[str]:
    arguments = ["--launcher-local-config", str(local_path)]
    arguments.extend(('--media-config', json.dumps(MediaSettings.parse(config.get('media')).document())))
    arguments.extend(('--observability-config', json.dumps(CaptureSettings.parse(config.get('observability')).document())))
    for key, option in RUNTIME_OPTIONS.items():
        value = config.get(key)
        if value:
            arguments.extend((option, str(value)))
    arguments.extend(("--interactive-timeout-seconds", str(config["interactiveTimeoutSeconds"])))
    policy = EnvironmentPolicy.parse(config.get("executionEnvironment"))
    arguments.extend(("--exec-env-mode", policy.mode))
    for name in policy.allow:
        arguments.extend(("--pass-env", name))
    for name in policy.deny:
        arguments.extend(("--deny-env", name))
    return arguments
