from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
from pathlib import Path

import pytest

import tiancheng_mcp
from tiancheng_mcp.policy import AccessPolicy, AccessPolicyError, AccessRule
from tiancheng_mcp.service import TianChengService


def test_default_policy_allows_workspace_and_denies_external(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = AccessPolicy.default(workspace)

    assert policy.authorize(workspace / "notes.txt", "write").mode == "full"
    with pytest.raises(PermissionError):
        policy.authorize(tmp_path / "outside.txt", "read")


def test_longest_rule_wins_and_deny_overrides_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    backup = tmp_path / "backup"
    secret = backup / "secret"
    secret.mkdir(parents=True)
    workspace.mkdir()
    policy = AccessPolicy(
        workspace,
        [
            AccessRule(workspace, "full"),
            AccessRule(backup, "read"),
            AccessRule(secret, "deny"),
        ],
    )

    assert policy.authorize(backup / "conversation.json", "read").mode == "read"
    with pytest.raises(PermissionError):
        policy.authorize(secret / "conversation.json", "read")


def test_operation_capabilities_are_separate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    tools = tmp_path / "tools"
    workspace.mkdir()
    tools.mkdir()
    policy = AccessPolicy(
        workspace,
        [AccessRule(workspace, "full"), AccessRule(tools, "write", allow_exec=False)],
    )

    assert policy.authorize(tools / "script.py", "write").allowed
    with pytest.raises(PermissionError):
        policy.authorize(tools / "script.py", "exec")

    exec_policy = AccessPolicy(
        workspace,
        [AccessRule(workspace, "full"), AccessRule(tools, "full", allow_exec=True)],
    )
    assert exec_policy.authorize(tools / "script.py", "exec").allow_exec is True


def test_missing_rule_directory_uses_existing_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    missing = external / "future-data"
    policy = AccessPolicy(workspace, [AccessRule(workspace, "full"), AccessRule(missing, "read")])

    decision = policy.explain(missing / "file.txt", "read")
    assert decision.allowed is True
    assert decision.rule_path == missing


def test_policy_loader_rejects_unknown_fields_and_conflicts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    config = tmp_path / "access-policy.json"

    config.write_text(
        json.dumps({"rules": [{"path": str(workspace), "mode": "full", "oops": True}]}),
        encoding="utf-8",
    )
    with pytest.raises(AccessPolicyError, match="unknown fields"):
        AccessPolicy.load(config, workspace)

    config.write_text(
        json.dumps(
            {
                "rules": [
                    {"path": str(workspace), "mode": "full"},
                    {"path": str(external), "mode": "read"},
                    {"path": str(external), "mode": "write"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AccessPolicyError, match="Conflicting"):
        AccessPolicy.load(config, workspace)


def test_service_rejects_policy_file_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="outside the workspace"):
        TianChengService(workspace, tmp_path / "audit", access_policy_path=workspace / "policy.json")


def test_service_reload_replaces_policy_only_after_valid_load(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    config = tmp_path / "access-policy.json"
    config.write_text(
        json.dumps({"rules": [{"path": str(workspace), "mode": "full"}]}),
        encoding="utf-8",
    )
    service = TianChengService(workspace, tmp_path / "audit", access_policy_path=config)
    try:
        assert service.access_policy_explain(str(external / "a.txt"), "read")["allowed"] is False
        config.write_text(
            json.dumps(
                {
                    "rules": [
                        {"path": str(workspace), "mode": "full"},
                        {"path": str(external), "mode": "read"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = service.reload_access_policy()
        assert result["reloaded"] is True
        assert service.access_policy_explain(str(external / "a.txt"), "read")["allowed"] is True
    finally:
        service.shutdown()


def test_policy_explain_reports_git_capabilities_and_approval(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    policy = AccessPolicy(
        workspace,
        [
            AccessRule(workspace, "full"),
            AccessRule(external, "write", require_approval=True),
        ],
    )
    decision = policy.explain(external / "repo", "git_write")
    assert decision.allowed is False
    assert decision.requires_approval is False
    assert decision.mode == "write"
    assert "does not allow" in decision.reason


def test_hot_reload_is_disabled_by_default(workspace, tmp_path) -> None:
    service = TianChengService(workspace, tmp_path / "audit")
    try:
        assert service.workspace_info()["access_policy_reload_mode"] == "cold"
        with pytest.raises(PermissionError):
            service.policy_change_request([str(tmp_path)], "read")
    finally:
        service.shutdown()


@contextlib.contextmanager
def _outside_directory(name: str = "new-project"):
    """A directory outside the repo.

    The suite runs with --basetemp inside the project, and whitelisting
    anything under the server's own root is refused by design, so a target
    for these tests has to live elsewhere.
    """

    base = Path(tempfile.mkdtemp(prefix="tc-policy-"))
    target = base / name
    target.mkdir()
    try:
        yield target
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _hot_service(workspace, tmp_path):
    policy_file = tmp_path / "policy" / "access-policy.json"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        json.dumps({"rules": [{"path": str(workspace), "mode": "full"}]}),
        encoding="utf-8",
    )
    return TianChengService(
        workspace,
        tmp_path / "audit",
        access_policy_path=policy_file,
        allow_policy_hot_reload=True,
    )


def test_approved_change_takes_effect_without_restart(workspace, tmp_path) -> None:
    service = _hot_service(workspace, tmp_path)
    try:
        with _outside_directory() as target:
            with pytest.raises(PermissionError):
                service.access_policy.authorize(target, "read")

            staged = service.policy_change_request([str(target)], "write")
            assert staged["status"] == "pending"
            # Staging alone must not grant anything.
            with pytest.raises(PermissionError):
                service.access_policy.authorize(target, "read")

            approved = service.policy_change_approve(
                staged["request_id"], staged["challenge"], "批准"
            )
            assert approved["effective_immediately"] is True
            # Same live service object: no restart, and the caller never had
            # to call reload itself.
            assert service.access_policy.authorize(target, "write").allowed is True
    finally:
        service.shutdown()


def test_approval_requires_the_matching_challenge_and_confirmation(
    workspace, tmp_path
) -> None:
    service = _hot_service(workspace, tmp_path)
    try:
        with _outside_directory() as target:
            staged = service.policy_change_request([str(target)], "read")
            with pytest.raises(PermissionError):
                service.policy_change_approve(
                    staged["request_id"], staged["challenge"], "ok"
                )
            with pytest.raises(PermissionError):
                service.policy_change_approve(
                    staged["request_id"], "WRON-GKEY", "批准"
                )
            with pytest.raises(PermissionError):
                service.access_policy.authorize(target, "read")
    finally:
        service.shutdown()


def test_hot_reload_refuses_to_whitelist_the_servers_own_directory(
    workspace, tmp_path
) -> None:
    service = _hot_service(workspace, tmp_path)
    project_root = Path(tiancheng_mcp.__file__).resolve().parents[2]
    try:
        for forbidden in (project_root, project_root / "config", tmp_path / "audit"):
            forbidden.mkdir(parents=True, exist_ok=True)
            with pytest.raises(PermissionError):
                service.policy_change_request([str(forbidden)], "full")
    finally:
        service.shutdown()


def test_hot_reload_refuses_roots_and_sensitive_names(workspace, tmp_path) -> None:
    service = _hot_service(workspace, tmp_path)
    try:
        with _outside_directory("secrets") as secrets_dir:
            with pytest.raises(PermissionError):
                service.policy_change_request([str(Path(tmp_path.anchor))], "read")
            with pytest.raises(PermissionError):
                service.policy_change_request([str(secrets_dir)], "read")
    finally:
        service.shutdown()


def test_deny_rule_cannot_be_overridden_by_a_chat_approval(workspace, tmp_path) -> None:
    # The blocked directory must sit outside the repo, otherwise the request
    # is refused by the "server's own directory" guard and this test would
    # pass without ever exercising the deny rule.
    with _outside_directory("blocked") as blocked:
        policy_file = tmp_path / "policy" / "access-policy.json"
        policy_file.parent.mkdir(parents=True, exist_ok=True)
        policy_file.write_text(
            json.dumps(
                {
                    "rules": [
                        {"path": str(workspace), "mode": "full"},
                        {"path": str(blocked), "mode": "deny"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        service = TianChengService(
            workspace,
            tmp_path / "audit",
            access_policy_path=policy_file,
            allow_policy_hot_reload=True,
        )
        try:
            with pytest.raises(PermissionError, match="deny rule"):
                service.policy_change_request([str(blocked)], "full")
        finally:
            service.shutdown()


def test_browse_walks_down_one_level_at_a_time(workspace, tmp_path) -> None:
    """browse is navigable, not a single fixed listing.

    The caller may descend into any subdirectory under the granted root and
    get that one level, which is how a user picks a folder to promote into
    the whitelist. It must never return a recursive tree or file content.
    """

    with _outside_directory("Projects") as projects:
        (projects / "Smile-Chat" / "src").mkdir(parents=True)
        (projects / "Smile-Chat" / "readme.md").write_text("body", encoding="utf-8")
        (projects / "Falcon-show").mkdir()
        policy_file = tmp_path / "policy" / "access-policy.json"
        policy_file.parent.mkdir(parents=True, exist_ok=True)
        policy_file.write_text(
            json.dumps(
                {
                    "rules": [
                        {"path": str(workspace), "mode": "full"},
                        {"path": str(projects), "mode": "browse"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        service = TianChengService(
            workspace, tmp_path / "audit", access_policy_path=policy_file
        )
        try:
            top = service.policy_external_list_dir(str(projects))
            assert top["browse_only"] is True
            assert {entry["path"] for entry in top["entries"]} == {
                "Falcon-show",
                "Smile-Chat",
            }

            nested = service.policy_external_list_dir(str(projects / "Smile-Chat"))
            assert {entry["path"] for entry in nested["entries"]} == {
                "Smile-Chat/readme.md",
                "Smile-Chat/src",
            }

            # An explicit deep request is clamped, so browse can never dump a
            # whole tree in one call.
            deep = service.policy_external_list_dir(
                str(projects / "Smile-Chat"), depth=5
            )
            assert {entry["path"] for entry in deep["entries"]} == {
                "Smile-Chat/readme.md",
                "Smile-Chat/src",
            }

            with pytest.raises(PermissionError):
                service.policy_external_read_text(
                    str(projects / "Smile-Chat" / "readme.md")
                )
            with pytest.raises(PermissionError):
                service.policy_external_write_text(
                    str(projects / "Smile-Chat" / "new.txt"), "x"
                )
        finally:
            service.shutdown()


def test_summary_lists_every_rule_so_callers_can_discover_them(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    tools = tmp_path / "tools"
    retired = tmp_path / "retired"
    for directory in (workspace, tools, retired):
        directory.mkdir()
    policy = AccessPolicy(
        workspace,
        [
            AccessRule(workspace, "full"),
            AccessRule(tools, "read", allow_exec=True, note="build scripts"),
            AccessRule(retired, "write", enabled=False),
        ],
    )

    summary = policy.summary()

    assert summary["rule_count"] == 3
    assert summary["enabled_rule_count"] == 2
    assert summary["rules"] == sorted(summary["rules"], key=lambda rule: rule["path"])
    listed = {rule["path"]: rule for rule in summary["rules"]}
    assert listed[str(tools)] == {
        "path": str(tools),
        "mode": "read",
        "allow_exec": True,
        "require_approval": False,
        "enabled": True,
        "note": "build scripts",
    }
    # Disabled rules stay visible: a caller that expected the directory to work
    # can see why it does not, instead of guessing the path was never granted.
    assert listed[str(retired)]["enabled"] is False


def test_workspace_info_reports_the_directories_a_caller_may_use(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    config = tmp_path / "access-policy.json"
    config.write_text(
        json.dumps(
            {
                "rules": [
                    {"path": str(workspace), "mode": "full"},
                    {"path": str(external), "mode": "read", "note": "agent cwd"},
                ]
            }
        ),
        encoding="utf-8",
    )
    service = TianChengService(workspace, tmp_path / "audit", access_policy_path=config)
    try:
        rules = service.workspace_info()["access_policy"]["rules"]
        assert {rule["path"] for rule in rules} == {str(workspace), str(external)}
        granted = next(rule for rule in rules if rule["path"] == str(external))
        assert granted["mode"] == "read"
        assert granted["note"] == "agent cwd"
    finally:
        service.shutdown()
