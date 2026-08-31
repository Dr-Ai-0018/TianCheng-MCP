"""Ephemeral, chat-approved grants for workspace-external directories.

The normal WorkspaceJail remains unchanged. A grant is an in-memory capability
that must be explicitly requested and approved with a one-time non-secret
challenge plus an explicit user-confirmation phrase.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .security import FILE_ATTRIBUTE_REPARSE_POINT, WorkspaceSecurityError
from .policy import AccessPolicy, AccessPolicyError


MAX_GRANT_TTL = 600
MAX_PENDING_TTL = 120
MAX_ACTIVE_GRANTS = 3
_ALLOWED_MODES = frozenset({"read", "write", "delete", "exec"})


def _validate_component(component: str) -> None:
    if component in {"", "."}:
        return
    if component == ".." or "\x00" in component or ":" in component:
        raise WorkspaceSecurityError("Unsafe component in external grant path")
    if component.endswith((" ", ".")) or any(ord(ch) < 32 for ch in component):
        raise WorkspaceSecurityError("Unsafe component in external grant path")


def _is_reparse(path: Path) -> bool:
    try:
        stat_result = path.lstat()
    except OSError as exc:
        raise WorkspaceSecurityError(f"Cannot inspect path safely: {path}") from exc
    return path.is_symlink() or bool(getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _reject_reparse_components(root: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise WorkspaceSecurityError("Resolved path escapes the grant root") from exc
    current = root
    if _is_reparse(current):
        raise WorkspaceSecurityError("Grant root cannot be a symlink or reparse point")
    for part in relative.parts:
        current = current / part
        if os.path.lexists(current) and _is_reparse(current):
            raise WorkspaceSecurityError(f"Symlink, junction, or reparse point is not allowed: {part!r}")


def _canonical_directory(raw: str, workspace_root: Path) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path must be a non-empty absolute directory path")
    if "\x00" in raw:
        raise WorkspaceSecurityError("NUL bytes are not allowed in paths")
    windows = PureWindowsPath(raw.replace("/", "\\"))
    if not windows.is_absolute() or not windows.drive:
        raise WorkspaceSecurityError("External grant path must be absolute")
    # Device namespaces are deliberately excluded. UNC paths can be granted
    # explicitly, but device paths cannot be safely canonicalized here.
    normalized = raw.replace("/", "\\")
    if normalized.startswith("\\\\?\\") or normalized.startswith("\\\\.\\"):
        raise WorkspaceSecurityError("Windows device paths are not allowed")
    requested = Path(raw)
    if not requested.exists() or not requested.is_dir():
        raise FileNotFoundError(f"External grant directory does not exist: {raw}")
    if _is_reparse(requested):
        raise WorkspaceSecurityError("External grant root cannot be a symlink or reparse point")
    canonical = requested.resolve(strict=True)
    try:
        canonical.relative_to(workspace_root)
    except ValueError:
        pass
    else:
        raise WorkspaceSecurityError("Use the normal workspace tools for paths inside the workspace")
    _reject_reparse_components(canonical, canonical)
    return canonical


def _totp(secret: str, timestep: int) -> str:
    try:
        key = base64.b32decode(secret.strip().upper().replace(" ", ""), casefold=True)
    except Exception as exc:
        raise ValueError("TOTP secret is not valid base32") from exc
    digest = hmac.new(key, struct.pack(">Q", timestep), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


@dataclass(frozen=True)
class PendingGrant:
    request_id: str
    challenge: str
    root: Path
    mode: str
    ttl_seconds: int
    expires_at: float
    reason: str


@dataclass(frozen=True)
class ExternalGrant:
    grant_id: str
    request_id: str
    root: Path
    mode: str
    expires_at: float
    issued_at: float


class ExternalGrantManager:
    """Thread-safe in-memory pending requests and grants."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        enabled: bool = False,
        totp_secret: str | None = None,
        access_policy: AccessPolicy | None = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve(strict=True)
        self.enabled = enabled
        self.access_policy = access_policy
        self._secret = totp_secret or os.environ.get("TIANCHENG_TOTP_SECRET")
        self._pending: dict[str, PendingGrant] = {}
        self._active: dict[str, ExternalGrant] = {}
        self._attempts: dict[str, int] = {}
        self._expired_ids: set[str] = set()
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return bool(self._secret)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise PermissionError("External grants are disabled; restart with --allow-external-grants")

    def request(self, path: str, mode: str = "read", ttl_seconds: int = 600, reason: str = "") -> dict[str, object]:
        self._require_enabled()
        if mode not in _ALLOWED_MODES:
            raise ValueError(f"mode must be one of: {', '.join(sorted(_ALLOWED_MODES))}")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= MAX_GRANT_TTL:
            raise ValueError(f"ttl_seconds must be between 1 and {MAX_GRANT_TTL}")
        root = _canonical_directory(path, self.workspace_root)
        now = time.time()
        policy_operation = {"read": "read", "write": "write", "delete": "delete", "exec": "exec"}[mode]
        if self.access_policy is not None:
            try:
                policy_decision = self.access_policy.explain(root, policy_operation)
            except (AccessPolicyError, WorkspaceSecurityError) as exc:
                # A malformed or unsafe policy must never silently grant access.
                raise PermissionError("Static access policy could not safely evaluate this path") from exc
            if (
                policy_decision.rule_path is not None
                and policy_decision.mode == "deny"
            ):
                raise PermissionError("Static access policy denies this path")
            if policy_decision.allowed and not policy_decision.requires_approval:
                with self._lock:
                    self._purge_expired_locked(now)
                    if len(self._active) >= MAX_ACTIVE_GRANTS:
                        raise RuntimeError(f"At most {MAX_ACTIVE_GRANTS} active external grants are allowed")
                    grant_id = "policy_" + uuid.uuid4().hex
                    grant = ExternalGrant(grant_id, "policy", root, mode, now + ttl_seconds, now)
                    self._active[grant_id] = grant
                payload = self._grant_payload(grant)
                return {
                    **payload,
                    "requested_ttl_seconds": ttl_seconds,
                    "status": "approved",
                    "approval_required": False,
                    "policy_rule": str(policy_decision.rule_path),
                    "reason": reason[:500],
                }
        with self._lock:
            self._purge_expired_locked(now)
            if len(self._active) >= MAX_ACTIVE_GRANTS:
                raise RuntimeError(f"At most {MAX_ACTIVE_GRANTS} active external grants are allowed")
            request_id = uuid.uuid4().hex
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            challenge = "".join(secrets.choice(alphabet) for _ in range(4)) + "-" + "".join(secrets.choice(alphabet) for _ in range(4))
            pending = PendingGrant(request_id, challenge, root, mode, ttl_seconds, now + min(ttl_seconds, MAX_PENDING_TTL), reason[:500])
            self._pending[request_id] = pending
            self._attempts[request_id] = 0
        return {
            "request_id": request_id,
            "challenge": challenge,
            "path": str(root),
            "mode": mode,
            "requested_ttl_seconds": ttl_seconds,
            "approval_expires_at": pending.expires_at,
            "reason": pending.reason,
            "status": "pending",
            "instructions": "Ask the user to explicitly confirm this request, then submit request_id, challenge, and confirmation='批准'.",
        }

    def approve(self, request_id: str, challenge: str, confirmation: str = "") -> dict[str, object]:
        self._require_enabled()
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id is required")
        if not isinstance(challenge, str) or not challenge:
            raise PermissionError("challenge is required")
        if confirmation != "批准":
            raise PermissionError("Explicit user confirmation is required: confirmation must be '批准'")
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            pending = self._pending.get(request_id)
            if pending is None:
                raise PermissionError("Unknown or expired external access request")
            attempts = self._attempts.get(request_id, 0) + 1
            self._attempts[request_id] = attempts
            if attempts > 5:
                self._pending.pop(request_id, None)
                self._attempts.pop(request_id, None)
                raise PermissionError("Too many TOTP attempts; request cancelled")
            if not hmac.compare_digest(challenge, pending.challenge):
                raise PermissionError("Invalid or mismatched approval challenge")
            grant_id = secrets.token_urlsafe(18)
            grant = ExternalGrant(grant_id, request_id, pending.root, pending.mode, now + pending.ttl_seconds, now)
            self._active[grant_id] = grant
            self._pending.pop(request_id, None)
            self._attempts.pop(request_id, None)
        return self._grant_payload(grant)

    def revoke(self, grant_id: str) -> dict[str, object]:
        with self._lock:
            grant = self._active.pop(grant_id, None)
        if grant is None:
            raise FileNotFoundError("Unknown or expired external grant")
        return {"grant_id": grant_id, "revoked": True, "path": str(grant.root)}

    def cancel_request(self, request_id: str) -> dict[str, object]:
        with self._lock:
            pending = self._pending.pop(request_id, None)
            self._attempts.pop(request_id, None)
        if pending is None:
            raise FileNotFoundError("Unknown or expired external access request")
        return {"request_id": request_id, "cancelled": True, "path": str(pending.root)}

    def status(self) -> dict[str, object]:
        with self._lock:
            self._purge_expired_locked(time.time())
            return {
                "enabled": self.enabled,
                "totp_configured": self.configured,
                "pending": [self._pending_payload(item) for item in self._pending.values()],
                "active": [self._grant_payload(item) for item in self._active.values()],
            }

    def expire(self) -> list[str]:
        """Remove expired capabilities and return their ids exactly once."""

        with self._lock:
            self._purge_expired_locked(time.time())
            expired = list(self._expired_ids)
            self._expired_ids.clear()
            return expired

    def resolve(self, grant_id: str, relative_path: str = ".", *, required_mode: str = "read", must_exist: bool = True, expect: str | None = None, allow_root: bool = True) -> tuple[Path, ExternalGrant]:
        with self._lock:
            self._purge_expired_locked(time.time())
            grant = self._active.get(grant_id)
        if grant is None:
            raise PermissionError("Unknown or expired external grant")
        if required_mode == "read" and grant.mode not in {"read", "write", "delete", "exec"}:
            raise PermissionError("Grant does not allow reading")
        if required_mode == "write" and grant.mode not in {"write", "delete", "exec"}:
            raise PermissionError("Grant does not allow writing")
        if required_mode == "delete" and grant.mode not in {"delete", "exec"}:
            raise PermissionError("Grant does not allow deletion")
        if required_mode == "exec" and grant.mode != "exec":
            raise PermissionError("Grant does not allow command execution")
        raw = "." if relative_path in (None, "") else str(relative_path)
        windows = PureWindowsPath(raw.replace("/", "\\"))
        if windows.drive or windows.root or windows.is_absolute() or any(part == ".." for part in windows.parts):
            raise WorkspaceSecurityError("External grant paths must be relative and cannot contain '..'")
        for part in windows.parts:
            _validate_component(part)
        candidate = grant.root.joinpath(*[part for part in windows.parts if part not in {"", "."}])
        if candidate == grant.root and not allow_root:
            raise WorkspaceSecurityError("The external grant root is not valid for this operation")
        existing = candidate
        while not os.path.lexists(existing):
            if existing == grant.root:
                break
            existing = existing.parent
        _reject_reparse_components(grant.root, existing)
        resolved_existing = existing.resolve(strict=True)
        try:
            resolved_existing.relative_to(grant.root)
        except ValueError as exc:
            raise WorkspaceSecurityError("Resolved path escapes the grant root") from exc
        resolved = resolved_existing.joinpath(*candidate.relative_to(existing).parts)
        if os.path.lexists(candidate):
            _reject_reparse_components(grant.root, candidate)
            resolved = candidate.resolve(strict=True)
        if must_exist and not os.path.lexists(candidate):
            raise FileNotFoundError(f"External path does not exist: {raw}")
        if expect == "file" and os.path.lexists(candidate) and not resolved.is_file():
            raise IsADirectoryError(f"Expected a file: {raw}")
        if expect == "directory" and os.path.lexists(candidate) and not resolved.is_dir():
            raise NotADirectoryError(f"Expected a directory: {raw}")
        return resolved, grant

    def _purge_expired_locked(self, now: float) -> None:
        self._pending = {key: value for key, value in self._pending.items() if value.expires_at > now}
        self._attempts = {key: value for key, value in self._attempts.items() if key in self._pending}
        self._expired_ids.update(
            key for key, value in self._active.items() if value.expires_at <= now
        )
        self._active = {key: value for key, value in self._active.items() if value.expires_at > now}

    @staticmethod
    def _pending_payload(item: PendingGrant) -> dict[str, object]:
        return {"request_id": item.request_id, "challenge": item.challenge, "path": str(item.root), "mode": item.mode, "requested_ttl_seconds": item.ttl_seconds, "approval_expires_at": item.expires_at, "reason": item.reason}

    @staticmethod
    def _grant_payload(item: ExternalGrant) -> dict[str, object]:
        return {"grant_id": item.grant_id, "request_id": item.request_id, "path": str(item.root), "mode": item.mode, "issued_at": item.issued_at, "expires_at": item.expires_at}
