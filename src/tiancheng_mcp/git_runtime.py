"""Git configuration semantics and server-owned execution safeguards.

Configuration is decoded by Git itself, without following repository includes.
The command safeguards also apply to init/clone and trusted global settings;
they never discard the user's identity or DEV credential-helper configuration.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .security import WorkspaceSecurityError

MAX_CONFIG_BYTES = 1024 * 1024
_UNSAFE_SECTIONS = frozenset({
    "include", "includeif", "filter", "credential", "url", "alias", "gpg", "diff",
})
_UNSAFE_VARIABLES = frozenset({
    "worktree", "worktreeconfig", "hookspath", "excludesfile", "attributesfile",
    "sshcommand", "fsmonitor", "textconv", "external", "signingkey", "template",
})


class GitRuntime:
    def __init__(self, executable: str, runner: Callable[..., dict[str, Any]]) -> None:
        self.executable = executable
        self.runner = runner

    @staticmethod
    def safeguards() -> list[str]:
        settings = (
            f"core.hooksPath={os.devnull}", "core.fsmonitor=false",
            "commit.gpgSign=false", "tag.gpgSign=false", "gc.auto=0",
            "maintenance.auto=false", "submodule.recurse=false", "protocol.ext.allow=never",
        )
        return [part for setting in settings for part in ("-c", setting)]

    def _config(self, cwd: Path, arguments: Sequence[str], **kwargs: Any) -> dict[str, Any]:
        return self.runner(
            # Git may process core.worktree during repository setup, before
            # --file is parsed. Freeze the parser's cwd so an unsafe value
            # cannot redirect that setup or prevent semantic validation.
            [self.executable, "-C", str(cwd), "--work-tree", str(cwd),
             *self.safeguards(), "config", *arguments],
            cwd=cwd, **kwargs,
        )

    def validate_config(self, config: Path, **kwargs: Any) -> None:
        if not config.exists():
            return
        if config.stat().st_size > MAX_CONFIG_BYTES:
            raise WorkspaceSecurityError("Repository Git config is too large")
        result = self._config(
            config.parent.parent,
            ["--file", str(config), "--no-includes", "--null", "--list"],
            **kwargs,
        )
        if result["exit_code"] or result["timeout"] or result["cancelled"] or result["stdout_truncated"]:
            raise WorkspaceSecurityError("Repository Git config could not be safely parsed")
        for record in result["stdout"].split("\0"):
            if not record:
                continue
            # With --null --list Git separates each key/value by a newline;
            # values may contain newlines, so only split the first one.
            key, separator, value = record.partition("\n")
            section = key.split(".", 1)[0].casefold()
            variable = key.rsplit(".", 1)[-1].casefold()
            if section in _UNSAFE_SECTIONS:
                raise WorkspaceSecurityError("Repository config contains an unsafe Git section")
            if variable in _UNSAFE_VARIABLES or (
                variable == "gpgsign" and (not separator or value.casefold() not in {"false", "no", "off", "0"})
            ) or key.casefold().startswith("protocol."):
                raise WorkspaceSecurityError("Repository config contains an unsafe Git setting")

    def run(self, cwd: Path, arguments: Sequence[str], *, allow_exec: bool, **kwargs: Any) -> dict[str, Any]:
        safeguards = self.safeguards()
        if not allow_exec:
            # Attributes can select a server-global filter. SAFE may retain
            # global identity, but must not execute that filter's program.
            filters = self._config(cwd, ["--null", "--name-only", "--get-regexp", r"^filter\."], **kwargs)
            if filters["exit_code"] not in {0, 1} or filters["timeout"] or filters["cancelled"] or filters["stdout_truncated"]:
                raise WorkspaceSecurityError("Git filters could not be safely evaluated")
            for key in filters["stdout"].split("\0"):
                if not key:
                    continue
                prefix = key.rsplit(".", 1)[0]
                safeguards.extend(part for setting in (
                    f"{prefix}.clean=", f"{prefix}.smudge=", f"{prefix}.process=", f"{prefix}.required=false",
                ) for part in ("-c", setting))
        return self.runner(
            [self.executable, "-C", str(cwd), *safeguards, *arguments], cwd=cwd, **kwargs,
        )
