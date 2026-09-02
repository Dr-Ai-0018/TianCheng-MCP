from __future__ import annotations

import pytest

from tiancheng_mcp.audit import AuditLogger
from tiancheng_mcp.service import TianChengService


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


def test_audit_write_failure_does_not_mask_success(tmp_path, capsys, monkeypatch) -> None:
    service = TianChengService(tmp_path / "workspace", tmp_path / "audit")
    side_effect = tmp_path / "side-effect.txt"

    def fail_audit(**_event) -> None:
        raise OSError("private audit path")

    monkeypatch.setattr(service.audit, "record", fail_audit)
    result = service.audited(
        "write_text",
        "side-effect.txt",
        lambda: (side_effect.write_text("done", encoding="utf-8"), {"ok": True})[1],
    )
    assert result == {"ok": True}
    assert side_effect.read_text(encoding="utf-8") == "done"
    warning = capsys.readouterr().err
    assert "audit record failed (OSError)" in warning
    assert "private audit path" not in warning


def test_audit_write_failure_preserves_original_operation_error(
    tmp_path, capsys, monkeypatch
) -> None:
    service = TianChengService(tmp_path / "workspace", tmp_path / "audit")

    def fail_audit(**_event) -> None:
        raise OSError("private audit path")

    def fail_operation():
        raise ValueError("original business failure")

    monkeypatch.setattr(service.audit, "record", fail_audit)
    with pytest.raises(ValueError, match="original business failure"):
        service.audited("write_text", "probe.txt", fail_operation)
    warning = capsys.readouterr().err
    assert "audit record failed (OSError)" in warning
    assert "private audit path" not in warning
