"""Provider-neutral local agent adapter contracts and the Codex adapter.

Adapters are server-owned.  Callers select only a registered profile; they
cannot provide executable paths, arbitrary flags, parsers, or environment
overrides.  This keeps future providers behind the same lifecycle boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
import re
import time
from typing import Any, Iterable, Protocol, runtime_checkable


MAX_AGENT_PROMPT_CHARS = 32_000
MAX_EVENT_SUMMARY_BYTES = 8 * 1024
MAX_EVENT_DATA_BYTES = 16 * 1024
_ALLOWED_SANDBOXES = frozenset({"read-only", "workspace-write"})
_NATIVE_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CODEX_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
CODEX_PROFILE_ENV = "TIANCHENG_CODEX_PROFILE"


def _codex_profile_override() -> str:
    """Return the optional local Codex profile name, or an empty string.

    Set ``TIANCHENG_CODEX_PROFILE`` to run the stock ``codex`` CLI under one of
    your own configured profiles.  The value is a bare profile name: it is
    validated against a strict character class so it can never introduce an
    extra argument into the fixed command template.
    """

    raw = (os.environ.get(CODEX_PROFILE_ENV) or "").strip()
    if not raw:
        return ""
    if not _CODEX_PROFILE_NAME.fullmatch(raw):
        raise ValueError(
            f"{CODEX_PROFILE_ENV} must be a bare profile name: it has to start "
            "with a letter or digit and may then contain letters, digits, dot, "
            "dash, and underscore, up to 64 characters"
        )
    return raw
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+"),
    re.compile(r"(?i)(sk-[A-Za-z0-9_-]{8,})"),
    re.compile(r"(?i)((?:token|key|secret|password)\s*[=:]\s*)[^\s,;]+"),
)


def _bounded_text(value: object, maximum: int) -> tuple[str, bool]:
    text = value if isinstance(value, str) else str(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return text, False
    clipped = encoded[:maximum].decode("utf-8", errors="ignore")
    return clipped, True


def redact_text(value: object, maximum: int = MAX_EVENT_SUMMARY_BYTES) -> tuple[str, bool]:
    text = value if isinstance(value, str) else str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: f"{match.group(1) if match.lastindex else ''}<redacted>",
            text,
        )
    return _bounded_text(text, maximum)


@dataclass(frozen=True)
class AdapterCapabilities:
    create: bool = True
    attach: bool = False
    resume: bool = False
    discover: bool = False
    stream: bool = True
    cancel: bool = True
    steer: bool = False
    interaction: bool = False
    fork: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "create": self.create,
            "attach": self.attach,
            "resume": self.resume,
            "discover": self.discover,
            "stream": self.stream,
            "cancel": self.cancel,
            "steer": self.steer,
            "interaction": self.interaction,
            "fork": self.fork,
        }


@dataclass(frozen=True)
class AgentProfile:
    name: str
    provider: str
    command: str
    provider_profile: str
    pass_configured_environment: bool = False
    allowed_sandboxes: frozenset[str] = _ALLOWED_SANDBOXES
    max_runtime_seconds: int = 900
    max_output_bytes: int = 512 * 1024

    @property
    def agent(self) -> str:
        """Compatibility alias retained for 0.8 callers and tests."""

        return self.provider

    @property
    def codex_profile(self) -> str:
        """Compatibility alias retained for callers predating multi-provider."""

        return self.provider_profile if self.provider == "codex" else ""

    def validate_sandbox(self, sandbox: str) -> str:
        if sandbox not in self.allowed_sandboxes:
            raise ValueError(
                f"sandbox must be one of: {', '.join(sorted(self.allowed_sandboxes))}"
            )
        return sandbox


@dataclass(frozen=True)
class NormalizedEvent:
    seq: int
    type: str
    summary: str
    data: dict[str, Any]
    truncated: bool = False
    created_epoch: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.type,
            "summary": self.summary,
            "data": self.data,
            "truncated": self.truncated,
            "created_at": datetime.fromtimestamp(self.created_epoch, UTC).isoformat(),
        }


@runtime_checkable
class AgentEventParser(Protocol):
    next_seq: int
    native_session_id: str | None
    final_message: str | None

    def feed_line(self, line: str) -> NormalizedEvent | None: ...

    def synthetic_event(
        self,
        event_type: str,
        summary_value: object,
        data: dict[str, Any] | None = None,
    ) -> NormalizedEvent: ...


@runtime_checkable
class AgentAdapter(Protocol):
    provider: str
    display_name: str
    command: str
    capabilities: AdapterCapabilities

    def profiles(self) -> tuple[AgentProfile, ...]: ...

    def probe(self, executable_prefix: list[str] | None) -> bool: ...

    def new_parser(self) -> AgentEventParser: ...

    def build_command(
        self,
        profile: AgentProfile,
        executable_prefix: list[str],
        *,
        prompt: str,
        cwd: str,
        sandbox: str,
        native_session_id: str | None = None,
    ) -> list[str]: ...


class CodexJsonlParser:
    """Normalize Codex JSONL while retaining only bounded event data."""

    def __init__(self) -> None:
        self.next_seq = 0
        self.native_session_id: str | None = None
        self.final_message: str | None = None

    @property
    def thread_id(self) -> str | None:
        """Compatibility alias for the provider-neutral native session id."""

        return self.native_session_id

    def feed_line(self, line: str) -> NormalizedEvent | None:
        if not isinstance(line, str) or not line.strip():
            return None
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        event_type = raw.get("type")
        data: dict[str, Any] = {}
        normalized_type = "status"
        summary_value: object = event_type or "codex event"
        clipped = False
        if event_type == "thread.started":
            normalized_type = "thread_started"
            candidate = raw.get("thread_id")
            self.native_session_id = (
                candidate
                if isinstance(candidate, str) and _NATIVE_SESSION_ID.fullmatch(candidate)
                else None
            )
            data = {"thread_id": self.native_session_id} if self.native_session_id else {}
            summary_value = "Codex thread started"
        elif event_type == "item.completed":
            item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
            item_type = item.get("type")
            if item_type == "agent_message":
                normalized_type = "agent_message"
                message, clipped = redact_text(item.get("text", ""), MAX_EVENT_DATA_BYTES)
                self.final_message = message
                data = {"text": message}
                summary_value = message
            else:
                data = {"item_type": item_type} if isinstance(item_type, str) else {}
                summary_value = f"Completed {item_type or 'item'}"
        elif event_type in {"error", "turn.failed"}:
            normalized_type = "error"
            message, clipped = redact_text(
                raw.get("message") or raw.get("error") or event_type
            )
            summary_value = message
            data = {"message": message}
        elif isinstance(event_type, str):
            data = {"source_type": event_type}
        summary, summary_clipped = redact_text(summary_value)
        event = NormalizedEvent(
            self.next_seq,
            normalized_type,
            summary,
            data,
            clipped or summary_clipped,
        )
        self.next_seq += 1
        return event

    def feed(self, lines: Iterable[str]) -> list[NormalizedEvent]:
        events: list[NormalizedEvent] = []
        for line in lines:
            event = self.feed_line(line)
            if event is not None:
                events.append(event)
        return events

    def synthetic_event(
        self,
        event_type: str,
        summary_value: object,
        data: dict[str, Any] | None = None,
    ) -> NormalizedEvent:
        summary, summary_clipped = redact_text(summary_value)
        safe_data: dict[str, Any] = {}
        data_clipped = False
        for key, value in (data or {}).items():
            safe_value, clipped = redact_text(value, MAX_EVENT_DATA_BYTES)
            safe_data[str(key)] = safe_value
            data_clipped = data_clipped or clipped
        event = NormalizedEvent(
            self.next_seq,
            event_type,
            summary,
            safe_data,
            summary_clipped or data_clipped,
        )
        self.next_seq += 1
        return event


class ClaudeJsonlParser:
    """Normalize Claude Code stream-json without retaining tool payloads."""

    def __init__(self) -> None:
        self.next_seq = 0
        self.native_session_id: str | None = None
        self.final_message: str | None = None

    def feed_line(self, line: str) -> NormalizedEvent | None:
        if not isinstance(line, str) or not line.strip():
            return None
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        candidate = raw.get("session_id")
        if isinstance(candidate, str) and _NATIVE_SESSION_ID.fullmatch(candidate):
            self.native_session_id = candidate
        source_type = raw.get("type")
        normalized_type = "status"
        summary_value: object = source_type or "claude event"
        data: dict[str, Any] = {}
        clipped = False
        if source_type == "system" and raw.get("subtype") == "init":
            normalized_type = "thread_started"
            summary_value = "Claude Code session started"
            if self.native_session_id:
                data = {"session_id": self.native_session_id}
        elif source_type == "assistant":
            message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            text_blocks = [
                block.get("text")
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
            if not text_blocks:
                return None
            message_text, clipped = redact_text(
                "\n".join(text_blocks), MAX_EVENT_DATA_BYTES
            )
            normalized_type = "agent_message"
            summary_value = message_text
            data = {"text": message_text}
        elif source_type == "result":
            result_text, clipped = redact_text(
                raw.get("result") or raw.get("error") or "Claude Code result",
                MAX_EVENT_DATA_BYTES,
            )
            if raw.get("is_error"):
                normalized_type = "error"
                summary_value = result_text
                data = {"message": result_text}
            else:
                normalized_type = "agent_message"
                summary_value = result_text
                data = {"text": result_text}
                self.final_message = result_text
        elif isinstance(source_type, str):
            data = {"source_type": source_type}
        summary, summary_clipped = redact_text(summary_value)
        event = NormalizedEvent(
            self.next_seq,
            normalized_type,
            summary,
            data,
            clipped or summary_clipped,
        )
        self.next_seq += 1
        return event

    def synthetic_event(
        self,
        event_type: str,
        summary_value: object,
        data: dict[str, Any] | None = None,
    ) -> NormalizedEvent:
        summary, summary_clipped = redact_text(summary_value)
        safe_data: dict[str, Any] = {}
        data_clipped = False
        for key, value in (data or {}).items():
            safe_value, clipped = redact_text(value, MAX_EVENT_DATA_BYTES)
            safe_data[str(key)] = safe_value
            data_clipped = data_clipped or clipped
        event = NormalizedEvent(
            self.next_seq,
            event_type,
            summary,
            safe_data,
            summary_clipped or data_clipped,
        )
        self.next_seq += 1
        return event


class CodexAdapter:
    provider = "codex"
    display_name = "Codex"
    command = "codex"
    capabilities = AdapterCapabilities(
        attach=True, resume=True, stream=True, cancel=True
    )

    def profiles(self) -> tuple[AgentProfile, ...]:
        # The public default drives the stock Codex CLI.  A named Codex
        # profile is an optional local choice, so it comes from the
        # environment rather than a tracked default: shipping one here would
        # bake a private provider into everybody's install.
        return (
            AgentProfile(
                name="codex-default",
                provider=self.provider,
                command=self.command,
                provider_profile=_codex_profile_override(),
                pass_configured_environment=True,
            ),
        )

    def probe(self, executable_prefix: list[str] | None) -> bool:
        return bool(executable_prefix)

    def new_parser(self) -> AgentEventParser:
        return CodexJsonlParser()

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
        if profile.provider != self.provider:
            raise ValueError("Agent profile does not belong to the Codex adapter")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty text")
        if "\x00" in prompt:
            raise ValueError("prompt cannot contain NUL bytes")
        if len(prompt) > MAX_AGENT_PROMPT_CHARS:
            raise ValueError(f"prompt is limited to {MAX_AGENT_PROMPT_CHARS} characters")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("cwd must be text")
        profile.validate_sandbox(sandbox)
        if native_session_id is not None and (
            not isinstance(native_session_id, str)
            or not _NATIVE_SESSION_ID.fullmatch(native_session_id)
        ):
            raise ValueError("native_session_id is invalid")
        command = [
            *executable_prefix,
            "exec",
            "--json",
            "-s",
            sandbox,
        ]
        if profile.provider_profile:
            command.extend(("-p", profile.provider_profile))
        command.extend(("-C", cwd))
        if native_session_id:
            command.extend(("resume", native_session_id, prompt))
        else:
            command.append(prompt)
        return command


class ClaudeCodeAdapter:
    provider = "claude-code"
    display_name = "Claude Code"
    command = "claude"
    capabilities = AdapterCapabilities(
        attach=True, resume=True, stream=True, cancel=True
    )

    _SANDBOX_POLICY = {
        "read-only": ("plan", "Read,Glob,Grep"),
        "workspace-write": ("acceptEdits", "Read,Glob,Grep,Edit,Write"),
    }

    def profiles(self) -> tuple[AgentProfile, ...]:
        return (
            AgentProfile(
                name="claude-default",
                provider=self.provider,
                command=self.command,
                provider_profile="local-default",
            ),
        )

    def probe(self, executable_prefix: list[str] | None) -> bool:
        return bool(executable_prefix)

    def new_parser(self) -> AgentEventParser:
        return ClaudeJsonlParser()

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
        if profile.provider != self.provider:
            raise ValueError("Agent profile does not belong to the Claude Code adapter")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty text")
        if "\x00" in prompt:
            raise ValueError("prompt cannot contain NUL bytes")
        if len(prompt) > MAX_AGENT_PROMPT_CHARS:
            raise ValueError(f"prompt is limited to {MAX_AGENT_PROMPT_CHARS} characters")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("cwd must be text")
        profile.validate_sandbox(sandbox)
        if native_session_id is not None and (
            not isinstance(native_session_id, str)
            or not _NATIVE_SESSION_ID.fullmatch(native_session_id)
        ):
            raise ValueError("native_session_id is invalid")
        permission_mode, tools = self._SANDBOX_POLICY[sandbox]
        command = [
            *executable_prefix,
            "-p",
            # --tools accepts a variadic list.  If the prompt is appended at
            # the end, Claude consumes it as another tool name and reports
            # that --print received no input.  Bind the prompt directly to
            # -p before adding any variadic options.
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--safe-mode",
            "--restricted",
            "--no-chrome",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--permission-mode",
            permission_mode,
            "--tools",
            tools,
        ]
        if native_session_id:
            command.extend(("--resume", native_session_id))
        return command
