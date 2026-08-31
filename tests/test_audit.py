from __future__ import annotations

from tiancheng_mcp.audit import AuditLogger


def test_audit_log_rotates_without_recording_content(tmp_path) -> None:
    logger = AuditLogger(tmp_path / "audit", max_bytes=180, backup_count=2)
    for index in range(10):
        logger.record(
            tool="write_text",
            relative_path=f"file-{index}.txt",
            success=True,
            duration_ms=1.5,
        )
    assert logger.path.exists()
    assert logger.path.with_name(f"{logger.path.name}.1").exists()
    assert len(list(logger.directory.glob("*.jsonl*"))) <= 3
