"""Shared, deliberately bounded operating-system environment compatibility."""
from __future__ import annotations

import os
import hashlib
import json
import re
from dataclasses import dataclass
from collections.abc import Mapping

USER_ENVIRONMENT_NAMES = (
    "APPDATA", "HOME", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA",
    "PROGRAMDATA", "SSH_AUTH_SOCK", "USERPROFILE", "CODEX_HOME",
)
WINDOWS_INSTALLATION_NAMES = (
    "ProgramFiles", "ProgramW6432", "ProgramFiles(x86)",
    "CommonProgramFiles", "CommonProgramW6432", "CommonProgramFiles(x86)",
)
PROTECTED_NAMES = frozenset({
    "CONTROL_PLANE_API_KEY", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "OPENAI_SECRET_KEY",
})
MANAGED_NAMES = frozenset({
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
    "NPM_CONFIG_CACHE", "NPM_CONFIG_UPDATE_NOTIFIER", "PIP_CACHE_DIR",
    "PYTHONPYCACHEPREFIX", "PYTHONUTF8", "PYTHONIOENCODING", "UV_CACHE_DIR",
    "NO_COLOR", "GIT_TERMINAL_PROMPT", "GCM_INTERACTIVE",
})


def normalize_name(name: str, *, windows: bool | None = None) -> str:
    return name.upper() if (os.name == "nt" if windows is None else windows) else name


def validate_names(names: object, *, windows: bool | None = None) -> tuple[str, ...]:
    if not isinstance(names, (list, tuple)) or len(names) > 128:
        raise ValueError("Environment names must be an array of at most 32 names")
    result: dict[str, str] = {}
    for name in names:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_()]{0,127}", name):
            raise ValueError("Invalid environment variable name")
        if name.upper() in PROTECTED_NAMES:
            raise PermissionError("Protected environment variables are never passed to child processes")
        if name.upper() in MANAGED_NAMES:
            raise PermissionError("Environment variable cannot override execution policy")
        result.setdefault(normalize_name(name, windows=windows), name)
    if len(result) > 32:
        raise ValueError("At most 32 distinct environment names may be configured")
    return tuple(result.values())


@dataclass(frozen=True)
class EnvironmentPolicy:
    mode: str = "basic"
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @classmethod
    def parse(cls, document: object = None) -> EnvironmentPolicy:
        if document is None:
            document = {}
        if not isinstance(document, dict) or set(document) - {"mode", "allow", "deny"}:
            raise ValueError("executionEnvironment must contain only mode, allow and deny")
        mode = document.get("mode", "basic")
        if not isinstance(mode, str) or mode not in {"minimal", "basic", "inherit"}:
            raise ValueError("Environment mode must be minimal, basic or inherit")
        return cls(mode, validate_names(document.get("allow", [])), validate_names(document.get("deny", [])))

    def document(self) -> dict[str, object]:
        return {"mode": self.mode, "allow": list(self.allow), "deny": list(self.deny)}

    @property
    def revision(self) -> str:
        document = {"mode": self.mode, "allow": sorted(normalize_name(n) for n in self.allow),
                    "deny": sorted(normalize_name(n) for n in self.deny)}
        return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()

    def build(
        self, managed: Mapping[str, str], *, source: Mapping[str, str] | None = None,
        credential_names: tuple[str, ...] = (),
    ) -> dict[str, str]:
        source = os.environ if source is None else source
        lookup = {normalize_name(n): v for n, v in source.items()}
        blocked = {normalize_name(n) for n in self.deny} | {normalize_name(n) for n in PROTECTED_NAMES}
        credentials = {normalize_name(n) for n in credential_names}
        initial = (
            {n: v for n, v in source.items() if normalize_name(n) not in credentials}
            if self.mode == "inherit" else
            base_environment(source, installation=self.mode == "basic")
        )
        # Explicit business allow entries remain compatible, including names
        # also used by a profile. Inheritance alone never exposes profile keys.
        for name in self.allow:
            if normalize_name(name) in lookup:
                initial[name] = lookup[normalize_name(name)]
        result = {normalize_name(n): (n, v) for n, v in initial.items() if normalize_name(n) not in blocked}
        forced = dict(managed)
        if self.mode == "inherit":
            directories = [*managed.get("PATH", "").split(os.pathsep), *lookup.get(normalize_name("PATH"), "").split(os.pathsep)]
            seen: set[str] = set()
            paths = []
            for directory in directories:
                key = os.path.normcase(os.path.normpath(directory))
                if directory and key not in seen:
                    paths.append(directory)
                    seen.add(key)
            forced["PATH"] = os.pathsep.join(paths)
        result.update({normalize_name(n): (n, v) for n, v in forced.items()})
        return {name: value for name, value in result.values()}

    def summary(self, *, source: Mapping[str, str] | None = None,
                credential_names: tuple[str, ...] = ()) -> dict[str, object]:
        source = os.environ if source is None else source
        present = {normalize_name(name) for name in source}
        denied = {normalize_name(name) for name in self.deny}
        effective = self.build({name: "" for name in MANAGED_NAMES}, source=source,
                               credential_names=credential_names)
        return {
            "schema_version": 1, "mode": self.mode, "policy_revision": self.revision,
            "allow": list(self.allow), "deny": list(self.deny),
            "missing": [name for name in self.allow if normalize_name(name) not in present],
            "denied_allow": [name for name in self.allow if normalize_name(name) in denied],
            "effective_name_count": len(effective),
            "base_names": [name for name in (*USER_ENVIRONMENT_NAMES, *WINDOWS_INSTALLATION_NAMES)
                           if normalize_name(name) in {normalize_name(n) for n in effective}],
            "managed_names": sorted(MANAGED_NAMES),
            "path_source": "registered_tools_and_parent" if self.mode == "inherit" else "registered_tools",
            "agent_mode": "basic", "dedicated_git_mode": "basic",
        }


def base_environment(
    source: Mapping[str, str] | None = None, *, installation: bool = True,
    windows: bool | None = None,
) -> dict[str, str]:
    """Copy existing location hints only; never infer paths or import secrets."""
    source = os.environ if source is None else source
    windows = os.name == "nt" if windows is None else windows
    names = USER_ENVIRONMENT_NAMES + (
        WINDOWS_INSTALLATION_NAMES if windows and installation else ()
    )
    lookup = {name.casefold(): value for name, value in source.items()} if windows else source
    return {
        name: lookup[name.casefold() if windows else name]
        for name in names
        if lookup.get(name.casefold() if windows else name)
    }
