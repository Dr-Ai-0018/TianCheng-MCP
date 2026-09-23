"""Provider-neutral local agent runtime state and profile registry.

Provider-specific command construction and event parsing live in
``agent_adapters``; runtime state and the profile registry live here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import threading
from typing import Any
import time
import uuid

from .agent_adapters import (
    AgentAdapter,
    AgentEventParser,
    AgentProfile,
    CodexAdapter,
    ClaudeCodeAdapter,
    CodexJsonlParser,
    NormalizedEvent,
)

MAX_AGENT_EVENTS = 2_000
MAX_AGENT_SESSIONS = 128
MAX_AGENT_RUNS_PER_SESSION = 100

_PROFILE_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_PROFILE_FIELDS_V1 = {
    "name",
    "provider",
    "provider_profile",
    "codex_home",
    "credential_env",
}
_PROFILE_FIELDS_V2 = {
    "name",
    "provider",
    "provider_profile",
    "codex_home",
    "auth",
    "enabled",
}


@dataclass(frozen=True)
class AgentProfileConfig:
    """Validated server-owned profile overlay configuration."""

    profiles: tuple[dict[str, Any], ...]
    inherit_defaults: bool = True


def load_agent_profile_definitions(path: str | Path) -> AgentProfileConfig:
    """Load a strict server-owned profile file without resolving secret values."""

    location = Path(path)
    try:
        payload = json.loads(location.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load agent profile config: {location}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Agent profile config must be an object")
    version = payload.get("version")
    if version == 1:
        if set(payload) != {"version", "profiles"}:
            raise ValueError("Agent profile config v1 must contain only version and profiles")
        inherit_defaults = True
        profile_fields = _PROFILE_FIELDS_V1
    elif version == 2:
        if not set(payload).issubset({"version", "inherit_defaults", "profiles"}):
            raise ValueError("Invalid fields in agent profile config v2")
        if set(payload) != {"version", "inherit_defaults", "profiles"}:
            raise ValueError(
                "Agent profile config v2 requires version, inherit_defaults, and profiles"
            )
        inherit_defaults = payload["inherit_defaults"]
        if not isinstance(inherit_defaults, bool):
            raise ValueError("inherit_defaults must be a boolean")
        profile_fields = _PROFILE_FIELDS_V2
    else:
        raise ValueError("Agent profile config version must be 1 or 2")
    if not isinstance(payload["profiles"], list):
        raise ValueError("Agent profile config profiles must be a list")
    definitions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload["profiles"]):
        if not isinstance(raw, dict) or not set(raw).issubset(profile_fields):
            raise ValueError(f"Invalid fields in agent profile at index {index}")
        if not {"name", "provider", "provider_profile"}.issubset(raw):
            raise ValueError(f"Missing required fields in agent profile at index {index}")
        name = raw["name"]
        provider = raw["provider"]
        provider_profile = raw["provider_profile"]
        if not isinstance(name, str) or _PROFILE_NAME.fullmatch(name) is None:
            raise ValueError(f"Invalid agent profile name at index {index}")
        if name in seen:
            raise ValueError(f"Duplicate agent profile: {name}")
        seen.add(name)
        if not isinstance(provider, str) or _PROFILE_NAME.fullmatch(provider) is None:
            raise ValueError(f"Invalid provider for agent profile {name}")
        if (
            not isinstance(provider_profile, str)
            or not provider_profile
            or len(provider_profile) > 256
            or any(character in provider_profile for character in "\r\n\0")
        ):
            raise ValueError(f"Invalid provider_profile for agent profile {name}")
        codex_home = raw.get("codex_home")
        if codex_home is not None and (
            not isinstance(codex_home, str) or not codex_home.strip()
        ):
            raise ValueError(f"Invalid codex_home for agent profile {name}")
        if codex_home is not None and provider != "codex":
            raise ValueError(f"codex_home requires provider=codex for agent profile {name}")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"Invalid enabled flag for agent profile {name}")
        if version == 1:
            credential_env = raw.get("credential_env")
            auth_mode = "env" if credential_env is not None else "existing-login"
        else:
            auth = raw.get("auth")
            if not isinstance(auth, dict) or "mode" not in auth:
                raise ValueError(f"Missing auth configuration for agent profile {name}")
            auth_mode = auth["mode"]
            if auth_mode == "existing-login":
                if set(auth) != {"mode"}:
                    raise ValueError(
                        f"existing-login auth cannot declare credentials for agent profile {name}"
                    )
                credential_env = None
            elif auth_mode == "env":
                if set(auth) != {"mode", "credential_env"}:
                    raise ValueError(
                        f"env auth requires exactly one credential_env for agent profile {name}"
                    )
                credential_env = auth["credential_env"]
            else:
                raise ValueError(f"Invalid auth mode for agent profile {name}")
        if credential_env is not None and (
            not isinstance(credential_env, str)
            or _ENVIRONMENT_NAME.fullmatch(credential_env) is None
        ):
            raise ValueError(f"Invalid credential_env for agent profile {name}")
        definitions.append(
            {
                "name": name,
                "provider": provider,
                "provider_profile": provider_profile,
                "codex_home": codex_home,
                "credential_env": credential_env,
                "auth_mode": auth_mode,
                "enabled": enabled,
            }
        )
    return AgentProfileConfig(tuple(definitions), inherit_defaults=inherit_defaults)


class AgentProfileRegistry:
    """Server-owned adapter/profile registry.

    Callers may select only profiles exposed by a registered, locally
    available adapter.  No provider module, executable, arguments, or parser
    can be supplied through an MCP request.
    """

    def __init__(
        self,
        available_commands: Iterable[str] | Mapping[str, list[str]],
        adapters: Iterable[AgentAdapter] | None = None,
        profile_definitions: Iterable[Mapping[str, Any]] | None = None,
        inherit_default_profiles: bool = True,
    ) -> None:
        if isinstance(available_commands, Mapping):
            command_prefixes = {
                str(name): list(prefix)
                for name, prefix in available_commands.items()
            }
        else:
            command_prefixes = {str(name): [str(name)] for name in available_commands}
        self._profiles: dict[str, AgentProfile] = {}
        self._adapters: dict[str, AgentAdapter] = {}
        self._known_adapters: dict[str, AgentAdapter] = {}
        self._profile_adapters: dict[str, AgentAdapter] = {}
        registered_adapters = (
            (CodexAdapter(), ClaudeCodeAdapter())
            if adapters is None
            else adapters
        )
        for adapter in registered_adapters:
            if adapter.provider in self._known_adapters:
                raise ValueError(f"Duplicate agent provider: {adapter.provider}")
            self._known_adapters[adapter.provider] = adapter
            prefix = command_prefixes.get(adapter.command)
            if not adapter.probe(prefix):
                continue
            profiles = adapter.profiles()
            if not profiles:
                raise ValueError(
                    f"Agent adapter {adapter.provider} did not register any profiles"
                )
            self._adapters[adapter.provider] = adapter
            for profile in profiles:
                if profile.provider != adapter.provider:
                    raise ValueError(
                        f"Profile {profile.name} provider does not match adapter"
                    )
                if profile.command != adapter.command:
                    raise ValueError(
                        f"Profile {profile.name} command does not match adapter"
                    )
                if profile.name in self._profiles:
                    raise ValueError(f"Duplicate agent profile: {profile.name}")
                self._profiles[profile.name] = profile
                self._profile_adapters[profile.name] = adapter
        if profile_definitions is not None:
            builtin_profile_providers = {
                profile.name: profile.provider
                for adapter in self._known_adapters.values()
                for profile in adapter.profiles()
            }
            if not inherit_default_profiles:
                self._profiles.clear()
                self._profile_adapters.clear()
            for definition in profile_definitions:
                provider = definition["provider"]
                if not isinstance(provider, str):
                    raise ValueError(f"Invalid agent profile provider: {provider!r}")
                # Profiles are server configuration, while executable
                # availability depends on the selected SAFE/DEV launcher and
                # the current machine.  Preserve the existing behavior: an
                # unavailable provider simply exposes no profiles.
                if provider not in self._known_adapters:
                    raise ValueError(f"Unknown agent profile provider: {provider!r}")
                name = str(definition["name"])
                existing = self._profiles.get(name)
                expected_provider = (
                    existing.provider
                    if existing is not None
                    else builtin_profile_providers.get(name)
                )
                if expected_provider is not None and expected_provider != provider:
                    raise ValueError(
                        f"Agent profile {name} cannot change provider from "
                        f"{expected_provider} to {provider}"
                    )
                enabled = definition.get("enabled", True)
                if not isinstance(enabled, bool):
                    raise ValueError(f"Invalid enabled flag for agent profile {name}")
                if not enabled:
                    self._profiles.pop(name, None)
                    self._profile_adapters.pop(name, None)
                    continue
                if provider not in self._adapters:
                    self._profiles.pop(name, None)
                    self._profile_adapters.pop(name, None)
                    continue
                adapter = self._adapters[provider]
                credential_env = definition.get("credential_env")
                auth_mode = definition.get("auth_mode")
                if auth_mode is None:
                    auth_mode = "env" if credential_env is not None else "existing-login"
                profile = AgentProfile(
                    name=name,
                    provider=provider,
                    command=adapter.command,
                    provider_profile=str(definition["provider_profile"]),
                    codex_home=definition.get("codex_home"),
                    credential_env=credential_env,
                    auth_mode=str(auth_mode),
                )
                self._profiles[profile.name] = profile
                self._profile_adapters[profile.name] = adapter

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def get(self, name: str) -> AgentProfile:
        if not isinstance(name, str) or name not in self._profiles:
            available = ", ".join(self.names()) or "none"
            raise ValueError(f"Unknown agent profile; available: {available}")
        return self._profiles[name]

    def adapter_for_profile(self, profile: AgentProfile | str) -> AgentAdapter:
        selected = self.get(profile) if isinstance(profile, str) else profile
        registered = self._profiles.get(selected.name)
        adapter = self._profile_adapters.get(selected.name)
        if registered != selected or adapter is None:
            raise ValueError("Agent profile is not registered by this server")
        if selected.provider != adapter.provider:
            raise ValueError("Agent profile provider does not match its adapter")
        return adapter

    def require_capability(
        self, profile: AgentProfile | str, capability: str
    ) -> AgentAdapter:
        adapter = self.adapter_for_profile(profile)
        capabilities = adapter.capabilities.as_dict()
        if capability not in capabilities:
            raise ValueError(f"Unknown agent capability: {capability}")
        if not capabilities[capability]:
            raise NotImplementedError(
                f"Agent provider {adapter.provider} does not support {capability}"
            )
        return adapter

    def providers(self) -> tuple[dict[str, Any], ...]:
        summaries: list[dict[str, Any]] = []
        for provider, adapter in sorted(self._adapters.items()):
            summaries.append(
                {
                    "provider": provider,
                    "display_name": adapter.display_name,
                    "available": True,
                    "profiles": sorted(
                        profile.name
                        for profile in self._profiles.values()
                        if profile.provider == provider
                    ),
                    "capabilities": adapter.capabilities.as_dict(),
                    "tested_cli_version": getattr(
                        adapter, "tested_cli_version", None
                    ),
                }
            )
        return tuple(summaries)

    def profile_summaries(self) -> tuple[dict[str, Any], ...]:
        """Expose non-sensitive server-owned profile metadata."""

        return tuple(
            {
                "profile": profile.name,
                "provider": profile.provider,
                "auth_mode": profile.auth_mode,
                "runtime_home_isolated": profile.codex_home is not None,
            }
            for profile in sorted(self._profiles.values(), key=lambda item: item.name)
        )

    def build_command(
        self,
        profile: AgentProfile,
        executable_prefix: list[str],
        *,
        prompt: str,
        cwd: str,
        sandbox: str,
        native_session_id: str | None = None,
        invocation_options: Mapping[str, Any] | None = None,
        action: str = "continue",
    ) -> list[str]:
        capability = action if action in {"fork", "review"} else (
            "resume" if native_session_id else "create"
        )
        adapter = self.require_capability(profile, capability)
        return adapter.build_command(
            profile,
            executable_prefix,
            prompt=prompt,
            cwd=cwd,
            sandbox=sandbox,
            native_session_id=native_session_id,
            invocation_options=invocation_options,
            action=action,
        )

    def build_codex_command(
        self,
        profile: AgentProfile,
        executable_prefix: list[str],
        *,
        prompt: str,
        cwd: str,
        sandbox: str,
        native_session_id: str | None = None,
        invocation_options: Mapping[str, Any] | None = None,
        action: str = "continue",
    ) -> list[str]:
        if profile.provider != "codex":
            raise ValueError("Only Codex profiles may use build_codex_command")
        return self.build_command(
            profile,
            executable_prefix,
            prompt=prompt,
            cwd=cwd,
            sandbox=sandbox,
            native_session_id=native_session_id,
            invocation_options=invocation_options,
            action=action,
        )


@dataclass
class AgentRunState:
    run_id: str
    session_id: str
    process_id: str
    parser: AgentEventParser = field(default_factory=CodexJsonlParser)
    stdout_offset: int = 0
    stderr_offset: int = 0
    pending_text: str = ""
    events: list[NormalizedEvent] = field(default_factory=list)
    created_epoch: float = field(default_factory=time.time)
    ended_epoch: float | None = None
    cancelled: bool = False
    state: str = "queued"
    terminal_override: str | None = None
    terminal_event_emitted: bool = False
    error_summary: str | None = None
    invocation_summary: dict[str, Any] = field(default_factory=dict)
    action: str = "continue"
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


@dataclass
class AgentSessionState:
    session_id: str
    profile: str
    cwd: str
    sandbox: str
    provider: str = "codex"
    runtime_home_isolated: bool = False
    # None means cwd is relative to the TianCheng workspace. Otherwise cwd is
    # relative to this whitelisted policy rule root, and every run
    # re-authorizes it so a hot policy reload can widen or revoke access
    # without restarting the server.
    policy_root: str | None = None
    created_epoch: float = field(default_factory=time.time)
    closed: bool = False
    native_session_id: str | None = None
    conversation_ref: str | None = None
    source_id: str | None = None
    codex_defaults: dict[str, Any] = field(default_factory=dict)
    runs: dict[str, AgentRunState] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex}"


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"
