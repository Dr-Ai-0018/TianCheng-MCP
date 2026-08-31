from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from tiancheng_mcp import agent_catalog
from tiancheng_mcp.agent_catalog import AgentCatalog
from tiancheng_mcp.agent_sources import AgentSourcePolicy
from tiancheng_mcp.jobs import JobCancelled
from tiancheng_mcp.policy import AccessPolicy
from tiancheng_mcp.service import TianChengService


def _catalog_fixture(tmp_path: Path) -> tuple[Path, AgentSourcePolicy, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex_root = tmp_path / ".codex" / "sessions"
    claude_root = tmp_path / ".claude" / "projects"
    codex_root.mkdir(parents=True)
    claude_root.mkdir(parents=True)
    policy = AgentSourcePolicy.from_payload(
        {
            "schema_version": 1,
            "sources": [
                {
                    "source_id": "src_codex_test",
                    "provider": "codex",
                    "root": str(codex_root),
                    "mode": "catalog-read",
                    "enabled": True,
                },
                {
                    "source_id": "src_claude_test",
                    "provider": "claude-code",
                    "root": str(claude_root),
                    "mode": "catalog-read",
                    "enabled": True,
                },
            ],
        }
    )
    return workspace, policy, codex_root, claude_root


def _write_codex(path: Path, session_id: str, cwd: Path, secret: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": "2026-08-29T10:00:00Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(cwd)},
        },
        {
            "type": "response_item",
            "payload": {"role": "user", "content": secret},
        },
    ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _write_claude(path: Path, session_id: str, cwd: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": session_id,
                "cwd": str(cwd),
                "timestamp": "2026-08-29T11:00:00Z",
                "message": {"role": "user", "content": "private transcript"},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_catalog_refresh_indexes_only_bounded_metadata(tmp_path: Path) -> None:
    workspace, policy, codex_root, claude_root = _catalog_fixture(tmp_path)
    canary = "TOP-SECRET-TRANSCRIPT-CANARY"
    codex_file = codex_root / "2026" / "08" / "29" / "rollout-test.jsonl"
    claude_file = claude_root / "project" / "claude_session_1.jsonl"
    _write_codex(codex_file, "codex_session_1", workspace, canary)
    _write_claude(claude_file, "claude_session_1", tmp_path / "external-project")
    database = tmp_path / "state" / "catalog.sqlite3"
    catalog = AgentCatalog(database, workspace)

    codex_result = catalog.refresh(policy, "src_codex_test")
    claude_result = catalog.refresh(policy, "src_claude_test")
    assert codex_result["state"] == "complete"
    assert codex_result["parsed_files"] == 1
    assert claude_result["parsed_files"] == 1

    page = catalog.list_records(policy, limit=10)
    assert page["count"] == 2
    records = {item["provider"]: item for item in page["conversations"]}
    assert records["codex"]["native_session_id"] == "codex_session_1"
    assert records["codex"]["cwd"] == "."
    assert records["claude-code"]["cwd"] is None
    assert records["codex"]["attachable"] is False
    assert canary.encode() not in database.read_bytes()
    assert b"private transcript" not in database.read_bytes()
    inspected = catalog.inspect_record(
        policy, records["codex"]["conversation_ref"]
    )
    assert inspected == records["codex"]


def test_catalog_incremental_refresh_skips_unchanged_and_removes_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, policy, codex_root, _ = _catalog_fixture(tmp_path)
    rollout = codex_root / "2026" / "08" / "29" / "rollout-incremental.jsonl"
    _write_codex(rollout, "codex_incremental", workspace)
    catalog = AgentCatalog(tmp_path / "state" / "catalog.sqlite3", workspace)
    assert catalog.refresh(policy, "src_codex_test")["parsed_files"] == 1

    def must_not_read(*args, **kwargs):
        raise AssertionError("unchanged files must not be reopened")

    monkeypatch.setattr(agent_catalog, "_read_metadata_bytes", must_not_read)
    unchanged = catalog.refresh(policy, "src_codex_test")
    assert unchanged["unchanged_files"] == 1
    assert unchanged["parsed_files"] == 0

    rollout.unlink()
    removed = catalog.refresh(policy, "src_codex_test")
    assert removed["removed_files"] == 1
    assert catalog.list_records(policy)["count"] == 0


def test_catalog_isolates_corrupt_oversized_and_excluded_files(tmp_path: Path) -> None:
    workspace, _, _, claude_root = _catalog_fixture(tmp_path)
    policy = AgentSourcePolicy.from_payload(
        {
            "schema_version": 1,
            "sources": [
                {
                    "source_id": "src_claude_small",
                    "provider": "claude-code",
                    "root": str(claude_root),
                    "mode": "catalog-read",
                    "max_file_bytes": 1024,
                }
            ],
        }
    )
    corrupt = claude_root / "project" / "corrupt.jsonl"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("not json\n", encoding="utf-8")
    invalid_utf8 = claude_root / "project" / "invalid_utf8.jsonl"
    invalid_utf8.write_bytes(b"\xff\xfe\xfd\n")
    oversized = claude_root / "project" / "oversized.jsonl"
    oversized.write_text("x" * 2048, encoding="utf-8")
    excluded = claude_root / "project" / "subagents" / "hidden.jsonl"
    _write_claude(excluded, "hidden", workspace)

    catalog = AgentCatalog(tmp_path / "state" / "catalog.sqlite3", workspace)
    result = catalog.refresh(policy, "src_claude_small")
    assert result["error_files"] == 2
    assert result["skipped_files"] == 1
    assert result["scanned_files"] == 3
    assert catalog.list_records(policy)["count"] == 0
    summary = catalog.source_summaries(policy)[0]
    assert summary["last_refresh"]["error_files"] == 2
    assert summary["record_status_counts"] == {
        "corrupt": 1,
        "oversized": 1,
        "unsupported": 1,
    }


def test_catalog_cancel_and_corrupt_database_recovery(tmp_path: Path) -> None:
    workspace, policy, _, _ = _catalog_fixture(tmp_path)
    database = tmp_path / "state" / "catalog.sqlite3"
    database.parent.mkdir()
    database.write_bytes(b"not a sqlite database")
    catalog = AgentCatalog(database, workspace)
    summaries = catalog.source_summaries(policy)
    assert len(summaries) == 2
    assert list(database.parent.glob("catalog.sqlite3.corrupt-*"))

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(JobCancelled):
        catalog.refresh(policy, "src_codex_test", cancel_event=cancelled)


def test_catalog_migrates_pre_identity_schema_before_query(tmp_path: Path) -> None:
    workspace, policy, _, _ = _catalog_fixture(tmp_path)
    database = tmp_path / "state" / "catalog.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE catalog_files (
                source_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
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
            PRAGMA user_version = 1;
            """
        )

    catalog = AgentCatalog(database, workspace)
    assert catalog.list_records(policy)["count"] == 0
    with sqlite3.connect(database) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(catalog_files)")
        }
        assert {"file_device", "file_inode"} <= columns
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_catalog_file_identity_encodes_unsigned_windows_values_losslessly() -> None:
    assert agent_catalog._sqlite_file_identity(0) == 0
    assert agent_catalog._sqlite_file_identity((1 << 63) - 1) == (1 << 63) - 1
    assert agent_catalog._sqlite_file_identity(1 << 63) == -(1 << 63)
    assert agent_catalog._sqlite_file_identity((1 << 64) - 1) == -1
    with pytest.raises(
        agent_catalog.AgentCatalogError, match="unsupported_file_identity"
    ):
        agent_catalog._sqlite_file_identity(1 << 64)


def test_catalog_walk_error_is_partial_and_preserves_existing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, policy, codex_root, _ = _catalog_fixture(tmp_path)
    rollout = codex_root / "2026" / "08" / "29" / "rollout-walk-error.jsonl"
    _write_codex(rollout, "codex_walk_error", workspace)
    catalog = AgentCatalog(tmp_path / "state" / "catalog.sqlite3", workspace)
    catalog.refresh(policy, "src_codex_test")
    record = catalog.list_records(policy)["conversations"][0]

    def failing_walk(
        _root: Path,
        *,
        topdown: bool,
        onerror: object,
        followlinks: bool,
    ) -> list[tuple[str, list[str], list[str]]]:
        assert topdown is True
        assert followlinks is False
        assert callable(onerror)
        onerror(PermissionError("fixture walk failure"))
        return []

    monkeypatch.setattr(agent_catalog.os, "walk", failing_walk)
    result = catalog.refresh(policy, "src_codex_test")
    assert result["state"] == "partial"
    assert result["error_files"] == 1
    assert result["removed_files"] == 0
    assert catalog.inspect_record(policy, record["conversation_ref"])[
        "native_session_id"
    ] == "codex_walk_error"


def test_catalog_queries_fail_closed_after_source_file_disappears(
    tmp_path: Path,
) -> None:
    workspace, policy, codex_root, _ = _catalog_fixture(tmp_path)
    rollout = codex_root / "2026" / "08" / "29" / "rollout-stale.jsonl"
    _write_codex(rollout, "codex_stale", workspace)
    catalog = AgentCatalog(tmp_path / "state" / "catalog.sqlite3", workspace)
    catalog.refresh(policy, "src_codex_test")
    record = catalog.list_records(policy)["conversations"][0]

    rollout.unlink()
    assert catalog.list_records(policy)["count"] == 0
    with pytest.raises(FileNotFoundError):
        catalog.inspect_record(policy, record["conversation_ref"])


def test_catalog_reports_active_writing_and_stale_ready_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, policy, codex_root, _ = _catalog_fixture(tmp_path)
    rollout = codex_root / "2026" / "08" / "29" / "rollout-active.jsonl"
    _write_codex(rollout, "codex_active", workspace)
    catalog = AgentCatalog(tmp_path / "state" / "catalog.sqlite3", workspace)
    catalog.refresh(policy, "src_codex_test")

    with rollout.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    stale = catalog.list_records(policy)
    assert stale["count"] == 0
    assert stale["unavailable_records_skipped"] == 1
    assert stale["active_writing_records_skipped"] == 1

    original_read = agent_catalog._read_metadata_bytes

    def changing_read(path: Path, size: int) -> tuple[bytes, bool]:
        result = original_read(path, size)
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        return result

    monkeypatch.setattr(agent_catalog, "_read_metadata_bytes", changing_read)
    refreshed = catalog.refresh(policy, "src_codex_test")
    assert refreshed["error_files"] == 1
    summary = catalog.source_summaries(policy)[0]
    assert summary["record_status_counts"] == {"active-writing": 1}


def test_catalog_invalidates_rows_when_source_root_identity_changes(
    tmp_path: Path,
) -> None:
    workspace, policy, codex_root, _ = _catalog_fixture(tmp_path)
    rollout = codex_root / "2026" / "08" / "29" / "rollout-binding.jsonl"
    _write_codex(rollout, "codex_binding", workspace)
    catalog = AgentCatalog(tmp_path / "state" / "catalog.sqlite3", workspace)
    catalog.refresh(policy, "src_codex_test")
    record = catalog.list_records(policy)["conversations"][0]

    previous_root = codex_root.with_name("sessions-previous")
    codex_root.rename(previous_root)
    codex_root.mkdir()
    with pytest.raises(PermissionError, match="identity changed"):
        catalog.inspect_record(policy, record["conversation_ref"])
    assert catalog.list_records(policy)["count"] == 0


def test_catalog_invalidates_rows_when_indexed_file_identity_changes(
    tmp_path: Path,
) -> None:
    workspace, policy, codex_root, _ = _catalog_fixture(tmp_path)
    rollout = codex_root / "2026" / "08" / "29" / "rollout-file-binding.jsonl"
    _write_codex(rollout, "codex_file_binding", workspace)
    catalog = AgentCatalog(tmp_path / "state" / "catalog.sqlite3", workspace)
    catalog.refresh(policy, "src_codex_test")
    record = catalog.list_records(policy)["conversations"][0]
    original = rollout.stat()

    preserved = rollout.with_suffix(".preserved")
    rollout.rename(preserved)
    _write_codex(rollout, "codex_file_binding", workspace)
    os.utime(rollout, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert rollout.stat().st_size == original.st_size

    with pytest.raises(PermissionError, match="changed since"):
        catalog.inspect_record(policy, record["conversation_ref"])
    assert catalog.list_records(policy)["count"] == 0

    refreshed = catalog.refresh(policy, "src_codex_test")
    assert refreshed["parsed_files"] == 1
    assert catalog.inspect_record(policy, record["conversation_ref"])[
        "native_session_id"
    ] == "codex_file_binding"


def test_catalog_list_has_stable_cursor_query_and_byte_budget(tmp_path: Path) -> None:
    workspace, policy, codex_root, _ = _catalog_fixture(tmp_path)
    for index in range(5):
        rollout = (
            codex_root
            / "2026"
            / "08"
            / "29"
            / f"rollout-page-{index}.jsonl"
        )
        _write_codex(rollout, f"codex_page_{index}", workspace)
    catalog = AgentCatalog(tmp_path / "state" / "catalog.sqlite3", workspace)
    catalog.refresh(policy, "src_codex_test")

    first = catalog.list_records(policy, limit=2, max_bytes=1024)
    assert 1 <= first["count"] <= 2
    assert first["next_cursor"] is not None
    assert first["bytes_used"] <= 1024
    assert first["truncated"] is True
    second = catalog.list_records(
        policy, cursor=first["next_cursor"], limit=2, max_bytes=1024
    )
    assert {
        item["conversation_ref"] for item in first["conversations"]
    }.isdisjoint(
        item["conversation_ref"] for item in second["conversations"]
    )
    queried = catalog.list_records(policy, query="codex_page_3")
    assert queried["count"] == 1
    assert queried["conversations"][0]["native_session_id"] == "codex_page_3"
    with pytest.raises(ValueError, match="max_bytes"):
        catalog.list_records(policy, max_bytes=100)


def test_service_exposes_catalog_without_general_source_file_access(
    tmp_path: Path,
) -> None:
    workspace, policy, codex_root, _ = _catalog_fixture(tmp_path)
    rollout = codex_root / "2026" / "08" / "29" / "rollout-service.jsonl"
    _write_codex(rollout, "codex_service", workspace)
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        access_policy=AccessPolicy.default(workspace),
        agent_source_policy=policy,
        agent_catalog_path=tmp_path / "state" / "service-catalog.sqlite3",
    )

    providers = service.agent_catalog_providers()
    assert providers["count"] == 2
    assert providers["providers"][0]["catalog_parser"] is True
    assert providers["providers"][0]["capabilities"]["discover"] is True
    sources = service.agent_catalog_sources()
    assert sources["count"] == 2
    assert all("root" not in source for source in sources["sources"])
    refreshed = service.agent_catalog_refresh("src_codex_test")
    assert refreshed["parsed_files"] == 1
    listed = service.agent_catalog_list(provider="codex")
    assert listed["count"] == 1
    inspected = service.agent_catalog_inspect(
        listed["conversations"][0]["conversation_ref"]
    )
    assert inspected["native_session_id"] == "codex_service"
    assert service.workspace_info()["agent_sources"]["enabled_source_count"] == 2

    scoped = TianChengService(
        tmp_path / "scoped-workspace",
        None,
        access_policy=AccessPolicy.default(workspace),
        enable_agent_catalog=False,
    )
    assert scoped.workspace_info()["capabilities"]["local_agent_catalog"] is False
    with pytest.raises(RuntimeError, match="disabled"):
        scoped.agent_catalog_sources()
