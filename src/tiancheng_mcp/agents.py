"""Provider-neutral local agent runtime state and profile registry.

Provider-specific command construction and event parsing live in
``agent_adapters``.  Compatibility re-exports keep the 0.8 public Python
surface stable while the service runtime moves behind the adapter contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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
    redact_text,
)

MAX_AGENT_EVENTS = 2_000
MAX_AGENT_SESSIONS = 128
MAX_AGENT_RUNS_PER_SESSION = 100


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
        self._profile_adapters: dict[str, AgentAdapter] = {}
        registered_adapters = (
            (CodexAdapter(), ClaudeCodeAdapter())
            if adapters is None
            else adapters
        )
        for adapter in registered_adapters:
            if adapter.provider in self._adapters:
                raise ValueError(f"Duplicate agent provider: {adapter.provider}")
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

    def get_adapter(self, provider: str) -> AgentAdapter:
        if not isinstance(provider, str) or provider not in self._adapters:
            available = ", ".join(sorted(self._adapters)) or "none"
            raise ValueError(f"Unknown agent provider; available: {available}")
        return self._adapters[provider]

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
                }
            )
        return tuple(summaries)

    def build_command(
        self,
        profile: AgentProfile,
        executable_prefix: list[str],
        *,
        prompt: str,
        cwd: str,
        sandbox: str,
        native_session_id: str | None = None,
    ) -> list[str]:
        adapter = self.require_capability(
            profile, "resume" if native_session_id else "create"
        )
        return adapter.build_command(
            profile,
            executable_prefix,
            prompt=prompt,
            cwd=cwd,
            sandbox=sandbox,
            native_session_id=native_session_id,
        )

    def build_codex_command(
        self,
        profile: AgentProfile,
        executable_prefix: list[str],
        *,
        prompt: str,
        cwd: str,
        sandbox: str,
        thread_id: str | None = None,
    ) -> list[str]:
        if profile.provider != "codex":
            raise ValueError("Only Codex profiles may use build_codex_command")
        return self.build_command(
            profile,
            executable_prefix,
            prompt=prompt,
            cwd=cwd,
            sandbox=sandbox,
            native_session_id=thread_id,
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
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


@dataclass
class AgentSessionState:
    session_id: str
    profile: str
    cwd: str
    sandbox: str
    provider: str = "codex"
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
    runs: dict[str, AgentRunState] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def thread_id(self) -> str | None:
        """Compatibility alias retained for the 0.8 MCP payload contract."""

        return self.native_session_id

    @thread_id.setter
    def thread_id(self, value: str | None) -> None:
        self.native_session_id = value


def new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex}"


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex}"
