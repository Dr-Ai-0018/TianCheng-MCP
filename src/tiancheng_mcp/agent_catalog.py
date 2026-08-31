"""Bounded, incremental metadata index for approved local agent sources."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import threading
import time
from typing import Any

from .agent_sources import AgentSource, AgentSourcePolicy
from .jobs import JobCancelled
from .security import FILE_ATTRIBUTE_REPARSE_POINT, WorkspaceSecurityError


AGENT_CATALOG_SCHEMA_VERSION = 2
AGENT_CATALOG_PARSER_VERSION = 1
MAX_METADATA_READ_BYTES = 256 * 1024
MAX_METADATA_LINES = 1_000
MAX_METADATA_LINE_BYTES = 64 * 1024
MAX_CATALOG_RESULTS = 200
DEFAULT_CATALOG_OUTPUT_BYTES = 128 * 1024
MAX_CATALOG_OUTPUT_BYTES = 512 * 1024
_NATIVE_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CONVERSATION_REF = re.compile(r"convref_[0-9a-f]{32}")
_CODEX_ROLLOUT = re.compile(r"rollout-.+\.jsonl", re.IGNORECASE)
_CLAUDE_SESSION = re.compile(r"[A-Za-z0-9_-]{1,128}\.jsonl", re.IGNORECASE)
_CLAUDE_EXCLUDED_PARTS = frozenset(
    {
        "subagents",
        "tool-results",
        "tool-cache",
        "plans",
        "attachments",
        "session-env",
        "shell-snapshots",
        "cache",
        "backup",
        "backups",
        "plugins",
        "hooks",
    }
)
_SIGNED_INT64_MAX = (1 << 63) - 1
_UNSIGNED_INT64_MAX = (1 << 64) - 1


class AgentCatalogError(ValueError):
    """Raised for bounded catalog configuration or query errors."""


def _sqlite_file_identity(value: int) -> int:
    """Encode an unsigned Windows file identity losslessly in SQLite int64."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AgentCatalogError("unsupported_file_identity")
    if value <= _SIGNED_INT64_MAX:
        return value
    if value <= _UNSIGNED_INT64_MAX:
        return value - (1 << 64)
    raise AgentCatalogError("unsupported_file_identity")


def _file_identity(status: os.stat_result) -> tuple[int, int]:
    return (
        _sqlite_file_identity(status.st_dev),
        _sqlite_file_identity(status.st_ino),
    )


@dataclass(frozen=True)
class ParsedConversation:
    native_session_id: str
    title: str
    cwd: str | None
    created_at: str | None
    updated_at: str
    metadata_truncated: bool


@dataclass(frozen=True)
class _IndexedFile:
    source_id: str
    provider: str
    source_fingerprint: str
    relative_path: str
    size: int
    mtime_ns: int
    file_device: int
    file_inode: int
    fingerprint: str | None
    status: str
    error_code: str | None
    conversation_ref: str | None = None
    native_session_id: str | None = None
    title: str | None = None
    cwd: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata_truncated: bool = False


def _iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _normalize_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        return None
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _bounded_metadata_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > maximum:
        text = encoded[:maximum].decode("utf-8", errors="ignore")
    return text


def _conversation_ref(
    source_id: str, provider: str, native_session_id: str, relative_path: str
) -> str:
    payload = "\x00".join(
        (source_id, provider, native_session_id, relative_path.casefold())
    ).encode("utf-8")
    return "convref_" + hashlib.sha256(payload).hexdigest()[:32]


def _is_candidate(source: AgentSource, relative_path: str) -> bool:
    path = PureWindowsPath(relative_path.replace("/", "\\"))
    lowered = [part.casefold() for part in path.parts]
    if any(part.startswith(".") and part not in {".", ".."} for part in lowered):
        return False
    if source.provider == "codex":
        if len(path.parts) < 4 or not _CODEX_ROLLOUT.fullmatch(path.name):
            return False
        year, month, day = path.parts[:3]
        return (
            len(year) == 4
            and len(month) == 2
            and len(day) == 2
            and year.isdigit()
            and month.isdigit()
            and day.isdigit()
        )
    if source.provider == "claude-code":
        if len(path.parts) < 2 or not _CLAUDE_SESSION.fullmatch(path.name):
            return False
        return not any(part in _CLAUDE_EXCLUDED_PARTS for part in lowered)
    return False


def _read_metadata_bytes(path: Path, size: int) -> tuple[bytes, bool]:
    maximum = min(size, MAX_METADATA_READ_BYTES)
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    truncated = size > MAX_METADATA_READ_BYTES or len(data) > maximum
    return data[:maximum], truncated


def _json_metadata_lines(data: bytes) -> tuple[list[dict[str, Any]], bool]:
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeError as exc:
        raise AgentCatalogError("invalid_utf8") from exc
    rows: list[dict[str, Any]] = []
    truncated = False
    for index, line in enumerate(text.splitlines()):
        if index >= MAX_METADATA_LINES:
            truncated = True
            break
        encoded = line.encode("utf-8")
        if len(encoded) > MAX_METADATA_LINE_BYTES:
            truncated = True
            continue
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    if not rows:
        raise AgentCatalogError("no_metadata_json")
    return rows, truncated


def _parse_codex(
    relative_path: str, data: bytes, *, updated_at: str, truncated: bool
) -> ParsedConversation:
    rows, rows_truncated = _json_metadata_lines(data)
    native_id: str | None = None
    cwd: str | None = None
    created_at: str | None = None
    for row in rows:
        timestamp = _normalize_timestamp(row.get("timestamp"))
        created_at = created_at or timestamp
        if row.get("type") != "session_meta":
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        candidate = payload.get("id")
        if isinstance(candidate, str) and _NATIVE_SESSION_ID.fullmatch(candidate):
            native_id = candidate
        cwd = _bounded_metadata_text(payload.get("cwd"), 2048)
        created_at = _normalize_timestamp(payload.get("timestamp")) or created_at
        break
    if native_id is None:
        raise AgentCatalogError("missing_session_id")
    return ParsedConversation(
        native_session_id=native_id,
        title=f"Codex session {native_id[:8]}",
        cwd=cwd,
        created_at=created_at,
        updated_at=updated_at,
        metadata_truncated=truncated or rows_truncated,
    )


def _parse_claude(
    relative_path: str, data: bytes, *, updated_at: str, truncated: bool
) -> ParsedConversation:
    rows, rows_truncated = _json_metadata_lines(data)
    filename_id = PureWindowsPath(relative_path.replace("/", "\\")).stem
    native_id = filename_id if _NATIVE_SESSION_ID.fullmatch(filename_id) else None
    cwd: str | None = None
    created_at: str | None = None
    for row in rows:
        candidate = row.get("sessionId")
        if isinstance(candidate, str) and _NATIVE_SESSION_ID.fullmatch(candidate):
            if native_id is not None and candidate != native_id:
                raise AgentCatalogError("session_id_mismatch")
            native_id = candidate
        cwd = cwd or _bounded_metadata_text(row.get("cwd"), 2048)
        created_at = created_at or _normalize_timestamp(row.get("timestamp"))
        if native_id and cwd and created_at:
            break
    if native_id is None:
        raise AgentCatalogError("missing_session_id")
    return ParsedConversation(
        native_session_id=native_id,
        title=f"Claude session {native_id[:8]}",
        cwd=cwd,
        created_at=created_at,
        updated_at=updated_at,
        metadata_truncated=truncated or rows_truncated,
    )


class AgentCatalog:
    """SQLite-backed metadata-only catalog with per-source isolation."""

    def __init__(self, database_path: str | Path, workspace_root: str | Path) -> None:
        self.database_path = Path(database_path)
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        database_resolved = self.database_path.resolve(strict=False)
        if database_resolved == self.workspace_root or self.workspace_root in database_resolved.parents:
            raise ValueError("Agent catalog database must be outside the workspace")
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(self.database_path):
            status = self.database_path.lstat()
            if self.database_path.is_symlink() or bool(
                getattr(status, "st_file_attributes", 0)
                & FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise AgentCatalogError(
                    "Agent catalog database cannot be a symlink or reparse point"
                )
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.database_path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, AGENT_CATALOG_SCHEMA_VERSION}:
                raise AgentCatalogError("Unsupported agent catalog schema version")
            self._ensure_schema(connection)
            return connection
        except AgentCatalogError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.DatabaseError as exc:
            if connection is not None:
                connection.close()
            detail = str(exc).casefold()
            if not any(
                marker in detail
                for marker in ("file is not a database", "database disk image is malformed")
            ):
                raise AgentCatalogError(
                    "Agent catalog database is temporarily unavailable"
                ) from exc
            self._recover_corrupt_database()
            recovered = sqlite3.connect(self.database_path, timeout=5)
            recovered.row_factory = sqlite3.Row
            recovered.execute("PRAGMA busy_timeout=5000")
            recovered.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema(recovered)
            return recovered

    def _recover_corrupt_database(self) -> None:
        if not self.database_path.exists():
            return
        suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        recovery = self.database_path.with_name(
            f"{self.database_path.name}.corrupt-{suffix}"
        )
        self.database_path.replace(recovery)

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS catalog_files (
                source_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                file_device INTEGER NOT NULL,
                file_inode INTEGER NOT NULL,
                fingerprint TEXT,
                parser_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT,
                conversation_ref TEXT,
                native_session_id TEXT,
                title TEXT,
                cwd TEXT,
                created_at TEXT,
                updated_at TEXT,
                metadata_truncated INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (source_id, relative_path)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS catalog_conversation_ref
                ON catalog_files(conversation_ref)
                WHERE conversation_ref IS NOT NULL;
            CREATE INDEX IF NOT EXISTS catalog_source_updated
                ON catalog_files(source_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS catalog_refreshes (
                source_id TEXT PRIMARY KEY,
                refreshed_at TEXT NOT NULL,
                partial INTEGER NOT NULL,
                scanned_files INTEGER NOT NULL,
                parsed_files INTEGER NOT NULL,
                unchanged_files INTEGER NOT NULL,
                skipped_files INTEGER NOT NULL,
                error_files INTEGER NOT NULL,
                removed_files INTEGER NOT NULL
            );
            """
        )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(catalog_files)")
        }
        if "file_device" not in columns:
            connection.execute(
                "ALTER TABLE catalog_files ADD COLUMN file_device INTEGER NOT NULL DEFAULT 0"
            )
        if "file_inode" not in columns:
            connection.execute(
                "ALTER TABLE catalog_files ADD COLUMN file_inode INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            f"PRAGMA user_version = {AGENT_CATALOG_SCHEMA_VERSION}"
        )
        connection.commit()

    def _existing(self, source_id: str) -> dict[str, sqlite3.Row]:
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM catalog_files WHERE source_id = ?", (source_id,)
            ).fetchall()
        return {str(row["relative_path"]): row for row in rows}

    @staticmethod
    def _cancel_if_requested(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("Agent catalog refresh was cancelled")

    def refresh(
        self,
        policy: AgentSourcePolicy,
        source_id: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            return self._refresh_locked(
                policy, source_id, cancel_event=cancel_event
            )

    def _refresh_locked(
        self,
        policy: AgentSourcePolicy,
        source_id: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        source = policy.get(source_id)
        jail = source.jail()
        started = time.monotonic()
        deadline = started + source.max_refresh_seconds
        existing = self._existing(source.source_id)
        seen: set[str] = set()
        updates: list[_IndexedFile] = []
        scanned_files = 0
        parsed_files = 0
        unchanged_files = 0
        skipped_files = 0
        error_files = 0
        scanned_bytes = 0
        inspected_entries = 0
        partial = False
        max_entries = min(200_000, max(1_000, source.max_files * 20))
        walk_errors: list[OSError] = []

        for current, directories, files in os.walk(
            source.root,
            topdown=True,
            onerror=walk_errors.append,
            followlinks=False,
        ):
            self._cancel_if_requested(cancel_event)
            if time.monotonic() >= deadline:
                partial = True
                break
            current_path = Path(current)
            safe_directories: list[str] = []
            for directory in directories:
                inspected_entries += 1
                if inspected_entries > max_entries:
                    partial = True
                    break
                child = current_path / directory
                try:
                    jail.resolve(jail.relative(child), must_exist=True, expect="directory")
                except (OSError, ValueError, WorkspaceSecurityError):
                    error_files += 1
                    continue
                safe_directories.append(directory)
            directories[:] = safe_directories
            if partial:
                break
            for filename in files:
                self._cancel_if_requested(cancel_event)
                inspected_entries += 1
                if inspected_entries > max_entries or time.monotonic() >= deadline:
                    partial = True
                    break
                candidate = current_path / filename
                try:
                    relative_path = jail.relative(candidate)
                except WorkspaceSecurityError:
                    error_files += 1
                    continue
                if not _is_candidate(source, relative_path):
                    continue
                if scanned_files >= source.max_files:
                    partial = True
                    break
                try:
                    checked = jail.resolve(
                        relative_path, must_exist=True, expect="file"
                    )
                    status = checked.stat()
                except (OSError, ValueError, WorkspaceSecurityError):
                    error_files += 1
                    continue
                scanned_files += 1
                seen.add(relative_path)
                size = status.st_size
                mtime_ns = status.st_mtime_ns
                try:
                    file_device, file_inode = _file_identity(status)
                except AgentCatalogError:
                    error_files += 1
                    continue
                previous = existing.get(relative_path)
                if (
                    previous is not None
                    and previous["size"] == size
                    and previous["mtime_ns"] == mtime_ns
                    and previous["file_device"] == file_device
                    and previous["file_inode"] == file_inode
                    and previous["parser_version"] == AGENT_CATALOG_PARSER_VERSION
                    and previous["source_fingerprint"]
                    == source.binding_fingerprint
                ):
                    unchanged_files += 1
                    continue
                if size > source.max_file_bytes:
                    skipped_files += 1
                    updates.append(
                        _IndexedFile(
                            source.source_id,
                            source.provider,
                            source.binding_fingerprint,
                            relative_path,
                            size,
                            mtime_ns,
                            file_device,
                            file_inode,
                            None,
                            "oversized",
                            "file_too_large",
                        )
                    )
                    continue
                if scanned_bytes + size > source.max_scan_bytes:
                    skipped_files += 1
                    updates.append(
                        _IndexedFile(
                            source.source_id,
                            source.provider,
                            source.binding_fingerprint,
                            relative_path,
                            size,
                            mtime_ns,
                            file_device,
                            file_inode,
                            None,
                            "deferred",
                            "scan_budget_exceeded",
                        )
                    )
                    partial = True
                    break
                scanned_bytes += size
                updated_at = _iso_from_epoch(status.st_mtime)
                try:
                    data, truncated = _read_metadata_bytes(checked, size)
                    after_read = checked.stat()
                    if (
                        after_read.st_size != size
                        or after_read.st_mtime_ns != mtime_ns
                    ):
                        raise AgentCatalogError("file_changed_during_metadata_read")
                    fingerprint = hashlib.sha256(
                        data + str(size).encode("ascii")
                    ).hexdigest()
                    if source.provider == "codex":
                        parsed = _parse_codex(
                            relative_path,
                            data,
                            updated_at=updated_at,
                            truncated=truncated,
                        )
                    else:
                        parsed = _parse_claude(
                            relative_path,
                            data,
                            updated_at=updated_at,
                            truncated=truncated,
                        )
                    reference = _conversation_ref(
                        source.source_id,
                        source.provider,
                        parsed.native_session_id,
                        relative_path,
                    )
                    updates.append(
                        _IndexedFile(
                            source.source_id,
                            source.provider,
                            source.binding_fingerprint,
                            relative_path,
                            size,
                            mtime_ns,
                            file_device,
                            file_inode,
                            fingerprint,
                            "ready",
                            None,
                            reference,
                            parsed.native_session_id,
                            parsed.title,
                            parsed.cwd,
                            parsed.created_at,
                            parsed.updated_at,
                            parsed.metadata_truncated,
                        )
                    )
                    parsed_files += 1
                except AgentCatalogError as exc:
                    error_files += 1
                    error_code = str(exc)
                    if error_code == "file_changed_during_metadata_read":
                        record_status = "active-writing"
                    elif error_code in {
                        "missing_session_id",
                        "no_metadata_json",
                        "session_id_mismatch",
                    }:
                        record_status = "unsupported"
                    else:
                        record_status = "corrupt"
                    updates.append(
                        _IndexedFile(
                            source.source_id,
                            source.provider,
                            source.binding_fingerprint,
                            relative_path,
                            size,
                            mtime_ns,
                            file_device,
                            file_inode,
                            None,
                            record_status,
                            error_code,
                        )
                    )
                except (OSError, UnicodeError):
                    error_files += 1
                    updates.append(
                        _IndexedFile(
                            source.source_id,
                            source.provider,
                            source.binding_fingerprint,
                            relative_path,
                            size,
                            mtime_ns,
                            file_device,
                            file_inode,
                            None,
                            "corrupt",
                            "metadata_read_failed",
                        )
                    )
            if partial:
                break
        if walk_errors:
            error_files += len(walk_errors)
            partial = True

        removed_files = 0
        refreshed_at = _iso_from_epoch(time.time())
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in updates:
                connection.execute(
                    """
                    INSERT INTO catalog_files (
                        source_id, provider, source_fingerprint, relative_path, size, mtime_ns,
                        file_device, file_inode, fingerprint, parser_version, status, error_code,
                        conversation_ref, native_session_id, title, cwd,
                        created_at, updated_at, metadata_truncated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id, relative_path) DO UPDATE SET
                        provider=excluded.provider,
                        source_fingerprint=excluded.source_fingerprint,
                        size=excluded.size,
                        mtime_ns=excluded.mtime_ns,
                        file_device=excluded.file_device,
                        file_inode=excluded.file_inode,
                        fingerprint=excluded.fingerprint,
                        parser_version=excluded.parser_version,
                        status=excluded.status,
                        error_code=excluded.error_code,
                        conversation_ref=excluded.conversation_ref,
                        native_session_id=excluded.native_session_id,
                        title=excluded.title,
                        cwd=excluded.cwd,
                        created_at=excluded.created_at,
                        updated_at=excluded.updated_at,
                        metadata_truncated=excluded.metadata_truncated
                    """,
                    (
                        item.source_id,
                        item.provider,
                        item.source_fingerprint,
                        item.relative_path,
                        item.size,
                        item.mtime_ns,
                        item.file_device,
                        item.file_inode,
                        item.fingerprint,
                        AGENT_CATALOG_PARSER_VERSION,
                        item.status,
                        item.error_code,
                        item.conversation_ref,
                        item.native_session_id,
                        item.title,
                        item.cwd,
                        item.created_at,
                        item.updated_at,
                        int(item.metadata_truncated),
                    ),
                )
            if not partial:
                stale = set(existing) - seen
                for relative_path in stale:
                    connection.execute(
                        "DELETE FROM catalog_files WHERE source_id = ? AND relative_path = ?",
                        (source.source_id, relative_path),
                    )
                removed_files = len(stale)
            connection.execute(
                """
                INSERT INTO catalog_refreshes (
                    source_id, refreshed_at, partial, scanned_files,
                    parsed_files, unchanged_files, skipped_files,
                    error_files, removed_files
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    refreshed_at=excluded.refreshed_at,
                    partial=excluded.partial,
                    scanned_files=excluded.scanned_files,
                    parsed_files=excluded.parsed_files,
                    unchanged_files=excluded.unchanged_files,
                    skipped_files=excluded.skipped_files,
                    error_files=excluded.error_files,
                    removed_files=excluded.removed_files
                """,
                (
                    source.source_id,
                    refreshed_at,
                    int(partial),
                    scanned_files,
                    parsed_files,
                    unchanged_files,
                    skipped_files,
                    error_files,
                    removed_files,
                ),
            )
            connection.commit()
        return {
            "source_id": source.source_id,
            "provider": source.provider,
            "state": "partial" if partial else "complete",
            "partial": partial,
            "scanned_files": scanned_files,
            "parsed_files": parsed_files,
            "unchanged_files": unchanged_files,
            "skipped_files": skipped_files,
            "error_files": error_files,
            "removed_files": removed_files,
            "scanned_bytes": scanned_bytes,
            "refreshed_at": refreshed_at,
        }

    def source_summaries(self, policy: AgentSourcePolicy) -> list[dict[str, Any]]:
        refreshes: dict[str, sqlite3.Row] = {}
        status_counts: dict[str, dict[str, int]] = {}
        if self.database_path.exists():
            with self._lock, closing(self._connect()) as connection:
                rows = connection.execute("SELECT * FROM catalog_refreshes").fetchall()
                refreshes = {str(row["source_id"]): row for row in rows}
                count_rows = connection.execute(
                    """
                    SELECT source_id, status, COUNT(*) AS record_count
                    FROM catalog_files
                    GROUP BY source_id, status
                    """
                ).fetchall()
                for row in count_rows:
                    status_counts.setdefault(str(row["source_id"]), {})[
                        str(row["status"])
                    ] = int(row["record_count"])
        results: list[dict[str, Any]] = []
        for source in policy.sources:
            refresh = refreshes.get(source.source_id)
            results.append(
                {
                    **source.as_dict(),
                    "record_status_counts": status_counts.get(
                        source.source_id, {}
                    ),
                    "last_refresh": (
                        None
                        if refresh is None
                        else {
                            "refreshed_at": refresh["refreshed_at"],
                            "partial": bool(refresh["partial"]),
                            "scanned_files": refresh["scanned_files"],
                            "parsed_files": refresh["parsed_files"],
                            "unchanged_files": refresh["unchanged_files"],
                            "skipped_files": refresh["skipped_files"],
                            "error_files": refresh["error_files"],
                            "removed_files": refresh["removed_files"],
                        }
                    ),
                }
            )
        return results

    def _display_cwd(self, value: str | None) -> str | None:
        if not value:
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            return None
        try:
            relative = candidate.resolve(strict=False).relative_to(self.workspace_root)
        except (OSError, ValueError):
            return None
        text = relative.as_posix()
        return "." if text == "." else text

    def _record_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "conversation_ref": row["conversation_ref"],
            "provider": row["provider"],
            "native_session_id": row["native_session_id"],
            "title": row["title"],
            "cwd": self._display_cwd(row["cwd"]),
            # Raw recorded path, for server-side authorization only. The
            # service strips this before the record reaches an MCP response.
            "cwd_absolute": row["cwd"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "source_id": row["source_id"],
            "resumable": True,
            "attachable": False,
            "status": row["status"],
            "metadata_truncated": bool(row["metadata_truncated"]),
        }

    @staticmethod
    def _authorize_record(
        policy: AgentSourcePolicy,
        row: sqlite3.Row,
        *,
        require_unchanged_metadata: bool = True,
    ) -> Path:
        source = policy.get(str(row["source_id"]))
        if source.provider != row["provider"]:
            raise PermissionError("Agent conversation provider binding is invalid")
        if source.binding_fingerprint != row["source_fingerprint"]:
            raise PermissionError("Agent conversation source binding is stale")
        checked = source.jail().resolve(
            str(row["relative_path"]), must_exist=True, expect="file"
        )
        status = checked.stat()
        try:
            file_device, file_inode = _file_identity(status)
        except AgentCatalogError as exc:
            raise PermissionError(
                "Agent conversation file identity is unsupported"
            ) from exc
        if file_device != row["file_device"] or file_inode != row["file_inode"]:
            raise PermissionError(
                "Agent conversation file identity changed since the last catalog refresh"
            )
        if require_unchanged_metadata and (
            status.st_size != row["size"]
            or status.st_mtime_ns != row["mtime_ns"]
        ):
            raise PermissionError(
                "Agent conversation file changed since the last catalog refresh"
            )
        return checked

    def list_records(
        self,
        policy: AgentSourcePolicy,
        *,
        provider: str = "",
        source_id: str = "",
        query: str = "",
        cursor: int = 0,
        limit: int = 50,
        max_bytes: int = DEFAULT_CATALOG_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        if (
            isinstance(cursor, bool)
            or not isinstance(cursor, int)
            or not 0 <= cursor <= 1_000_000
        ):
            raise AgentCatalogError("cursor must be between 0 and 1000000")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CATALOG_RESULTS:
            raise AgentCatalogError(f"limit must be between 1 and {MAX_CATALOG_RESULTS}")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1024 <= max_bytes <= MAX_CATALOG_OUTPUT_BYTES
        ):
            raise AgentCatalogError(
                f"max_bytes must be between 1024 and {MAX_CATALOG_OUTPUT_BYTES}"
            )
        if not isinstance(query, str) or len(query) > 200:
            raise AgentCatalogError("query is limited to 200 characters")
        enabled = [source for source in policy.sources if source.enabled]
        if source_id:
            selected = policy.get(source_id)
            enabled = [selected]
        if provider:
            if provider not in {"codex", "claude-code"}:
                raise AgentCatalogError("provider must be codex or claude-code")
            enabled = [source for source in enabled if source.provider == provider]
        if not enabled or not self.database_path.exists():
            return {
                "conversations": [],
                "count": 0,
                "cursor": cursor,
                "next_cursor": None,
                "bytes_used": 0,
                "max_bytes": max_bytes,
                "required_bytes_for_next_record": None,
                "truncated": False,
                "unavailable_records_skipped": 0,
                "active_writing_records_skipped": 0,
            }
        source_ids = [source.source_id for source in enabled]
        placeholders = ",".join("?" for _ in source_ids)
        parameters: list[Any] = [*source_ids]
        where = [f"source_id IN ({placeholders})", "status = 'ready'"]
        if query:
            where.append(
                "(title LIKE ? ESCAPE '\\' OR native_session_id LIKE ? ESCAPE '\\')"
            )
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.extend((f"%{escaped}%", f"%{escaped}%"))
        parameters.extend((limit + 1, cursor))
        sql = (
            "SELECT * FROM catalog_files WHERE "
            + " AND ".join(where)
            + " ORDER BY updated_at DESC, conversation_ref ASC LIMIT ? OFFSET ?"
        )
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        has_more = len(rows) > limit
        selected_payloads: list[dict[str, Any]] = []
        bytes_used = 0
        required_bytes_for_next_record: int | None = None
        next_offset = cursor
        unavailable_records_skipped = 0
        active_writing_records_skipped = 0
        for index, row in enumerate(rows):
            try:
                self._authorize_record(policy, row)
            except PermissionError as exc:
                unavailable_records_skipped += 1
                if "changed since the last catalog refresh" in str(exc):
                    active_writing_records_skipped += 1
                next_offset = cursor + index + 1
                continue
            except (OSError, ValueError, WorkspaceSecurityError):
                unavailable_records_skipped += 1
                next_offset = cursor + index + 1
                continue
            payload = self._record_payload(row)
            size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            if bytes_used + size > max_bytes:
                required_bytes_for_next_record = size
                has_more = True
                break
            selected_payloads.append(payload)
            bytes_used += size
            next_offset = cursor + index + 1
            if len(selected_payloads) >= limit:
                break
        return {
            "conversations": selected_payloads,
            "count": len(selected_payloads),
            "cursor": cursor,
            "next_cursor": next_offset if has_more else None,
            "bytes_used": bytes_used,
            "max_bytes": max_bytes,
            "required_bytes_for_next_record": required_bytes_for_next_record,
            "truncated": has_more,
            "unavailable_records_skipped": unavailable_records_skipped,
            "active_writing_records_skipped": active_writing_records_skipped,
        }

    def inspect_record(
        self, policy: AgentSourcePolicy, conversation_ref: str
    ) -> dict[str, Any]:
        if not isinstance(conversation_ref, str) or not _CONVERSATION_REF.fullmatch(
            conversation_ref
        ):
            raise AgentCatalogError("conversation_ref is invalid")
        if not self.database_path.exists():
            raise FileNotFoundError("Agent conversation was not found")
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM catalog_files WHERE conversation_ref = ? AND status = 'ready'",
                (conversation_ref,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("Agent conversation was not found")
        # Revalidate the source root and indexed file on every inspect so a
        # path replacement, deletion, or reparse change invalidates old rows.
        self._authorize_record(policy, row)
        return self._record_payload(row)

    def authorize_attachment(
        self, policy: AgentSourcePolicy, conversation_ref: str
    ) -> dict[str, Any]:
        """Revalidate one catalog binding without requiring an unchanged file size.

        Native agent histories are append-only during normal use.  A bound
        TianCheng session may therefore continue after the same file grows, but
        source/root/file identity and the bounded provider metadata must still
        match the indexed record on every run.
        """

        if not isinstance(conversation_ref, str) or not _CONVERSATION_REF.fullmatch(
            conversation_ref
        ):
            raise AgentCatalogError("conversation_ref is invalid")
        if not self.database_path.exists():
            raise FileNotFoundError("Agent conversation was not found")
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM catalog_files WHERE conversation_ref = ? AND status = 'ready'",
                (conversation_ref,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("Agent conversation was not found")
        checked = self._authorize_record(
            policy, row, require_unchanged_metadata=False
        )
        before = checked.stat()
        data, truncated = _read_metadata_bytes(checked, before.st_size)
        after = checked.stat()
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise PermissionError(
                "Agent conversation changed during attachment validation"
            )
        updated_at = _iso_from_epoch(after.st_mtime)
        relative_path = str(row["relative_path"])
        if row["provider"] == "codex":
            parsed = _parse_codex(
                relative_path,
                data,
                updated_at=updated_at,
                truncated=truncated,
            )
        elif row["provider"] == "claude-code":
            parsed = _parse_claude(
                relative_path,
                data,
                updated_at=updated_at,
                truncated=truncated,
            )
        else:
            raise PermissionError("Agent conversation provider is unsupported")
        if parsed.native_session_id != row["native_session_id"]:
            raise PermissionError("Agent conversation native session binding changed")
        if parsed.cwd != row["cwd"]:
            raise PermissionError("Agent conversation working directory binding changed")
        return self._record_payload(row)
