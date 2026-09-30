"""Opt-in Pi Coding Agent adapter with no file or command tools."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .agent_adapters import (
    AdapterCapabilities,
    AgentProfile,
    MAX_AGENT_PROMPT_CHARS,
    MAX_EVENT_DATA_BYTES,
    NormalizedEvent,
    redact_text,
)

_PROVIDER = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


class PiJsonlParser:
    """Keep bounded public events; never forward raw Pi messages or tool data."""

    def __init__(self) -> None:
        self.next_seq = 0
        # The first release uses --no-session, so there is no resumable ID.
        self.native_session_id: str | None = None
        self.final_message: str | None = None
        self.done = False
        self.policy_violation = False
        self.reported_error = False
        self.update_count = 0

    def feed_line(self, line: str) -> NormalizedEvent | None:
        if not isinstance(line, str) or not line.strip():
            return None
        if len(line) > 256 * 1024:
            self.policy_violation = True
            return self.synthetic_event("error", "Pi protocol line exceeded limit")
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        kind = raw.get("type")
        if kind == "session":
            return self.synthetic_event("thread_started", "Pi run started")
        if kind == "message_update":
            # Keep the caller informed during long generations without
            # forwarding text deltas, thinking, or provider payloads.
            self.update_count += 1
            if self.update_count % 32 == 0:
                return self.synthetic_event("status", "Pi response is still streaming")
            return None
        if kind in {"tool_execution_start", "tool_execution_end"}:
            self.policy_violation = True
            return self.synthetic_event("error", "Pi tool execution is disabled")
        if kind == "message_end":
            message = raw.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                return None
            content = message.get("content")
            if not isinstance(content, list):
                return None
            if any(
                isinstance(block, dict) and block.get("type") == "toolCall"
                for block in content
            ):
                self.policy_violation = True
                return self.synthetic_event("error", "Pi tool calls are disabled")
            text = "\n".join(
                block["text"] for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )
            if text:
                safe, clipped = redact_text(text, MAX_EVENT_DATA_BYTES)
                self.final_message = safe
                event = self.synthetic_event("agent_message", safe, {"text": safe})
                if clipped:
                    return NormalizedEvent(
                        event.seq, event.type, event.summary, event.data, True,
                        event.created_epoch,
                    )
                return event
            return None
        if kind == "agent_end":
            self.done = True
            return self.synthetic_event("completed", "Pi run completed")
        if kind == "error":
            self.reported_error = True
            return self.synthetic_event("error", "Pi reported an error")
        return None

    def synthetic_event(
        self, event_type: str, summary_value: object,
        data: dict[str, Any] | None = None,
    ) -> NormalizedEvent:
        summary, summary_clipped = redact_text(summary_value)
        safe_data: dict[str, Any] = {}
        data_clipped = False
        for key, value in (data or {}).items():
            safe_value, clipped = redact_text(value, MAX_EVENT_DATA_BYTES)
            safe_data[str(key)] = safe_value
            data_clipped |= clipped
        event = NormalizedEvent(
            self.next_seq, event_type, summary, safe_data,
            summary_clipped or data_clipped,
        )
        self.next_seq += 1
        return event


class PiAdapter:
    provider = "pi"
    display_name = "Pi Coding Agent"
    command = "pi"
    capabilities = AdapterCapabilities()
    tested_cli_version = "0.85.1"

    def profiles(self) -> tuple[AgentProfile, ...]:
        # Pi has no implicit model or credential. A local profile is required.
        return ()

    def probe(self, executable_prefix: list[str] | None) -> bool:
        return bool(executable_prefix)

    def new_parser(self) -> PiJsonlParser:
        return PiJsonlParser()

    def build_command(
        self, profile: AgentProfile, executable_prefix: list[str], *,
        prompt: str, cwd: str, sandbox: str,
        native_session_id: str | None = None,
        invocation_options: Mapping[str, Any] | None = None,
        action: str = "continue",
    ) -> list[str]:
        if profile.provider != self.provider:
            raise ValueError("Agent profile does not belong to the Pi adapter")
        if not isinstance(prompt, str) or not prompt.strip() or prompt == "-":
            raise ValueError("prompt must be non-empty text")
        if "\x00" in prompt or len(prompt) > MAX_AGENT_PROMPT_CHARS:
            raise ValueError("prompt is invalid or too long")
        if not isinstance(cwd, str) or not cwd:
            raise ValueError("cwd must be text")
        profile.validate_sandbox(sandbox)
        if native_session_id is not None:
            raise NotImplementedError("Pi resume is not available yet")
        if invocation_options:
            raise NotImplementedError("Pi invocation options are not available")
        if action != "continue":
            raise NotImplementedError("Pi supports continue action only")
        if not profile.pi_provider or not _PROVIDER.fullmatch(profile.pi_provider):
            raise ValueError("Pi provider is invalid")
        if not profile.pi_model or not _MODEL.fullmatch(profile.pi_model):
            raise ValueError("Pi model is invalid")
        return [
            *executable_prefix,
            "--provider", profile.pi_provider,
            "--model", profile.pi_model,
            "--mode", "json",
            "--print", prompt,
            "--no-session",
            "--no-extensions",
            "--no-approve",
            "--no-context-files",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-tools",
        ]
