"""Shared trusted executable discovery for the service and local console."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

from .security import WorkspaceSecurityError


def _desktop_codex_roots() -> tuple[Path, ...]:
    """Install roots of the Codex Desktop app's private, versioned builds."""
    roots: list[Path] = []
    for name in ("LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(name)
        if not base:
            continue
        try:
            roots.append((Path(base) / "OpenAI" / "Codex").resolve())
        except OSError:
            continue
    return tuple(roots)


def discover_shell_commands(workspace: Path) -> dict[str, list[str]]:
    names = ("pwsh", "powershell", "cmd") if os.name == "nt" else ("bash", "sh")
    result = {}
    for name in names:
        executable = shutil.which(name)
        if executable:
            target = Path(executable).resolve()
            if target.is_relative_to(workspace.resolve()):
                raise WorkspaceSecurityError("Refusing a Shell executable from inside the workspace")
            if os.name != "nt" or target.suffix.casefold() == ".exe":
                result[name] = [str(target)]
    return result


def discover_exec_commands(workspace: Path) -> dict[str, list[str]]:
    discovered: dict[str, list[str]] = {
        "python": [sys.executable],
        "pytest": [sys.executable, "-m", "pytest"],
    }
    for name in ("py", "uv", "git", "gh", "node", "rg"):
        executable = shutil.which(name)
        if executable:
            discovered[name] = [str(Path(executable).resolve())]
    node = discovered.get("node")
    if node:
        for name, script_name in (("npm", "npm-cli.js"), ("npx", "npx-cli.js")):
            command_file = shutil.which(name)
            if not command_file:
                continue
            candidate = Path(command_file).resolve().parent / "node_modules/npm/bin" / script_name
            if candidate.is_file():
                discovered[name] = [*node, str(candidate)]
    # Require the npm-managed launcher.  It supplies
    # CODEX_MANAGED_PACKAGE_ROOT so the native runtime resolves its own
    # codex-resources (sandbox setup helper and command runner). Keep this
    # service's launcher selection stable when Desktop updates change PATH.
    # Different build numbers alone do not establish marker incompatibility
    # or prevent CLI/Desktop coexistence; setup also depends on home state.
    codex_executable = shutil.which("codex") or shutil.which("codex.exe")
    if codex_executable:
        codex_path = Path(codex_executable).resolve()
        from_desktop = any(
            codex_path.is_relative_to(root) for root in _desktop_codex_roots()
        )
        if from_desktop:
            pass  # Never mix a Desktop build with the npm-managed release.
        elif codex_path.suffix.casefold() in {".cmd", ".ps1"}:
            codex_script = codex_path.parent / "node_modules/@openai/codex/bin/codex.js"
            node = discovered.get("node")
            if node and codex_script.is_file():
                discovered["codex"] = [*node, str(codex_script)]
        elif os.name != "nt":
            discovered["codex"] = [str(codex_path)]
    for command in discovered.values():
        executable = Path(command[0]).resolve()
        try:
            executable.relative_to(workspace)
        except ValueError:
            continue
        raise WorkspaceSecurityError("Refusing an allowlisted executable from inside the workspace")
    return discovered
