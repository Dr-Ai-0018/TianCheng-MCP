"""Minimal JSON-lines audit logging. Never records file content or arguments."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(
        self,
        directory: str | Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "tiancheng-mcp-audit.jsonl"
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.Lock()

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if not self.path.exists() or self.path.stat().st_size + incoming_bytes <= self.max_bytes:
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def record(
        self,
        *,
        tool: str,
        relative_path: str | None,
        success: bool,
        duration_ms: float,
        error_type: str | None = None,
        job_id: str | None = None,
        state: str | None = None,
        reason: str | None = None,
        output_truncated: bool | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "tool": tool,
            "relative_path": relative_path,
            "success": success,
            "duration_ms": round(duration_ms, 3),
        }
        if error_type:
            event["error_type"] = error_type
        if job_id:
            event["job_id"] = job_id
        if state:
            event["state"] = state
        if reason:
            event["reason"] = reason[:200]
        if output_truncated is not None:
            event["output_truncated"] = bool(output_truncated)
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._rotate_if_needed(len((encoded + "\n").encode("utf-8")))
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded + "\n")
