"""Real boundary regressions: Git side effects and delegated descendants."""
from __future__ import annotations

import concurrent.futures
import shutil
import threading
from pathlib import Path

import pytest

from tiancheng_mcp.grants import ExternalGrantManager, MAX_ACTIVE_GRANTS, MAX_PENDING_GRANTS
from tiancheng_mcp.policy import AccessPolicy, AccessRule
from tiancheng_mcp.service import TianChengService


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
@pytest.mark.parametrize("config_text", [
    '[core] fsmonitor = "{command}"',
    '[CoRe] FsMoNiToR = "{command}"',
    '[core]\nfsmonitor = "{command}"',
    '[core]\nfsmonitor = false\nfsmonitor = "{command}"',
])
def test_safe_git_rejects_real_fsmonitor_side_effect(tmp_path: Path, config_text: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with_service = TianChengService(workspace, None, access_policy=AccessPolicy.default(workspace))
    try:
        with_service.git_init("repo")
        marker = workspace / "executed.txt"
        script = workspace / "monitor.sh"
        script.write_text(f'#!/bin/sh\nprintf SYNTHETIC_EXECUTED > "{marker.as_posix()}"\n', encoding="utf-8")
        script.chmod(0o755)
        config = workspace / "repo/.git/config"
        with config.open("a", encoding="utf-8") as stream:
            stream.write("\n" + config_text.format(command=script.as_posix()) + "\n")
        with pytest.raises(ValueError, match="unsafe Git setting"):
            with_service.git_status("repo")
        assert not marker.exists()
    finally:
        with_service.shutdown()


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
@pytest.mark.parametrize("section", ["include", 'includeIf "gitdir:./"', 'filter "test"', 'credential "https://example.invalid"', 'diff "test"'])
def test_git_semantic_sections_do_not_follow_includes(tmp_path: Path, section: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = TianChengService(workspace, None, access_policy=AccessPolicy.default(workspace))
    try:
        service.git_init("repo")
        with (workspace / "repo/.git/config").open("a", encoding="utf-8") as stream:
            stream.write(f'\n[{section}] path = "missing-file"\n')
        with pytest.raises(ValueError, match="unsafe Git section"):
            service.git_status("repo")
    finally:
        service.shutdown()


@pytest.fixture
def delegated(tmp_path: Path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    (external / "tree/blocked").mkdir(parents=True)
    (external / "tree/public.txt").write_text("PUBLIC_MARKER", encoding="utf-8")
    (external / "tree/blocked/private.txt").write_text("DENIED_MARKER", encoding="utf-8")
    workspace.mkdir()
    policy = AccessPolicy(workspace, [AccessRule(workspace, "full"), AccessRule(external, "full"), AccessRule(external / "tree/blocked", "deny")])
    service = TianChengService(workspace, None, access_policy=policy, allow_external_grants=True, enable_jobs=False, enable_agent_catalog=False)
    try:
        yield service, external
    finally:
        service.shutdown()


@pytest.mark.parametrize("grant", [False, True])
@pytest.mark.parametrize("engine", ["python", "rg"])
def test_delegated_search_never_returns_denied_descendants(delegated, grant: bool, engine: str) -> None:
    service, external = delegated
    if engine == "rg" and not service.rg_executable:
        pytest.skip("ripgrep is unavailable")
    # Scoped services rediscover the engine; patch discovery only for this test.
    from unittest.mock import patch
    discovery = shutil.which
    def which(name, *args, **kwargs):
        return None if engine == "python" and name == "rg" else discovery(name, *args, **kwargs)
    with patch("tiancheng_mcp.service.shutil.which", which):
        if grant:
            issued = service.request_external_access(str(external), "read")
            search = lambda query: service.external_search_text(issued["grant_id"], query)
        else:
            search = lambda query: service.policy_external_search_text(query, base_path=str(external))
        assert search("DENIED_MARKER")["results"] == []
        assert [item["path"] for item in search("PUBLIC_MARKER")["results"]] == ["tree/public.txt"]


@pytest.mark.parametrize("grant", [False, True])
def test_delegated_listing_and_glob_skip_denied_subtree(delegated, grant: bool) -> None:
    service, external = delegated
    if grant:
        issued = service.request_external_access(str(external), "read")
        listing = service.external_list_dir(issued["grant_id"], depth=3)
        glob = service.external_glob(issued["grant_id"], "**/*")
        with pytest.raises(PermissionError):
            service.external_read_text(issued["grant_id"], "tree/blocked/private.txt")
    else:
        listing = service.policy_external_list_dir(str(external), depth=3)
        glob = service.policy_external_glob("**/*", base_path=str(external))
    assert all("blocked" not in item["path"] for item in listing["entries"] + glob["results"])


@pytest.mark.parametrize("operation", ["copy", "move", "delete"])
@pytest.mark.parametrize("grant", [False, True])
def test_tree_mutation_preflights_denied_source_without_partial_effect(delegated, operation: str, grant: bool) -> None:
    service, external = delegated
    if grant:
        issued = service.request_external_access(str(external), "delete")
        method = getattr(service, "external_" + operation)
        args = [issued["grant_id"], "tree"] + ([] if operation == "delete" else ["result"])
    else:
        method = getattr(service, "policy_external_" + operation)
        args = [str(external / "tree")] + ([] if operation == "delete" else [str(external / "result")])
    with pytest.raises(PermissionError):
        method(*args)
    assert (external / "tree/public.txt").read_text() == "PUBLIC_MARKER"
    assert not (external / "result").exists()
    assert not (external / ".tiancheng-trash").exists()


def test_copy_preflights_projected_destination_and_preserves_readable_source(delegated) -> None:
    service, external = delegated
    source = external / "allowed"
    source.mkdir()
    (source / "child.txt").write_text("PUBLIC_MARKER")
    service.access_policy = service.access_policy.with_rules([AccessRule(external / "result/child.txt", "deny")])
    with pytest.raises(PermissionError):
        service.policy_external_copy(str(source), str(external / "result"))
    assert not (external / "result").exists()
    assert (source / "child.txt").read_text() == "PUBLIC_MARKER"


def test_scoped_operation_observes_policy_replacement(delegated) -> None:
    service, external = delegated
    scoped, relative = service._policy_scoped_service(str(external / "tree/public.txt"), "read")
    try:
        service.access_policy = service.access_policy.with_rules([AccessRule(external / "tree", "deny")])
        with pytest.raises(PermissionError):
            scoped.read_text(relative)
    finally:
        scoped.shutdown()


def manager(tmp_path: Path) -> tuple[ExternalGrantManager, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    return ExternalGrantManager(workspace, enabled=True, access_policy=AccessPolicy.default(workspace)), external


def approve(manager: ExternalGrantManager, request: dict) -> dict:
    return manager.approve(request["request_id"], request["challenge"], "批准")


def test_concurrent_grant_approval_respects_active_limit(tmp_path: Path) -> None:
    grants, external = manager(tmp_path)
    requests = [grants.request(str(external)) for _ in range(MAX_ACTIVE_GRANTS + 1)]
    barrier = threading.Barrier(len(requests))
    def submit(request):
        barrier.wait(timeout=5)
        try:
            return approve(grants, request)
        except RuntimeError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(requests)) as pool:
        results = list(pool.map(submit, requests))
    assert sum(item is not None for item in results) == MAX_ACTIVE_GRANTS
    assert len(grants.status()["active"]) == MAX_ACTIVE_GRANTS


def test_pending_grants_are_bounded_and_cancellation_releases_capacity(tmp_path: Path) -> None:
    grants, external = manager(tmp_path)
    pending = [grants.request(str(external)) for _ in range(MAX_PENDING_GRANTS)]
    with pytest.raises(RuntimeError, match="pending"):
        grants.request(str(external))
    grants.cancel_request(pending[0]["request_id"])
    assert grants.request(str(external))["status"] == "pending"


@pytest.mark.parametrize("phase", ["approve", "resolve"])
def test_current_deny_cannot_be_overridden_by_pending_or_active_grant(tmp_path: Path, phase: str) -> None:
    grants, external = manager(tmp_path)
    request = grants.request(str(external))
    issued = approve(grants, request) if phase == "resolve" else None
    grants.access_policy = grants.access_policy.with_rules([AccessRule(external, "deny")])
    with pytest.raises(PermissionError):
        if issued:
            grants.resolve(issued["grant_id"])
        else:
            approve(grants, request)


def test_approval_revalidates_directory_still_exists(tmp_path: Path) -> None:
    grants, external = manager(tmp_path)
    request = grants.request(str(external))
    external.rmdir()
    with pytest.raises(FileNotFoundError):
        approve(grants, request)


def test_denied_active_grant_is_reported_for_background_cancellation(tmp_path: Path) -> None:
    grants, external = manager(tmp_path)
    issued = approve(grants, grants.request(str(external)))
    grants.access_policy = grants.access_policy.with_rules([AccessRule(external, "deny")])
    assert grants.expire() == [issued["grant_id"]]
    assert grants.expire() == []
    assert grants.status()["active"] == []


def test_reload_commits_policy_between_grant_approvals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    old_policy = AccessPolicy.default(workspace)
    service = TianChengService(workspace, None, access_policy=old_policy,
                              allow_external_grants=True, enable_jobs=False, enable_agent_catalog=False)
    grants = service.external_grants
    pending = grants.request(str(external))
    old_policy.with_rules([AccessRule(external, "deny")]).save_atomic(service.access_policy_path)
    checked = threading.Event()
    release = threading.Event()
    updating = threading.Event()
    check = grants._check_static_deny
    lock = grants._lock

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == "policy-reload":
                updating.set()
            return lock.__enter__()
        def __exit__(self, *args):
            return lock.__exit__(*args)

    def pause_after_check(path, operation):
        check(path, operation)
        if threading.current_thread().name == "grant-approve" and not checked.is_set():
            checked.set()
            assert release.wait(5)

    monkeypatch.setattr(grants, "_lock", ObservedLock())
    monkeypatch.setattr(grants, "_check_static_deny", pause_after_check)
    outcomes = {}
    errors = []
    def call(name, function):
        try:
            outcomes[name] = function()
        except BaseException as error:
            errors.append(error)
    approving = threading.Thread(name="grant-approve", target=call,
                                args=("grant", lambda: approve(grants, pending)))
    reloading = threading.Thread(name="policy-reload", target=call,
                                args=("reload", service.reload_access_policy))
    try:
        approving.start()
        assert checked.wait(5)
        reloading.start()
        assert updating.wait(5)
        # The reload is now waiting for the grant's commit. Publishing a new
        # owner policy here would expose two different authorization snapshots.
        assert service.access_policy is old_policy
    finally:
        release.set()
        approving.join(5)
        if reloading.ident is not None:
            reloading.join(5)
        service.shutdown()
    assert not approving.is_alive() and not reloading.is_alive()
    assert not errors
    with pytest.raises(PermissionError):
        grants.resolve(outcomes["grant"]["grant_id"])


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
def test_safe_global_identity_survives_without_running_global_filter(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = TianChengService(workspace, None, access_policy=AccessPolicy.default(workspace))
    marker = workspace / "filter-executed.txt"
    script = workspace / "filter.sh"
    script.write_text(f'#!/bin/sh\nprintf EXECUTED > "{marker.as_posix()}"\ncat\n', encoding="utf-8")
    script.chmod(0o755)
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        '[user]\nname = Synthetic User\nemail = synthetic@example.invalid\n'
        f'[filter "audit"]\nclean = "{script.as_posix()}"\n'
        f'[core]\nfsmonitor = "{script.as_posix()}"\n', encoding="utf-8",
    )
    environment = service._git_environment()
    environment.update(GIT_CONFIG_GLOBAL=str(global_config), GIT_CONFIG_NOSYSTEM="1")
    service._git_environment = lambda: dict(environment)
    try:
        service.git_init("repo")
        service.write_text("repo/.gitattributes", "*.txt filter=audit\n")
        service.write_text("repo/file.txt", "PUBLIC_MARKER\n")
        service.git_status("repo")
        service.git_add(["."], repo="repo")
        assert not marker.exists()
        committed = service.git_commit("synthetic safe commit", repo="repo")
        assert committed["identity_source"] == "git-config"
        assert service.git_log("repo")["commits"][0]["author"] == "Synthetic User"
    finally:
        service.shutdown()
