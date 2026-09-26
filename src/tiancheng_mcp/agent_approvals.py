"""Bounded app-server protocol for explicitly selected manual approvals.

No generic RPC endpoint is exposed. The service owns process launch, environment,
working directory, lifecycle and session binding; this object owns one turn.
"""
from __future__ import annotations

import codecs
import json
from pathlib import Path
import re
import time
import tomllib
import uuid
from typing import Any

from .agent_adapters import CodexJsonlParser, redact_text

MAX_APPROVAL_BYTES = 16_384
APPROVAL_TTL_SECONDS = 300
_METHODS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
_PROFILE_KEYS = {
    "model", "model_provider", "model_providers", "model_reasoning_effort",
    "approval_policy", "approvals_reviewer", "sandbox_mode", "windows",
    "model_instructions_file", "developer_instructions",
}


def load_manual_profile(home: Path, name: str) -> dict[str, Any]:
    """Load only the server-bound profile. Never silently drop its settings."""
    if name == "":
        return {}  # Native app-server loads the user's base config itself.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", name):
        raise ValueError("Manual approval requires a valid native profile name")
    path = home / (name + ".config.toml")
    if path.stat().st_size > 128 * 1024:
        raise ValueError("Native profile exceeds manual transport limit")
    config = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    if set(config) - _PROFILE_KEYS:
        raise ValueError("Native profile has settings unsupported by manual transport")
    # RPC config maps cannot represent TOML date/time values.
    json.dumps(config)
    if "model_instructions_file" in config:
        instructions = config["model_instructions_file"]
        if not isinstance(instructions, str) or not Path(instructions).is_absolute():
            raise ValueError("Manual transport requires an absolute model instructions path")
    return config


class ManualApprovalParser(CodexJsonlParser):
    def __init__(self, *, prompt: str, cwd: str, sandbox: str,
                 config: dict[str, Any], options: dict[str, Any], native_id: str | None):
        super().__init__()
        options = {k: v for k, v in options.items() if not (k in {"images", "add_dirs"} and not v)}
        supported = {"manual_approval", "ask_for_approval", "approve_for_me",
                     "skip_git_repo_check", "model", "reasoning_effort", "route"}
        if set(options) - supported:
            raise ValueError("Options unsupported by manual approval transport")
        if options.get("approve_for_me") or options.get("ask_for_approval") == "never":
            raise ValueError("Manual approval conflicts with automatic or never approval")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 64 * 1024:
            raise ValueError("Manual prompt must be nonempty and at most 64 KiB")
        self.prompt, self.cwd, self.sandbox = prompt, cwd, sandbox
        self.config = {**config, "approval_policy": "on-request", "approvals_reviewer": "user", "sandbox_mode": sandbox}
        self.options, self.resume_id = options, native_id
        self.turn_id: str | None = None
        self.pending: dict[str, dict[str, Any]] = {}
        self.seen_requests: set[str] = set()
        self.items: dict[str, dict[str, Any]] = {}
        self.outgoing: list[dict[str, Any]] = []
        self.done: str | None = None
        self.failure_message: str | None = None
        self.stdin_closed = False
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.stage = "initialize"
        self.outgoing.append({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "tiancheng_mcp", "version": "0.11.0"},
            "capabilities": {"experimentalApi": True},
        }})

    def fail(self, message: str):
        self.done = "failed"
        self.failure_message = redact_text(message)[0]
        self.pending.clear()
        return self.synthetic_event("error", message)

    def decode_chunk(self, chunk: bytes) -> str:
        try:
            return self.decoder.decode(chunk)
        except UnicodeError:
            self.fail("Invalid UTF-8 in approval protocol")
            return ""

    def _reply(self, rpc_id: Any, decision: str) -> None:
        self.outgoing.append({"id": rpc_id, "result": {"decision": decision}})

    def expire(self) -> None:
        for key, request in list(self.pending.items()):
            if time.monotonic() >= request["deadline"]:
                self._reply(request["rpc_id"], "cancel")
                del self.pending[key]

    def list_pending(self) -> list[dict[str, Any]]:
        self.expire()
        return [dict(r["public"]) for r in self.pending.values()]

    def respond(self, approval_id: str, decision: str) -> dict[str, Any]:
        self.expire()
        if decision not in {"accept", "decline", "cancel"}:
            raise ValueError("decision must be accept, decline, or cancel")
        if self.done or approval_id not in self.pending:
            raise FileNotFoundError("Approval expired, resolved, or not in this run")
        request = self.pending.pop(approval_id)
        if decision not in request["public"]["allowed_decisions"]:
            self.pending[approval_id] = request
            raise ValueError("Decision is not offered by this native request")
        self._reply(request["rpc_id"], decision)
        return {"approval_id": approval_id, "decision": decision, "status": "submitted"}

    def feed_line(self, line: str):
        if not line.strip():
            return None
        if len(line.encode()) > 256 * 1024:
            return self.fail("App-server message exceeds protocol limit")
        try:
            msg = json.loads(line)
        except ValueError:
            return self.fail("Invalid app-server JSON")
        if not isinstance(msg, dict):
            return self.fail("Invalid app-server message")
        if self.done:
            return None
        method, params = msg.get("method"), msg.get("params", {})
        if not isinstance(params, dict):
            return self.fail("Invalid app-server params")
        if method and "id" in msg:
            return self._approval(msg)
        if "id" in msg:
            if "error" in msg:
                return self.fail("App-server request failed: " + str(msg["error"]))
            result = msg.get("result", {})
            if not isinstance(result, dict):
                return self.fail("Invalid app-server result")
            if msg["id"] == 1 and self.stage == "initialize":
                self.stage = "thread"
                self.outgoing.append({"method": "initialized", "params": {}})
                p = {"cwd": self.cwd, "sandbox": self.sandbox, "approvalPolicy": "on-request",
                     "approvalsReviewer": "user", "config": self.config}
                if self.options.get("model"):
                    p["model"] = self.options["model"]
                if self.config.get("model_provider"):
                    p["modelProvider"] = self.config["model_provider"]
                if self.resume_id:
                    p.update(threadId=self.resume_id, excludeTurns=True)
                self.outgoing.append({"id": 2, "method": "thread/resume" if self.resume_id else "thread/start", "params": p})
            elif msg["id"] == 2 and self.stage == "thread":
                thread = result.get("thread", {})
                native = thread.get("id")
                if not isinstance(native, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", native):
                    return self.fail("Invalid thread binding")
                if self.resume_id and native != self.resume_id:
                    return self.fail("Resumed thread binding changed")
                expected = self.config.get("model_provider")
                if expected and thread.get("modelProvider") != expected:
                    return self.fail("Native provider does not match bound profile")
                self.native_session_id = native
                self.stage = "turn"
                p = {"threadId": native, "input": [{"type": "text", "text": self.prompt}],
                     "approvalPolicy": "on-request", "approvalsReviewer": "user"}
                if self.options.get("reasoning_effort"):
                    p["effort"] = self.options["reasoning_effort"]
                self.outgoing.append({"id": 3, "method": "turn/start", "params": p})
                return self.synthetic_event("thread_started", "Manual approval thread started", {"native_session_id": native})
            elif msg["id"] == 3 and self.stage == "turn":
                self.turn_id = result.get("turn", {}).get("id")
            return None
        if method == "turn/started":
            if params.get("threadId") != self.native_session_id:
                return self.fail("Turn thread binding mismatch")
            self.turn_id = params.get("turn", {}).get("id")
        elif method == "serverRequest/resolved":
            for key, r in list(self.pending.items()):
                if r["rpc_id"] == params.get("requestId"):
                    del self.pending[key]
        elif method in {"item/started", "item/completed"}:
            item = params.get("item", {})
            if params.get("threadId") != self.native_session_id or params.get("turnId") != self.turn_id:
                return self.fail("Item binding mismatch")
            if method == "item/started" and item.get("type") in {"fileChange", "commandExecution"}:
                if len(self.items) >= 32:
                    return self.fail("Too many concurrent native items")
                if len(json.dumps(item).encode()) <= MAX_APPROVAL_BYTES:
                    self.items[item["id"]] = item
            if method == "item/completed":
                self.items.pop(item.get("id"), None)
                if item.get("type") == "agentMessage":
                    return super().feed_line(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": item.get("text", "")}}))
                if item.get("type") == "commandExecution":
                    return super().feed_line(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "status": item.get("status"), "exit_code": item.get("exitCode")}}))
        elif method == "turn/completed":
            turn = params.get("turn", {})
            if params.get("threadId") != self.native_session_id or turn.get("id") != self.turn_id:
                return self.fail("Completed turn binding mismatch")
            self.done = {"completed": "succeeded", "interrupted": "cancelled"}.get(turn.get("status"), "failed")
            self.pending.clear()
            return self.synthetic_event("error" if self.done == "failed" else "status", "Manual turn " + str(turn.get("status")))
        elif method == "error":
            return self.fail(str(params.get("error", "App-server error")))
        return None

    def _approval(self, msg: dict[str, Any]):
        method, p, rpc_id = msg["method"], msg["params"], msg["id"]
        if type(rpc_id) not in {str, int} or len(str(rpc_id)) > 128:
            return self.fail("Invalid native request id")
        key = json.dumps(rpc_id)
        if key in self.seen_requests or len(self.seen_requests) >= 256:
            return self.fail("Duplicate or excessive native requests")
        self.seen_requests.add(key)
        if method not in _METHODS:
            self.outgoing.append({"id": rpc_id, "error": {"code": -32601, "message": "Unsupported interactive request"}})
            return self.synthetic_event("status", "Unsupported interactive request rejected")
        if p.get("threadId") != self.native_session_id or not self.turn_id or p.get("turnId") != self.turn_id:
            return self.fail("Approval binding mismatch")
        if len(self.pending) >= 16:
            self._reply(rpc_id, "cancel")
            return self.synthetic_event("status", "Approval capacity reached; cancelled")
        details = {k: p[k] for k in ("command", "cwd", "reason", "networkApprovalContext", "additionalPermissions", "grantRoot", "kind") if k in p}
        if method == "item/fileChange/requestApproval":
            details["changes"] = self.items.get(p.get("itemId"), {}).get("changes")
        encoded = json.dumps(details, ensure_ascii=False)
        safe, clipped = redact_text(encoded, MAX_APPROVAL_BYTES)
        missing_details = (
            method == "item/fileChange/requestApproval" and not details.get("changes")
        ) or (
            method == "item/commandExecution/requestApproval"
            and not details.get("command") and not details.get("networkApprovalContext")
        )
        if clipped or safe != encoded or "\ufffd" in encoded or details.get("grantRoot") or missing_details:
            self._reply(rpc_id, "cancel")
            return self.synthetic_event("status", "Approval details unavailable or unsafe to display; cancelled")
        decisions = ["accept", "decline", "cancel"]
        if isinstance(p.get("availableDecisions"), list):
            decisions = [d for d in decisions if d in p["availableDecisions"]]
        approval_id = "approval_" + uuid.uuid4().hex
        public = {"approval_id": approval_id, "method": method, "thread_id": self.native_session_id,
                  "turn_id": self.turn_id, "item_id": p.get("itemId"), "details": details,
                  "allowed_decisions": decisions, "expires_at_epoch": time.time() + APPROVAL_TTL_SECONDS}
        self.pending[approval_id] = {"rpc_id": rpc_id, "deadline": time.monotonic() + APPROVAL_TTL_SECONDS, "public": public}
        return self.synthetic_event("approval_requested", "User decision required", {"approval_id": approval_id})
