"""Strict, local-only policy for read-only agent conversation sources.

The MCP runtime may consume this policy but cannot create or widen it.  Source
roots are provider-specific metadata stores, not general file grants: callers
never receive arbitrary file reads through this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile
from typing import Any, Callable

from .security import FILE_ATTRIBUTE_REPARSE_POINT, WorkspaceJail


AGENT_SOURCE_SCHEMA_VERSION = 1
MAX_AGENT_SOURCE_CONFIG_BYTES = 1024 * 1024
_SOURCE_ID = re.compile(r"src_[a-z0-9][a-z0-9_-]{2,63}")
_PROVIDERS = frozenset({"codex", "claude-code"})
_MODE = "catalog-read"
_SENSITIVE_COMPONENTS = frozenset(
    {
        "auth",
        "auth.json",
        "credential",
        "credentials",
        "key",
        "keys",
        "secret",
        "secrets",
        "settings",
        "config",
        "session-env",
        "shell-snapshots",
        "backup",
        "backups",
        "cache",
        "plugins",
        "hooks",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "source_id",
        "provider",
        "root",
        "enabled",
        "mode",
        "max_files",
        "max_file_bytes",
        "max_scan_bytes",
        "max_refresh_seconds",
    }
)


class AgentSourcePolicyError(ValueError):
    """Raised when an agent source policy is unsafe or malformed."""


def _bounded_int(
    value: object, *, default: int, minimum: int, maximum: int, label: str
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentSourcePolicyError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise AgentSourcePolicyError(f"{label} must be between {minimum} and {maximum}")
    return value


def _is_reparse(path: Path) -> bool:
    try:
        status = path.lstat()
    except OSError as exc:
        raise AgentSourcePolicyError("Cannot inspect agent source path safely") from exc
    return path.is_symlink() or bool(
        getattr(status, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _reject_reparse_ancestors(path: Path, *, include_self: bool = True) -> None:
    current = path if include_self else path.parent
    while True:
        if os.path.lexists(current) and _is_reparse(current):
            raise AgentSourcePolicyError(
                "Agent source paths cannot contain symlinks, junctions, or reparse points"
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _canonical_source_root(raw: object, provider: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise AgentSourcePolicyError("source root must be a non-empty absolute path")
    if "\x00" in raw:
        raise AgentSourcePolicyError("source root cannot contain NUL bytes")
    normalized = raw.replace("/", "\\")
    if normalized.startswith(("\\\\", "\\\\?\\", "\\\\.\\")):
        raise AgentSourcePolicyError("UNC and Windows device paths are not allowed")
    windows = PureWindowsPath(normalized)
    if not windows.is_absolute() or not windows.drive:
        raise AgentSourcePolicyError("source root must be an absolute Windows path")
    requested = Path(raw)
    if not requested.exists() or not requested.is_dir():
        raise AgentSourcePolicyError("source root must be an existing directory")
    _reject_reparse_ancestors(requested)
    canonical = requested.resolve(strict=True)
    components = {part.casefold() for part in canonical.parts}
    forbidden = components & _SENSITIVE_COMPONENTS
    if forbidden:
        raise AgentSourcePolicyError(
            f"source root contains a sensitive component: {sorted(forbidden)[0]}"
        )
    leaf = canonical.name.casefold()
    if provider == "codex" and leaf != "sessions":
        raise AgentSourcePolicyError("Codex source root must be its sessions directory")
    if provider == "claude-code" and leaf != "projects":
        raise AgentSourcePolicyError("Claude Code source root must be its projects directory")
    # Constructing a jail performs a second root/reparse validation and gives
    # catalog scanning the same relative-path boundary used by workspace tools.
    WorkspaceJail(canonical, create=False)
    return canonical


@dataclass(frozen=True)
class AgentSource:
    source_id: str
    provider: str
    root: Path
    root_device: int
    root_inode: int
    enabled: bool = True
    mode: str = _MODE
    max_files: int = 10_000
    max_file_bytes: int = 2 * 1024 * 1024
    max_scan_bytes: int = 64 * 1024 * 1024
    max_refresh_seconds: int = 60

    def as_dict(self, *, include_root: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_id": self.source_id,
            "provider": self.provider,
            "enabled": self.enabled,
            "mode": self.mode,
            "display_name": f"{self.provider} {self.root.name}",
            "limits": {
                "max_files": self.max_files,
                "max_file_bytes": self.max_file_bytes,
                "max_scan_bytes": self.max_scan_bytes,
                "max_refresh_seconds": self.max_refresh_seconds,
            },
        }
        if include_root:
            payload["root"] = str(self.root)
        return payload

    def jail(self) -> WorkspaceJail:
        jail = WorkspaceJail(self.root, create=False)
        status = jail.root.stat()
        if (status.st_dev, status.st_ino) != (self.root_device, self.root_inode):
            raise PermissionError("Agent source root identity changed")
        return jail

    @property
    def binding_fingerprint(self) -> str:
        payload = "\x00".join(
            (
                self.provider,
                str(self.root).casefold(),
                str(self.root_device),
                str(self.root_inode),
            )
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class AgentSourcePolicy:
    """Immutable set of validated, non-overlapping catalog-read sources."""

    def __init__(self, sources: list[AgentSource], *, config_path: Path | None = None) -> None:
        by_id: dict[str, AgentSource] = {}
        canonical_roots: list[tuple[Path, str]] = []
        for source in sources:
            if source.source_id in by_id:
                raise AgentSourcePolicyError("Duplicate agent source_id")
            for root, source_id in canonical_roots:
                if source.root == root or source.root in root.parents or root in source.root.parents:
                    raise AgentSourcePolicyError(
                        f"Agent source roots overlap: {source_id} and {source.source_id}"
                    )
            by_id[source.source_id] = source
            canonical_roots.append((source.root, source.source_id))
        self.sources = tuple(sources)
        self._by_id = by_id
        self.config_path = config_path

    @classmethod
    def empty(cls, config_path: str | Path | None = None) -> "AgentSourcePolicy":
        return cls([], config_path=Path(config_path) if config_path else None)

    @classmethod
    def load(cls, config_path: str | Path) -> "AgentSourcePolicy":
        path = Path(config_path)
        _reject_reparse_ancestors(path, include_self=False)
        if not path.exists():
            return cls.empty(path)
        if not path.is_file() or _is_reparse(path):
            raise AgentSourcePolicyError(
                "Agent source policy must be a regular non-reparse file"
            )
        try:
            size = path.stat().st_size
            if size > MAX_AGENT_SOURCE_CONFIG_BYTES:
                raise AgentSourcePolicyError("Agent source policy file is too large")
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except AgentSourcePolicyError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AgentSourcePolicyError(
                "Agent source policy is unreadable or invalid JSON"
            ) from exc
        return cls.from_payload(payload, config_path=path)

    @classmethod
    def from_payload(
        cls, payload: object, *, config_path: Path | None = None
    ) -> "AgentSourcePolicy":
        if not isinstance(payload, dict):
            raise AgentSourcePolicyError("Agent source policy must be an object")
        unknown = set(payload) - {"schema_version", "sources"}
        if unknown:
            raise AgentSourcePolicyError(
                f"Agent source policy has unknown fields: {', '.join(sorted(unknown))}"
            )
        if payload.get("schema_version") != AGENT_SOURCE_SCHEMA_VERSION:
            raise AgentSourcePolicyError(
                f"schema_version must be {AGENT_SOURCE_SCHEMA_VERSION}"
            )
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list):
            raise AgentSourcePolicyError("sources must be an array")
        if len(raw_sources) > 32:
            raise AgentSourcePolicyError("At most 32 agent sources may be configured")
        sources: list[AgentSource] = []
        for index, raw_source in enumerate(raw_sources):
            if not isinstance(raw_source, dict):
                raise AgentSourcePolicyError(f"source {index} must be an object")
            unknown_source = set(raw_source) - _SOURCE_FIELDS
            if unknown_source:
                raise AgentSourcePolicyError(
                    f"source {index} has unknown fields: "
                    f"{', '.join(sorted(unknown_source))}"
                )
            source_id = raw_source.get("source_id")
            if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
                raise AgentSourcePolicyError(f"source {index} source_id is invalid")
            provider = raw_source.get("provider")
            if provider not in _PROVIDERS:
                raise AgentSourcePolicyError(
                    f"source {index} provider must be codex or claude-code"
                )
            enabled = raw_source.get("enabled", True)
            if not isinstance(enabled, bool):
                raise AgentSourcePolicyError(f"source {index} enabled must be boolean")
            mode = raw_source.get("mode", _MODE)
            if mode != _MODE:
                raise AgentSourcePolicyError(
                    f"source {index} mode must be {_MODE}; file access is not implied"
                )
            root = _canonical_source_root(raw_source.get("root"), provider)
            root_status = root.stat()
            sources.append(
                AgentSource(
                    source_id=source_id,
                    provider=provider,
                    root=root,
                    root_device=root_status.st_dev,
                    root_inode=root_status.st_ino,
                    enabled=enabled,
                    mode=mode,
                    max_files=_bounded_int(
                        raw_source.get("max_files"),
                        default=10_000,
                        minimum=1,
                        maximum=100_000,
                        label=f"source {index} max_files",
                    ),
                    max_file_bytes=_bounded_int(
                        raw_source.get("max_file_bytes"),
                        default=2 * 1024 * 1024,
                        minimum=1024,
                        maximum=16 * 1024 * 1024,
                        label=f"source {index} max_file_bytes",
                    ),
                    max_scan_bytes=_bounded_int(
                        raw_source.get("max_scan_bytes"),
                        default=64 * 1024 * 1024,
                        minimum=1024,
                        maximum=512 * 1024 * 1024,
                        label=f"source {index} max_scan_bytes",
                    ),
                    max_refresh_seconds=_bounded_int(
                        raw_source.get("max_refresh_seconds"),
                        default=60,
                        minimum=1,
                        maximum=120,
                        label=f"source {index} max_refresh_seconds",
                    ),
                )
            )
        return cls(sources, config_path=config_path)

    def get(self, source_id: str, *, require_enabled: bool = True) -> AgentSource:
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise AgentSourcePolicyError("source_id is invalid")
        source = self._by_id.get(source_id)
        if source is None:
            raise FileNotFoundError("Agent source was not found")
        if require_enabled and not source.enabled:
            raise PermissionError("Agent source is disabled")
        return source

    def summaries(self) -> list[dict[str, Any]]:
        return [source.as_dict() for source in self.sources]

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": AGENT_SOURCE_SCHEMA_VERSION,
            "source_count": len(self.sources),
            "enabled_source_count": sum(source.enabled for source in self.sources),
            "providers": sorted({source.provider for source in self.sources}),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": AGENT_SOURCE_SCHEMA_VERSION,
            "sources": [
                {
                    "source_id": source.source_id,
                    "provider": source.provider,
                    "root": str(source.root),
                    "enabled": source.enabled,
                    "mode": source.mode,
                    "max_files": source.max_files,
                    "max_file_bytes": source.max_file_bytes,
                    "max_scan_bytes": source.max_scan_bytes,
                    "max_refresh_seconds": source.max_refresh_seconds,
                }
                for source in self.sources
            ],
        }

    def save_atomic(
        self,
        config_path: str | Path | None = None,
        *,
        acl_hardener: Callable[[Path], None] | None = None,
    ) -> dict[str, Any]:
        """Save a validated local policy with one recoverable backup.

        This method is intended for the local TUI, never an MCP tool.  Windows
        ACL ownership is environment-specific, so the TUI supplies its tested
        hardener callback after each atomic replacement.
        """

        path = Path(config_path) if config_path is not None else self.config_path
        if path is None:
            raise AgentSourcePolicyError("Agent source policy path is not configured")
        path.parent.mkdir(parents=True, exist_ok=True)
        _reject_reparse_ancestors(path, include_self=False)
        if os.path.lexists(path) and (not path.is_file() or _is_reparse(path)):
            raise AgentSourcePolicyError(
                "Agent source policy must be a regular non-reparse file"
            )
        encoded = (
            json.dumps(self.to_payload(), ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_AGENT_SOURCE_CONFIG_BYTES:
            raise AgentSourcePolicyError("Agent source policy file is too large")
        backup = path.with_name(path.name + ".bak")
        if os.path.lexists(backup) and (
            not backup.is_file() or _is_reparse(backup)
        ):
            raise AgentSourcePolicyError(
                "Agent source policy backup must be a regular non-reparse file"
            )
        previous: bytes | None = None
        if path.exists():
            previous = path.read_bytes()
            if len(previous) > MAX_AGENT_SOURCE_CONFIG_BYTES:
                raise AgentSourcePolicyError(
                    "Existing agent source policy is too large to back up safely"
                )
            self._atomic_write(backup, previous)
        self._atomic_write(path, encoded)
        if acl_hardener is not None:
            try:
                acl_hardener(path)
                if backup.exists():
                    acl_hardener(backup)
            except Exception as exc:
                try:
                    if previous is None:
                        if path.exists():
                            path.unlink()
                    else:
                        self._atomic_write(path, previous)
                except OSError as rollback_error:
                    raise AgentSourcePolicyError(
                        "Agent source policy ACL hardening and rollback both failed"
                    ) from rollback_error
                raise AgentSourcePolicyError(
                    "Agent source policy ACL hardening failed; previous policy restored"
                ) from exc
        return {
            "saved": True,
            "schema_version": AGENT_SOURCE_SCHEMA_VERSION,
            "source_count": len(self.sources),
            "backup_created": backup.exists(),
        }

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
