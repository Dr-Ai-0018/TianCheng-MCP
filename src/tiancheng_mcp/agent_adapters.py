"""Provider-neutral local agent adapter contracts and the Codex adapter.

Adapters are server-owned. Callers select only a registered profile and may
provide provider-specific, schema-validated invocation options. They cannot
provide executable paths, pre-built argv, parsers, or arbitrary environment
maps. This keeps native CLI controls behind the same lifecycle boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import re
import time
from typing import Any, Mapping, Protocol, TypedDict, runtime_checkable

from .agent_preflight import validate_windows_home_preflight


MAX_AGENT_PROMPT_CHARS = 32_000
MAX_EVENT_SUMMARY_BYTES = 8 * 1024
MAX_EVENT_DATA_BYTES = 16 * 1024
_ALLOWED_SANDBOXES = frozenset({"read-only", "workspace-write"})
_NATIVE_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_CODEX_CONFIG_KEY = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
_CODEX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_CODEX_THREAD_SOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CODEX_PROTECTED_CONFIG_ROOTS = frozenset(
    {
        "approval_policy",
        "approvals_reviewer",
        "default_permissions",
        "include_permissions_instructions",
        "permissions",
        "windows",
        "hooks",
        "notify",
        "mcp_servers",
        "model_providers",
        "plugins",
        "profiles",
        "sandbox_mode",
        "sandbox_workspace_write",
        "shell_environment_policy",
    }
)
# These features select a sandbox backend or change permission/approval tools.
# Reserve both current names and schema-listed compatibility names to the host.
_CODEX_PROTECTED_FEATURES = frozenset(
    {
        "elevated_windows_sandbox",
        "enable_experimental_windows_sandbox",
        "experimental_windows_sandbox",
        "windows_sandbox_service",
        "use_linux_sandbox_bwrap",
        "exec_permission_approvals",
        "request_permissions",
        "request_permissions_tool",
        "guardian_approval",
        "write_stdin_approval",
        "codex_hooks",
        "hooks",
        "plugin_hooks",
    }
)
_CODEX_SECRET_CONFIG_SEGMENTS = frozenset(
    {
        "api_key",
        "api-key",
        "credential",
        "env_http_headers",
        "experimental_bearer_token",
        "http_headers",
        "password",
        "secret",
        "token",
    }
)
_CODEX_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
_CODEX_OPTION_KEYS = frozenset(
    {
        "model",
        "reasoning_effort",
        "model_provider",
        "route",
        "config",
        "enable",
        "disable",
        "strict_config",
        "images",
        "oss",
        "local_provider",
        "ask_for_approval",
        "search",
        "approve_for_me",
        "manual_approval",
        "add_dirs",
        "dangerously_bypass_approvals_and_sandbox",
        "dangerously_bypass_hook_trust",
        "thread_source",
        "skip_git_repo_check",
        "ephemeral",
        "ignore_user_config",
        "ignore_rules",
        "output_schema",
        "color",
        "output_last_message",
        "review_uncommitted",
        "review_base",
        "review_commit",
        "review_title",
    }
)
_SECRET_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+"), r"\1<redacted>"),
    (
        re.compile(r"(?i)((?:token|key|secret|password)\s*[=:]\s*)[^\s,;]+"),
        r"\1<redacted>",
    ),
    (re.compile(r"(?i)sk-[A-Za-z0-9_-]{8,}"), "<redacted>"),
    (re.compile(r"(?i)github_pat_[A-Za-z0-9_]{8,}"), "<redacted>"),
    (re.compile(r"(?i)gh[pousr]_[A-Za-z0-9]{8,}"), "<redacted>"),
    (re.compile(r"(?i)glpat-[A-Za-z0-9_-]{8,}"), "<redacted>"),
    (re.compile(r"(?i)xox[baprs]-[A-Za-z0-9-]{8,}"), "<redacted>"),
)


class CodexOptionsInput(TypedDict, total=False):
    """MCP-visible Codex invocation controls.

    Fields may be null on an ``agent_run`` override to remove a session
    default. The service normalizes this object before it reaches the adapter.
    """

    model: str | None
    reasoning_effort: str | None
    model_provider: str | None
    route: str | None
    config: list[str] | None
    enable: list[str] | None
    disable: list[str] | None
    strict_config: bool | None
    images: list[str] | None
    oss: bool | None
    local_provider: str | None
    ask_for_approval: str | None
    search: bool | None
    approve_for_me: bool | None
    manual_approval: bool | None
    add_dirs: list[str] | None
    dangerously_bypass_approvals_and_sandbox: bool | None
    dangerously_bypass_hook_trust: bool | None
    thread_source: str | None
    skip_git_repo_check: bool | None
    ephemeral: bool | None
    ignore_user_config: bool | None
    ignore_rules: bool | None
    output_schema: str | None
    color: str | None
    output_last_message: str | None
    review_uncommitted: bool | None
    review_base: str | None
    review_commit: str | None
    review_title: str | None


def _bounded_text(value: object, maximum: int) -> tuple[str, bool]:
    text = value if isinstance(value, str) else str(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return text, False
    clipped = encoded[:maximum].decode("utf-8", errors="ignore")
    return clipped, True


def redact_text(value: object, maximum: int = MAX_EVENT_SUMMARY_BYTES) -> tuple[str, bool]:
    text = value if isinstance(value, str) else str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return _bounded_text(text, maximum)


def _codex_text(value: object, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    if "\x00" in value:
        raise ValueError(f"{label} cannot contain NUL bytes")
    if len(value) > maximum:
        raise ValueError(f"{label} is limited to {maximum} characters")
    return value


def _codex_sequence(
    value: object,
    label: str,
    *,
    maximum_items: int = 32,
    maximum_chars: int = 4096,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list of strings")
    if len(value) > maximum_items:
        raise ValueError(f"{label} is limited to {maximum_items} entries")
    return tuple(
        _codex_text(item, f"{label} entry", maximum=maximum_chars) for item in value
    )


def _codex_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean")
    return value


def _append_codex_prompt(command: list[str], prompt: str) -> None:
    if prompt.startswith("-"):
        command.append("--")
    command.append(prompt)


def _validate_codex_config_key(key: str) -> None:
    segments = tuple(part.casefold() for part in key.split("."))
    if segments[0] in _CODEX_PROTECTED_CONFIG_ROOTS:
        raise PermissionError(
            f"Codex config key {key} requires a separate server-side policy"
        )
    if segments[0] == "features" and (
        len(segments) == 1 or segments[1] in _CODEX_PROTECTED_FEATURES
    ):
        # Whole-table assignments could hide a protected key in an inline table.
        raise PermissionError(
            f"Codex config key {key} requires a separate server-side policy"
        )
    if any(segment in _CODEX_SECRET_CONFIG_SEGMENTS for segment in segments):
        raise PermissionError(
            f"Codex config key {key} may expose credentials or environment data"
        )


def normalize_codex_options(
    value: Mapping[str, Any] | None,
    *,
    allow_null: bool = False,
) -> dict[str, Any]:
    """Validate a Codex options object while preserving explicit field order.

    ``allow_null`` is used for per-run overrides, where null removes a session
    default. The returned mapping contains only fields supplied by the caller.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("codex options must be an object")
    unknown = sorted(set(value) - _CODEX_OPTION_KEYS)
    if unknown:
        raise ValueError("Unknown Codex option fields: " + ", ".join(unknown))
    normalized: dict[str, Any] = {}
    booleans = {
        "strict_config",
        "oss",
        "search",
        "approve_for_me",
        "manual_approval",
        "dangerously_bypass_approvals_and_sandbox",
        "dangerously_bypass_hook_trust",
        "skip_git_repo_check",
        "ephemeral",
        "ignore_user_config",
        "ignore_rules",
        "review_uncommitted",
    }
    sequences = {"config", "enable", "disable", "images", "add_dirs"}
    for key, raw in value.items():
        if raw is None:
            if not allow_null:
                raise ValueError(f"Codex option {key} cannot be null")
            normalized[key] = None
            continue
        if key in booleans:
            enabled = _codex_bool(raw, key)
            if enabled and key in {"ignore_rules", "ignore_user_config"}:
                raise PermissionError(
                    f"Codex option {key} requires a separate server-side policy"
                )
            normalized[key] = enabled
        elif key in sequences:
            maximum_items = 64 if key == "config" else 32
            maximum_chars = 8192 if key == "config" else 4096
            items = _codex_sequence(
                raw,
                key,
                maximum_items=maximum_items,
                maximum_chars=maximum_chars,
            )
            if key == "config":
                for item in items:
                    config_key, separator, _config_value = item.partition("=")
                    if not separator or not _CODEX_CONFIG_KEY.fullmatch(config_key):
                        raise ValueError(
                            "config entries must use a dotted key=value form"
                        )
                    _validate_codex_config_key(config_key)
            if key in {"enable", "disable"} and any(
                not _CODEX_NAME.fullmatch(item) for item in items
            ):
                raise ValueError(f"{key} entries contain an invalid feature name")
            if key in {"enable", "disable"} and any(
                item.casefold().split(".", 1)[0] in _CODEX_PROTECTED_FEATURES
                for item in items
            ):
                raise PermissionError(
                    "Codex security feature changes require a separate server-side policy"
                )
            normalized[key] = items
        elif key == "local_provider":
            provider = _codex_text(raw, key, maximum=32)
            if provider not in {"lmstudio", "ollama"}:
                raise ValueError("local_provider must be lmstudio or ollama")
            normalized[key] = provider
        elif key == "ask_for_approval":
            policy = _codex_text(raw, key, maximum=32)
            if policy not in {"on-request", "never"}:
                raise ValueError("ask_for_approval must be on-request or never")
            normalized[key] = policy
        elif key == "color":
            color = _codex_text(raw, key, maximum=16)
            if color not in {"always", "never", "auto"}:
                raise ValueError("color must be always, never, or auto")
            normalized[key] = color
        elif key == "reasoning_effort":
            effort = _codex_text(raw, key, maximum=16)
            if effort not in _CODEX_REASONING_EFFORTS:
                raise ValueError(
                    "reasoning_effort must be one of: "
                    + ", ".join(sorted(_CODEX_REASONING_EFFORTS))
                )
            normalized[key] = effort
        elif key == "thread_source":
            source = _codex_text(raw, key, maximum=128)
            if not _CODEX_THREAD_SOURCE.fullmatch(source):
                raise ValueError("thread_source contains invalid characters")
            if source != "tiancheng-mcp":
                raise PermissionError(
                    "thread_source is server-owned and must be tiancheng-mcp"
                )
            normalized[key] = source
        elif key in {"model", "model_provider"}:
            # These are opaque native Codex identifiers. They are separate
            # argv values (never shell text), so avoid rejecting future model
            # or registered-provider naming schemes TianCheng does not know.
            normalized[key] = _codex_text(raw, key, maximum=256)
        elif key == "route":
            route = _codex_text(raw, key, maximum=256)
            if "\r" in route or "\n" in route:
                raise ValueError("route cannot contain line breaks")
            normalized[key] = route
        else:
            normalized[key] = _codex_text(raw, key)
    return normalized


def merge_codex_options(
    defaults: Mapping[str, Any] | None,
    overrides: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = normalize_codex_options(defaults)
    for key, value in normalize_codex_options(overrides, allow_null=True).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def summarize_codex_options(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a bounded, non-secret summary suitable for MCP payloads."""

    options = normalize_codex_options(value)
    summary: dict[str, Any] = {}
    for key in (
        "model",
        "reasoning_effort",
        "model_provider",
        "strict_config",
        "oss",
        "local_provider",
        "ask_for_approval",
        "search",
        "approve_for_me",
        "thread_source",
        "manual_approval",
        "skip_git_repo_check",
        "ephemeral",
        "ignore_user_config",
        "ignore_rules",
        "color",
    ):
        if key in options:
            summary[key] = options[key]
    if "route" in options:
        summary["route_configured"] = True
    for key in ("config", "enable", "disable", "images", "add_dirs"):
        if key in options:
            summary[f"{key}_count"] = len(options[key])
    for key in ("output_schema", "output_last_message"):
        if key in options:
            summary[f"{key}_configured"] = True
    for key in ("review_base", "review_commit", "review_title"):
        if key in options:
            summary[f"{key}_configured"] = True
    if "review_uncommitted" in options:
        summary["review_uncommitted"] = options["review_uncommitted"]
    return summary


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
    manual_approval: bool = False
    fork: bool = False
    review: bool = False

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
            "manual_approval": self.manual_approval,
            "fork": self.fork,
            "review": self.review,
        }


@dataclass(frozen=True)
class AgentProfile:
    name: str
    provider: str
    command: str
    provider_profile: str
    # Server-owned runtime isolation.  This is deliberately not part of the
    # MCP session/run input schema, so callers cannot redirect one named agent
    # into another agent's Codex state.
    codex_home: str | None = None
    # One provider credential may be bound to this server-owned profile.  The
    # value is resolved privately by the service and only injected into this
    # profile's child process; it is never supplied by an MCP caller.
    credential_env: str | None = None
    # Authentication selection is server-owned. ``existing-login`` reuses
    # only the launcher's safe login-state environment; ``env`` injects the
    # single named credential above.
    auth_mode: str = "existing-login"
    allowed_sandboxes: frozenset[str] = _ALLOWED_SANDBOXES
    # Default hard lifetime for one Agent run. MCP callers may request a
    # different per-run value, but the service enforces a server-owned ceiling.
    max_runtime_seconds: int = 3600
    max_output_bytes: int = 512 * 1024

    # Opt-in legacy Windows state screening, not the effective native backend.
    windows_home_preflight: str = "none"

    def __post_init__(self) -> None:
        validate_windows_home_preflight(
            self.windows_home_preflight, provider=self.provider, codex_home=self.codex_home,
        )

    @property
    def codex_config_profile(self) -> str:
        """The Codex CLI ``-p`` profile, distinct from this MCP profile name."""

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
        invocation_options: Mapping[str, Any] | None = None,
        action: str = "continue",
    ) -> list[str]: ...


class CodexJsonlParser:
    """Normalize Codex JSONL while retaining only bounded event data."""

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
            data = {"native_session_id": self.native_session_id} if self.native_session_id else {}
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
            elif item_type == "command_execution":
                normalized_type = "command_completed"
                code = item.get("exit_code")
                code = code if type(code) is int else None
                status = item.get("status")
                status = status if isinstance(status, str) and status in {"completed", "failed", "in_progress"} else "unknown"
                data = {"item_type": item_type, "exit_code": code, "status": status}
                summary_value = f"Command completed: status={status}, exit_code={code}"
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
        attach=True,
        resume=True,
        stream=True,
        cancel=True,
        fork=True,
        review=True,
        manual_approval=True,
    )

    def profiles(self) -> tuple[AgentProfile, ...]:
        return (
            AgentProfile(
                name="codex-default",
                provider=self.provider,
                command=self.command,
                provider_profile="",
                auth_mode="existing-login",
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
        invocation_options: Mapping[str, Any] | None = None,
        action: str = "continue",
    ) -> list[str]:
        if profile.provider != self.provider:
            raise ValueError("Agent profile does not belong to the Codex adapter")
        if action not in {"continue", "fork", "review"}:
            raise ValueError("Codex action must be continue, fork, or review")
        if not isinstance(prompt, str) or (action == "continue" and not prompt.strip()):
            raise ValueError("prompt must be non-empty text")
        if prompt == "-":
            raise ValueError("prompt='-' would read from the closed stdin stream")
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
        options = normalize_codex_options(invocation_options)
        review_keys = {
            "review_uncommitted",
            "review_base",
            "review_commit",
            "review_title",
        }
        if review_keys & options.keys() and action != "review":
            raise ValueError("review options require the review Codex action")
        review_targets = sum(
            bool(options.get(key))
            for key in ("review_uncommitted", "review_base", "review_commit")
        )
        if review_targets > 1:
            raise ValueError("review target options are mutually exclusive")
        if action == "fork" and native_session_id is None:
            raise ValueError("fork requires a bound native session id")
        if options.get("dangerously_bypass_approvals_and_sandbox"):
            raise PermissionError(
                "dangerously_bypass_approvals_and_sandbox requires a separate "
                "externally isolated execution policy"
            )
        if options.get("dangerously_bypass_hook_trust"):
            raise PermissionError(
                "dangerously_bypass_hook_trust is not available through remote Agent runs"
            )
        if options.get("approve_for_me") and sandbox != "workspace-write":
            raise ValueError("approve_for_me requires the workspace-write sandbox")
        if options.get("manual_approval"):
            raise ValueError("manual_approval requires the app-server transport")
        if options.get("approve_for_me") and options.get("ask_for_approval") == "never":
            raise ValueError("approve_for_me conflicts with ask_for_approval=never")
        if options.get("color") == "always":
            raise ValueError("color=always is incompatible with the JSONL parser")
        if options.get("ephemeral") and native_session_id and action == "continue":
            raise ValueError("ephemeral cannot be used when resuming a session")
        enabled = set(options.get("enable", ()))
        disabled = set(options.get("disable", ()))
        conflicts = sorted(enabled & disabled)
        if conflicts:
            raise ValueError(
                "A feature cannot be both enabled and disabled: "
                + ", ".join(conflicts)
            )

        command = [*executable_prefix]
        # Root -a is an interactive option, not inherited by exec. Pass the
        # typed request through exec's config layer without changing reviewer.
        if options.get("search"):
            command.append("--search")
        command.extend(("exec", "--json"))
        # --approve-for-me selects workspace-write itself and conflicts with -s.
        if not options.get("approve_for_me"):
            command.extend(("-s", sandbox))
        if "ask_for_approval" in options:
            command.extend(
                ("-c", "approval_policy=" + json.dumps(options["ask_for_approval"]))
            )
        if profile.codex_config_profile:
            command.extend(("-p", profile.codex_config_profile))
        command.extend(("-C", cwd))
        if "model" in options:
            command.extend(("-m", options["model"]))
        if "reasoning_effort" in options:
            command.extend(
                (
                    "-c",
                    "model_reasoning_effort="
                    + json.dumps(options["reasoning_effort"], ensure_ascii=False),
                )
            )
        if "model_provider" in options:
            command.extend(
                (
                    "-c",
                    "model_provider="
                    + json.dumps(options["model_provider"], ensure_ascii=False),
                )
            )
        for config in options.get("config", ()):
            command.extend(("-c", config))
        for feature in options.get("enable", ()):
            command.extend(("--enable", feature))
        for feature in options.get("disable", ()):
            command.extend(("--disable", feature))
        if options.get("strict_config"):
            command.append("--strict-config")
        for image in options.get("images", ()):
            command.extend(("-i", image))
        if options.get("oss"):
            command.append("--oss")
        if "local_provider" in options:
            command.extend(("--local-provider", options["local_provider"]))
        if options.get("approve_for_me"):
            command.append("--approve-for-me")
        for directory in options.get("add_dirs", ()):
            command.extend(("--add-dir", directory))
        if "thread_source" in options:
            command.extend(("--thread-source", options["thread_source"]))
        if options.get("skip_git_repo_check"):
            command.append("--skip-git-repo-check")
        if options.get("ephemeral"):
            command.append("--ephemeral")
        if options.get("ignore_user_config"):
            command.append("--ignore-user-config")
        if options.get("ignore_rules"):
            command.append("--ignore-rules")
        if "output_schema" in options:
            command.extend(("--output-schema", options["output_schema"]))
        if "color" in options:
            command.extend(("--color", options["color"]))
        if "output_last_message" in options:
            command.extend(("-o", options["output_last_message"]))
        if action == "fork":
            command.extend(("fork", str(native_session_id)))
            if prompt:
                _append_codex_prompt(command, prompt)
        elif action == "review":
            command.append("review")
            if options.get("review_uncommitted"):
                command.append("--uncommitted")
            if "review_base" in options:
                command.extend(("--base", options["review_base"]))
            if "review_commit" in options:
                command.extend(("--commit", options["review_commit"]))
            if "review_title" in options:
                command.extend(("--title", options["review_title"]))
            if prompt:
                _append_codex_prompt(command, prompt)
        elif native_session_id:
            command.extend(("resume", native_session_id))
            _append_codex_prompt(command, prompt)
        else:
            _append_codex_prompt(command, prompt)
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
        invocation_options: Mapping[str, Any] | None = None,
        action: str = "continue",
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
        if invocation_options:
            raise NotImplementedError(
                "Claude Code invocation options are not part of the Codex-first release"
            )
        if action != "continue":
            raise NotImplementedError(
                "Claude Code actions are not part of the Codex-first release"
            )
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
