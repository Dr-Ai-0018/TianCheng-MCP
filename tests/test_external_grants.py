from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from tiancheng_mcp.jobs import JobCancelled
from tiancheng_mcp.service import TianChengService


SECRET = base64.b32encode(b"tiancheng-test-secret-123").decode()


def test_chat_approved_external_grant_read_write_and_revoke(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    external.mkdir()
    (external / "中文.txt").write_text("hello", encoding="utf-8")
    (external / "nested").mkdir()
    (external / "nested" / "note.md").write_text("needle", encoding="utf-8")
    service = TianChengService(workspace, tmp_path / "audit", allow_external_grants=True, totp_secret=SECRET)

    pending = service.request_external_access(str(external), "write", 600, "test")
    assert pending["status"] == "pending"
    grant = service.approve_external_access(str(pending["request_id"]), str(pending["challenge"]), "批准")
    grant_id = str(grant["grant_id"])
    assert service.external_read_text(grant_id, "中文.txt")["content"] == "hello"
    rooted = service.external_glob(grant_id, "*.md", base_path="nested")
    assert [item["path"] for item in rooted["results"]] == ["nested/note.md"]
    rooted_search = service.external_search_text(
        grant_id, "needle", glob_pattern="*.md", base_path="nested"
    )
    assert [item["path"] for item in rooted_search["results"]] == ["nested/note.md"]
    service.external_write_text(grant_id, "new.txt", "世界")
    assert (external / "new.txt").read_text(encoding="utf-8") == "世界"
    service.revoke_external_access(grant_id)
    with pytest.raises(PermissionError):
        service.external_stat(grant_id, "new.txt")


def test_external_grant_rejects_escape_and_replay(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    external.mkdir()
    service = TianChengService(workspace, tmp_path / "audit", allow_external_grants=True, totp_secret=SECRET)
    pending = service.request_external_access(str(external), "read")
    request_id = str(pending["request_id"])
    with pytest.raises(PermissionError):
        service.approve_external_access(request_id, str(pending["challenge"]))
    service.approve_external_access(request_id, str(pending["challenge"]), "批准")
    with pytest.raises(PermissionError):
        service.approve_external_access(request_id, str(pending["challenge"]), "批准")
    pending = service.request_external_access(str(external), "read")
    grant = service.approve_external_access(str(pending["request_id"]), str(pending["challenge"]), "批准")
    with pytest.raises(Exception):
        service.external_stat(str(grant["grant_id"]), "..")


def test_external_grants_disabled_by_default(tmp_path: Path) -> None:
    service = TianChengService(tmp_path / "workspace", tmp_path / "audit")
    with pytest.raises(PermissionError):
        service.request_external_access(str(tmp_path), "read")


def test_static_policy_can_issue_no_approval_external_grant(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "approved"
    external.mkdir()
    policy_file = tmp_path / "access-policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "rules": [
                    {"path": str(workspace), "mode": "full"},
                    {
                        "path": str(external),
                        "mode": "write",
                        "require_approval": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_external_grants=True,
        access_policy_path=policy_file,
    )
    try:
        grant = service.request_external_access(str(external), "write")
        assert grant["status"] == "approved"
        assert grant["approval_required"] is False
        service.external_write_text(str(grant["grant_id"]), "中文.txt", "已授权")
        assert (external / "中文.txt").read_text(encoding="utf-8") == "已授权"
    finally:
        service.shutdown()


def test_static_deny_rule_cannot_be_overridden_by_grant_request(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "blocked"
    external.mkdir()
    policy_file = tmp_path / "access-policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "rules": [
                    {"path": str(workspace), "mode": "full"},
                    {"path": str(external), "mode": "deny"},
                ]
            }
        ),
        encoding="utf-8",
    )
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_external_grants=True,
        access_policy_path=policy_file,
    )
    try:
        with pytest.raises(PermissionError, match="denies"):
            service.request_external_access(str(external), "read")
    finally:
        service.shutdown()


def test_static_policy_direct_external_tools_use_absolute_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "approved"
    external.mkdir()
    policy_file = tmp_path / "access-policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "rules": [
                    {"path": str(workspace), "mode": "full"},
                    {"path": str(external), "mode": "write", "require_approval": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_external_grants=True,
        access_policy_path=policy_file,
    )
    try:
        written = service.policy_external_write_text(str(external / "中文.txt"), "直接白名单")
        assert written["path"] == "中文.txt"
        read = service.policy_external_read_text(str(external / "中文.txt"))
        assert read["content"] == "直接白名单"
    finally:
        service.shutdown()


def test_static_policy_move_rejects_cross_rule_with_explanation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    (source_root / "a.txt").write_text("a", encoding="utf-8")
    policy_file = tmp_path / "access-policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "rules": [
                    {"path": str(workspace), "mode": "full"},
                    {"path": str(source_root), "mode": "write"},
                    {"path": str(destination_root), "mode": "write"},
                ]
            }
        ),
        encoding="utf-8",
    )
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_external_grants=True,
        access_policy_path=policy_file,
    )
    try:
        with pytest.raises(PermissionError, match="share one compatible static policy rule"):
            service.policy_external_move(str(source_root / "a.txt"), str(destination_root / "a.txt"))
    finally:
        service.shutdown()


def test_pending_request_can_be_cancelled(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    service = TianChengService(tmp_path / "workspace", tmp_path / "audit", allow_external_grants=True, totp_secret=SECRET)
    pending = service.request_external_access(str(external), "read")
    cancelled = service.cancel_external_access_request(str(pending["request_id"]))
    assert cancelled["cancelled"] is True
    assert service.external_grant_status()["pending"] == []


def test_revoke_external_grant_cancels_associated_job(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    service = TianChengService(
        tmp_path / "workspace",
        tmp_path / "audit",
        allow_external_grants=True,
        totp_secret=SECRET,
        interactive_timeout_seconds=1,
    )
    try:
        pending = service.request_external_access(str(external), "read")
        grant = service.approve_external_access(
            str(pending["request_id"]), str(pending["challenge"]), "批准"
        )
        grant_id = str(grant["grant_id"])

        def slow_operation():
            # The real scanners use the same context token; this loop models a
            # long external operation and proves revoke reaches its worker.
            while True:
                event = service._cancel_event()
                if event is not None and event.wait(0.01):
                    raise JobCancelled("grant revoked")

        response = service.run_with_fallback(
            "external_synthetic",
            "<external-grant>",
            slow_operation,
            metadata={"grant_id": grant_id},
        )
        assert response["execution"] == "background"
        service.revoke_external_access(grant_id)
        job_id = str(response["job_id"])
        assert service.jobs is not None
        assert service.jobs.get(job_id).done.wait(2)
        assert service.job_status(job_id)["state"] == "cancelled"
    finally:
        service.shutdown()


def test_revoke_external_grant_hides_completed_job_result(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    service = TianChengService(
        tmp_path / "workspace",
        tmp_path / "audit",
        allow_external_grants=True,
        totp_secret=SECRET,
    )
    try:
        pending = service.request_external_access(str(external), "read")
        grant = service.approve_external_access(
            str(pending["request_id"]), str(pending["challenge"]), "批准"
        )
        grant_id = str(grant["grant_id"])
        assert service.jobs is not None
        record = service.jobs.submit(
            "external_done", lambda _cancel: {"secret_like": "not returned"},
            metadata={"grant_id": grant_id},
        )
        assert record.done.wait(2)
        service.revoke_external_access(grant_id)
        result = service.job_result(record.job_id)
        assert result["state"] == "expired"
        assert "result" not in result
    finally:
        service.shutdown()


def test_expired_external_grant_cancels_running_job(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    service = TianChengService(
        tmp_path / "workspace",
        tmp_path / "audit",
        allow_external_grants=True,
        totp_secret=SECRET,
        interactive_timeout_seconds=1,
    )
    try:
        pending = service.request_external_access(str(external), "read", ttl_seconds=1)
        grant = service.approve_external_access(
            str(pending["request_id"]), str(pending["challenge"]), "批准"
        )
        grant_id = str(grant["grant_id"])

        def slow_operation():
            while True:
                event = service._cancel_event()
                if event is not None and event.wait(0.01):
                    raise JobCancelled("grant expired")

        response = service.run_with_fallback(
            "external_expiring", "<external-grant>", slow_operation,
            metadata={"grant_id": grant_id},
        )
        assert response["execution"] == "background"
        job_id = str(response["job_id"])
        assert service.jobs is not None
        assert service.jobs.get(job_id).done.wait(4)
        assert service.job_status(job_id)["state"] == "cancelled"
        with pytest.raises(PermissionError):
            service.external_stat(grant_id, ".")
    finally:
        service.shutdown()
