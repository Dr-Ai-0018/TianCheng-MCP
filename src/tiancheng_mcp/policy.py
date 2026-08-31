"""Static, fail-closed path capability policy.

The policy is deliberately separate from :mod:`security`: ``WorkspaceJail``
protects one fixed root, while this module describes additional explicitly
trusted roots.  It never replaces canonicalization or reparse-point checks.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from .security import FILE_ATTRIBUTE_REPARSE_POINT, WorkspaceSecurityError


_MODES = frozenset({"browse", "read", "write", "full", "deny"})
_OPERATIONS = frozenset(
    {"list", "read", "write", "delete", "purge", "git_read", "git_write", "exec"}
)
# "browse" exists so a caller can discover what lives under a directory before
# asking for real access to it.  It grants a single directory listing and
# nothing else: no file content, no writes, no exec.
_LISTING_MODES = frozenset({"browse", "read", "write", "full"})
MAX_ACCESS_POLICY_BYTES = 1024 * 1024


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace a file atomically using a same-directory temporary file."""

    handle, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class AccessPolicyError(ValueError):
    """Raised when a policy is malformed or cannot be safely evaluated."""


def _is_reparse(path: Path) -> bool:
    try:
        stat_result = path.lstat()
    except OSError as exc:
        raise AccessPolicyError(f"Cannot inspect policy path safely: {path}") from exc
    return path.is_symlink() or bool(
        getattr(stat_result, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _reject_reparse_components(root: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise AccessPolicyError("Policy path escaped its canonical root") from exc
    current = root
    if _is_reparse(current):
        raise AccessPolicyError("Policy root cannot be a symlink or reparse point")
    for part in relative.parts:
        current = current / part
        if os.path.lexists(current) and _is_reparse(current):
            raise AccessPolicyError(
                f"Policy path contains a symlink, junction, or reparse point: {part!r}"
            )


def _reject_reparse_ancestors(path: Path) -> None:
    """Reject a policy file stored below a link/reparse-point parent."""

    current = path
    while True:
        if os.path.lexists(current) and _is_reparse(current):
            raise AccessPolicyError("Access policy cannot be stored below a reparse point")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _canonical_path(raw: str, *, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise AccessPolicyError(f"{label} must be a non-empty absolute path")
    if "\x00" in raw:
        raise AccessPolicyError(f"{label} cannot contain NUL bytes")
    windows = PureWindowsPath(raw.replace("/", "\\"))
    if not windows.is_absolute() or not windows.drive:
        raise AccessPolicyError(f"{label} must be an absolute Windows path")
    normalized = raw.replace("/", "\\")
    if normalized.startswith(("\\\\?\\", "\\\\.\\")):
        raise AccessPolicyError("Windows device paths are not allowed in access policy")
    requested = Path(raw)
    existing = requested
    missing: list[str] = []
    while not os.path.lexists(existing):
        parent = existing.parent
        if parent == existing:
            raise AccessPolicyError(f"{label} has no safe existing parent")
        missing.append(existing.name)
        existing = parent
    if not existing.is_dir():
        raise AccessPolicyError(f"{label} must identify a directory")
    canonical_parent = existing.resolve(strict=True)
    _reject_reparse_components(canonical_parent, canonical_parent)
    candidate = canonical_parent.joinpath(*reversed(missing))
    if os.path.lexists(requested):
        if not requested.is_dir():
            raise AccessPolicyError(f"{label} must identify a directory")
        candidate = requested.resolve(strict=True)
        _reject_reparse_components(canonical_parent, candidate)
    return candidate


@dataclass(frozen=True)
class AccessRule:
    path: Path
    mode: str
    require_approval: bool = False
    enabled: bool = True
    allow_exec: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.mode not in _MODES:
            raise AccessPolicyError(f"mode must be one of: {', '.join(sorted(_MODES))}")
        if not isinstance(self.require_approval, bool):
            raise AccessPolicyError("require_approval must be boolean")
        if not isinstance(self.enabled, bool) or not isinstance(self.allow_exec, bool):
            raise AccessPolicyError("enabled and allow_exec must be boolean")
        if self.mode == "browse" and self.allow_exec:
            raise AccessPolicyError("browse rules cannot enable exec")
        if len(self.note) > 500:
            raise AccessPolicyError("note is limited to 500 characters")

    @property
    def specificity(self) -> int:
        return len(self.path.parts)

    def allows(self, operation: str) -> bool:
        if operation not in _OPERATIONS:
            raise AccessPolicyError(f"Unknown policy operation: {operation}")
        if self.mode == "deny":
            return False
        if operation == "list":
            return self.mode in _LISTING_MODES
        # Every operation below reads or changes file content, so a browse
        # rule refuses all of them.
        if operation == "exec":
            return self.allow_exec
        if operation == "purge":
            return self.mode == "full"
        if operation == "git_write":
            return self.mode == "full"
        if operation == "git_read":
            return self.mode in {"read", "write", "full"}
        if operation == "delete":
            return self.mode in {"write", "full"}
        if operation == "write":
            return self.mode in {"write", "full"}
        return self.mode in {"read", "write", "full"}


@dataclass(frozen=True)
class AccessDecision:
    path: Path
    operation: str
    allowed: bool
    requires_approval: bool
    rule_path: Path | None
    mode: str
    allow_exec: bool
    reason: str

    def as_dict(self, *, redact_paths: bool = False) -> dict[str, Any]:
        path = "<path>" if redact_paths else str(self.path)
        rule_path = None if self.rule_path is None else ("<rule>" if redact_paths else str(self.rule_path))
        return {
            "path": path,
            "operation": self.operation,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "rule_path": rule_path,
            "mode": self.mode,
            "allow_exec": self.allow_exec,
            "reason": self.reason,
        }


class AccessPolicy:
    """Immutable-in-use static policy with deterministic longest-path matching."""

    def __init__(self, workspace_root: str | Path, rules: list[AccessRule]) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir() or _is_reparse(self.workspace_root):
            raise AccessPolicyError("Workspace root must be a real directory")
        normalized: list[AccessRule] = []
        for rule in rules:
            if rule.path == self.workspace_root:
                canonical = self.workspace_root
            else:
                canonical = _canonical_path(str(rule.path), label="rule path")
            normalized.append(
                AccessRule(
                    canonical,
                    rule.mode,
                    rule.require_approval,
                    rule.enabled,
                    rule.allow_exec,
                    rule.note,
                )
            )
        by_path: dict[Path, tuple[str, bool, bool, bool]] = {}
        for rule in normalized:
            semantics = (rule.mode, rule.require_approval, rule.enabled, rule.allow_exec)
            previous = by_path.get(rule.path)
            if previous is not None and previous != semantics:
                raise AccessPolicyError("Conflicting rules at the same path")
            by_path[rule.path] = semantics
        if not any(rule.enabled and rule.path == self.workspace_root for rule in normalized):
            raise AccessPolicyError("Policy must contain an enabled workspace root rule")
        self.rules = tuple(normalized)

    @classmethod
    def default(cls, workspace_root: str | Path) -> "AccessPolicy":
        root = Path(workspace_root).resolve(strict=True)
        return cls(root, [AccessRule(root, "full")])

    def to_payload(self) -> dict[str, Any]:
        return {
            "rules": [
                {
                    "path": str(rule.path),
                    "mode": rule.mode,
                    "require_approval": rule.require_approval,
                    "enabled": rule.enabled,
                    "allow_exec": rule.allow_exec,
                    "note": rule.note,
                }
                for rule in self.rules
            ]
        }

    def with_rules(self, added: list[AccessRule]) -> "AccessPolicy":
        """Return a new validated policy with these rules added or replaced.

        Replacing by canonical path keeps the file free of duplicate entries
        when the same directory is granted twice with different capabilities.
        """

        canonical = {
            _canonical_path(str(rule.path), label="rule path"): rule for rule in added
        }
        kept = [rule for rule in self.rules if rule.path not in canonical]
        merged = kept + [
            AccessRule(
                path,
                rule.mode,
                rule.require_approval,
                rule.enabled,
                rule.allow_exec,
                rule.note,
            )
            for path, rule in canonical.items()
        ]
        return AccessPolicy(self.workspace_root, merged)

    def save_atomic(
        self, config_path: str | Path, *, acl_hardener: Any | None = None
    ) -> dict[str, Any]:
        """Write this policy with one recoverable backup and an atomic replace.

        The policy is already validated by ``__init__``, so a file written here
        always reloads.  The previous file is kept as ``.bak`` and restored if
        the ACL hardening step fails, so a failed save never leaves a wider
        policy in force than the caller approved.
        """

        path = Path(config_path)
        _reject_reparse_ancestors(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(path) and (not path.is_file() or _is_reparse(path)):
            raise AccessPolicyError("Access policy must be a regular non-reparse file")
        encoded = (
            json.dumps(self.to_payload(), ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_ACCESS_POLICY_BYTES:
            raise AccessPolicyError("Access policy file is too large")
        backup = path.with_name(path.name + ".bak")
        previous: bytes | None = None
        if path.exists():
            previous = path.read_bytes()
            _atomic_write_bytes(backup, previous)
        _atomic_write_bytes(path, encoded)
        if acl_hardener is not None:
            try:
                acl_hardener(path)
            except Exception:
                if previous is not None:
                    _atomic_write_bytes(path, previous)
                else:
                    path.unlink(missing_ok=True)
                raise
        return {"path": str(path), "backup": str(backup) if previous else None}

    @classmethod
    def load(cls, config_path: str | Path, workspace_root: str | Path) -> "AccessPolicy":
        path = Path(config_path)
        _reject_reparse_ancestors(path)
        if not path.exists():
            return cls.default(workspace_root)
        if not path.is_file() or _is_reparse(path):
            raise AccessPolicyError("Access policy file must be a regular non-reparse file")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AccessPolicyError("Access policy file is unreadable or invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
            raise AccessPolicyError("Access policy must be an object containing a rules array")
        rules: list[AccessRule] = []
        for index, raw in enumerate(payload["rules"]):
            if not isinstance(raw, dict):
                raise AccessPolicyError(f"Rule {index} must be an object")
            unknown = set(raw) - {"path", "mode", "require_approval", "enabled", "allow_exec", "note"}
            if unknown:
                raise AccessPolicyError(f"Rule {index} has unknown fields: {', '.join(sorted(unknown))}")
            path_value = raw.get("path")
            if not isinstance(path_value, str):
                raise AccessPolicyError(f"Rule {index} path must be text")
            canonical = _canonical_path(path_value, label=f"rule {index} path")
            rules.append(
                AccessRule(
                    canonical,
                    raw.get("mode", "deny"),
                    raw.get("require_approval", False),
                    raw.get("enabled", True),
                    raw.get("allow_exec", False),
                    raw.get("note", ""),
                )
            )
        return cls(workspace_root, rules)

    def explain(self, path: str | Path, operation: str = "read") -> AccessDecision:
        if operation not in _OPERATIONS:
            raise AccessPolicyError(f"Unknown policy operation: {operation}")
        candidate = _canonical_target(path)
        matches = [
            rule
            for rule in self.rules
            if rule.enabled and _is_within(rule.path, candidate)
        ]
        if not matches:
            return AccessDecision(candidate, operation, False, False, None, "deny", False, "No enabled rule matches this path")
        maximum = max(rule.specificity for rule in matches)
        specific = [rule for rule in matches if rule.specificity == maximum]
        modes = {(rule.mode, rule.require_approval, rule.allow_exec) for rule in specific}
        if len(modes) != 1:
            raise AccessPolicyError("Conflicting rules at the same path specificity")
        rule = specific[0]
        allowed = rule.allows(operation)
        return AccessDecision(
            candidate,
            operation,
            allowed,
            allowed and rule.require_approval,
            rule.path,
            rule.mode,
            rule.allow_exec,
            "Matched most-specific enabled rule" if allowed else "Matched rule does not allow this operation",
        )

    def authorize(self, path: str | Path, operation: str = "read") -> AccessDecision:
        decision = self.explain(path, operation)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        return decision

    def summary(self) -> dict[str, Any]:
        """Describe the policy, including every rule.

        Callers need to know which directories they may use before they touch
        one.  Listing the rules is not a disclosure: ``access_policy_explain``
        already answers the same question one path at a time, so withholding
        the list only forces a caller to guess paths.
        """

        return {
            "rule_count": len(self.rules),
            "enabled_rule_count": sum(rule.enabled for rule in self.rules),
            "workspace_rule": True,
            "rules": [
                {
                    "path": str(rule.path),
                    "mode": rule.mode,
                    "allow_exec": rule.allow_exec,
                    "require_approval": rule.require_approval,
                    "enabled": rule.enabled,
                    "note": rule.note,
                }
                for rule in sorted(self.rules, key=lambda rule: str(rule.path))
            ],
        }


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _canonical_target(value: str | Path) -> Path:
    raw = str(value)
    if "\x00" in raw:
        raise WorkspaceSecurityError("NUL bytes are not allowed in paths")
    requested = Path(raw)
    if not requested.is_absolute():
        raise WorkspaceSecurityError("Access policy paths must be absolute")
    existing = requested
    missing: list[str] = []
    while not os.path.lexists(existing):
        parent = existing.parent
        if parent == existing:
            raise WorkspaceSecurityError("Could not find a safe existing parent")
        missing.append(existing.name)
        existing = parent
    resolved = existing.resolve(strict=True)
    _reject_reparse_components(resolved, resolved)
    candidate = resolved.joinpath(*reversed(missing))
    if os.path.lexists(requested):
        _reject_reparse_components(resolved, requested)
        candidate = requested.resolve(strict=True)
    return candidate
