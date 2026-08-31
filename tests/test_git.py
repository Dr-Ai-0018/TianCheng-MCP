from __future__ import annotations

import shutil

import pytest

from tiancheng_mcp.service import TianChengService, _redact_git_text


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")


def test_git_output_redacts_embedded_credentials_and_tokens() -> None:
    raw = "fatal: https://alice:s3cr3t@example.invalid/repo (gho_abcdef123456)"
    redacted = _redact_git_text(raw)
    assert "s3cr3t" not in redacted
    assert "gho_abcdef123456" not in redacted
    assert "<redacted>" in redacted


def test_local_git_tools_end_to_end(service: TianChengService) -> None:
    initialized = service.git_init("repo")
    assert initialized["initialized"] is True
    assert initialized["repository"] == "repo"

    service.write_text("repo/hello.txt", "hello\n")
    status = service.git_status("repo")
    assert "hello.txt" in status["status"]

    service.git_add(["hello.txt"], repo="repo")
    staged = service.git_diff("repo", staged=True)
    assert "+hello" in staged["diff"]

    committed = service.git_commit("Initial local commit", repo="repo")
    assert len(committed["commit"]) == 40
    log = service.git_log("repo", limit=5)
    assert log["commits"][0]["subject"] == "Initial local commit"
    assert log["commits"][0]["author"]
    assert committed["identity_source"] in {"git-config", "fallback"}

    service.write_text("repo/hello.txt", "changed\n")
    unstaged = service.git_diff("repo", path="hello.txt")
    assert "-hello" in unstaged["diff"]
    assert "+changed" in unstaged["diff"]


def test_git_rejects_paths_outside_repository(service: TianChengService) -> None:
    service.git_init("repo")
    service.write_text("outside.txt", "no")
    with pytest.raises(ValueError, match="Parent path|inside the repository"):
        service.git_add(["../outside.txt"], repo="repo")


def test_git_rejects_unsafe_local_config(service: TianChengService) -> None:
    service.git_init("repo")
    config = service.jail.root / "repo/.git/config"
    with config.open("a", encoding="utf-8") as stream:
        stream.write('\n[include]\n\tpath = C:/outside/config\n')
    with pytest.raises(ValueError, match="unsafe Git section"):
        service.git_status("repo")


def test_git_rejects_unsafe_setting_without_spaces(service: TianChengService) -> None:
    service.git_init("repo")
    config = service.jail.root / "repo/.git/config"
    with config.open("a", encoding="utf-8") as stream:
        stream.write("\n[core]\n\tworktree=C:/outside\n")
    with pytest.raises(ValueError, match="unsafe Git setting"):
        service.git_status("repo")


def test_remote_git_end_to_end_with_workspace_local_bare_remote(
    workspace, tmp_path
) -> None:
    service = TianChengService(workspace, tmp_path / "exec-audit", allow_exec=True)
    service.git_init("repo")
    service.write_text("repo/hello.txt", "one\n")
    service.git_add(["hello.txt"], repo="repo")
    service.git_commit("first", repo="repo")

    bare = service.run_command("git", ["init", "--bare", "remote.git"], cwd="repo")
    assert bare["exit_code"] == 0
    added = service.git_remote_add("origin", "remote.git", repo="repo")
    assert added["remote"] == "origin"
    assert service.git_remote_list("repo")["remotes"][0]["name"] == "origin"

    pushed = service.git_push(
        repo="repo", remote="origin", branch="main", set_upstream=True
    )
    assert pushed["exit_code"] == 0
    cloned = service.git_clone("repo/remote.git", "clone")
    assert cloned["repository"] == "clone"

    service.write_text("clone/hello.txt", "two\n")
    service.git_add(["hello.txt"], repo="clone")
    service.git_commit("second", repo="clone")
    service.git_push(repo="clone", remote="origin", branch="main")

    fetched = service.git_fetch(repo="repo", remote="origin")
    assert fetched["exit_code"] == 0
    pulled = service.git_pull(repo="repo", remote="origin", branch="main")
    assert pulled["exit_code"] == 0
    assert service.read_text("repo/hello.txt")["content"].splitlines() == ["two"]


def test_git_commit_uses_configured_identity(workspace, tmp_path) -> None:
    service = TianChengService(workspace, tmp_path / "exec-audit", allow_exec=True)
    service.git_init("repo")
    service.write_text("repo/file.txt", "content\n")
    service.git_add(["file.txt"], repo="repo")
    configured = service.run_command(
        "git", ["config", "user.name", "Workspace User"], cwd="repo"
    )
    assert configured["exit_code"] == 0
    configured = service.run_command(
        "git", ["config", "user.email", "workspace@example.invalid"], cwd="repo"
    )
    assert configured["exit_code"] == 0
    committed = service.git_commit("configured identity", repo="repo")
    assert committed["identity_source"] == "git-config"
    author = service.run_command(
        "git", ["log", "-1", "--format=%an <%ae>"], cwd="repo"
    )
    assert author["stdout"].strip() == "Workspace User <workspace@example.invalid>"


def test_remote_url_rejects_embedded_credentials(workspace, tmp_path) -> None:
    service = TianChengService(workspace, tmp_path / "exec-audit", allow_exec=True)
    service.git_init("repo")
    with pytest.raises(ValueError, match="embed credentials"):
        service.git_remote_add(
            "origin", "https://secret@github.com/example/repo.git", repo="repo"
        )
