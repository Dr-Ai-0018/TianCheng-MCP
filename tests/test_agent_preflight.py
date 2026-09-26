from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

import pytest

from tiancheng_mcp.agent_preflight import inspect_windows_codex_home
from tiancheng_mcp.agent_adapters import AgentProfile
from tiancheng_mcp.agents import AgentProfileRegistry, load_agent_profile_definitions
import tiancheng_mcp.service as service_module
from tiancheng_mcp.service import TianChengService


def _home(path):
    (path / ".sandbox").mkdir(parents=True)
    (path / ".sandbox-secrets").mkdir()
    (path / ".sandbox" / "setup_marker.json").write_text('{"version": 999}', encoding="utf-8")
    (path / ".sandbox-secrets" / "sandbox_users.json").write_bytes(b"not-read-or-decrypted")
    return path


def test_intact_metadata_is_not_credential_or_version_verification(tmp_path, monkeypatch):
    home = _home(tmp_path / "home")
    original = Path.open
    def marker_only(path, *args, **kwargs):
        assert path.name == "setup_marker.json"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", marker_only)
    assert inspect_windows_codex_home(home) == "not_verified"


@pytest.mark.parametrize("payload,reason", [
    (b"", "marker_empty"), (b"no-json", "marker_invalid_json"),
    (b"[]", "marker_invalid_object"), (b"{}", "marker_invalid_object"),
    (b"\xff", "marker_invalid_json"), (b" " * 65537, "marker_too_large"),
    (b"[" * 2000, "marker_invalid_json"),
], ids=["empty", "syntax", "array", "empty-object", "encoding", "oversized", "deeply-nested"])
def test_bad_marker(tmp_path, payload, reason):
    home = _home(tmp_path / "home")
    (home / ".sandbox" / "setup_marker.json").write_bytes(payload)
    assert inspect_windows_codex_home(home) == reason


@pytest.mark.parametrize("target,reason", [
    (".sandbox/setup_marker.json", "marker_missing"),
    (".sandbox-secrets/sandbox_users.json", "credentials_missing"),
])
def test_missing_state(tmp_path, target, reason):
    home = _home(tmp_path / "home")
    (home / target).unlink()
    assert inspect_windows_codex_home(home) == reason


def test_new_home_requires_host_review(tmp_path):
    assert inspect_windows_codex_home(tmp_path) == "sandbox_directory_missing"


@pytest.mark.parametrize("error,reason", [
    (PermissionError, "marker_unreadable"),
    (FileNotFoundError, "marker_missing"), (OSError, "marker_read_failed"),
])
def test_open_failure_has_no_exception_text(tmp_path, monkeypatch, error, reason):
    home = _home(tmp_path / "home")
    def fail(*args, **kwargs):
        raise error("private-detail")
    monkeypatch.setattr(Path, "open", fail)
    assert inspect_windows_codex_home(home) == reason


@pytest.mark.parametrize("error,reason", [
    (PermissionError, "credentials_unreadable"),
    (OSError, "credentials_metadata_unavailable"),
])
def test_credential_metadata_error(tmp_path, monkeypatch, error, reason):
    home = _home(tmp_path / "home")
    original = Path.lstat
    def guarded(path, *args, **kwargs):
        if path.name == "sandbox_users.json":
            raise error("private-detail")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", guarded)
    assert inspect_windows_codex_home(home) == reason


@pytest.mark.parametrize("directory", [False, True])
def test_empty_or_wrong_type_credentials(tmp_path, directory):
    home = _home(tmp_path / "home")
    path = home / ".sandbox-secrets" / "sandbox_users.json"
    path.unlink()
    if directory:
        path.mkdir()
    else:
        path.touch()
    assert inspect_windows_codex_home(home) == ("credentials_wrong_type" if directory else "credentials_empty")


def test_reparse_metadata_rejected_without_following(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import stat
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(
        st_mode=stat.S_IFDIR, st_file_attributes=0x400,
    ))
    assert inspect_windows_codex_home(tmp_path) == "sandbox_directory_reparse_point"


@pytest.mark.skipif(os.name != "nt", reason="Windows launch contract")
@pytest.mark.parametrize("audit_available", [True, False])
def test_block_before_spawn_and_retry_rechecks_home(independent_workspace, tmp_path, monkeypatch, audit_available):
    workspace = independent_workspace
    home = _home(workspace / "home")
    marker = home / ".sandbox" / "setup_marker.json"
    marker.write_bytes(b"broken")
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True, enable_agent_catalog=False)
    service._exec_commands["codex"] = [sys.executable]
    service.agent_profiles = AgentProfileRegistry(["codex"], profile_definitions=[{
        "name": "independent", "provider": "codex", "provider_profile": "independent",
        "codex_home": str(home),
        "windows_home_preflight": "require-existing",
    }])
    spawned = []
    def fake_spawn(*args, **kwargs):
        spawned.append(True)
        raise OSError("fake-provider-stop")
    monkeypatch.setattr("tiancheng_mcp.service.subprocess.Popen", fake_spawn)
    if not audit_available:
        def fail_audit(**kwargs):
            raise OSError("private-audit-failure")
        monkeypatch.setattr(service.audit, "record", fail_audit)
    try:
        session = service.agent_session_create(profile="independent")
        with pytest.raises(PermissionError, match="marker_invalid_json"):
            service.agent_run_start(session["session_id"], "private-prompt")
        assert not spawned
        assert not service._get_agent_session(session["session_id"]).runs
        if audit_available:
            rows = [json.loads(line) for line in service.audit.path.read_text(encoding="utf-8").splitlines()]
            assert len(rows) == 1
            assert rows[0]["state"] == "preflight_blocked"
            assert rows[0]["success"] is False
            assert rows[0]["runtime_context"]["windows_home_preflight"] == "marker_invalid_json"
            assert rows[0]["runtime_context"]["windows_home_preflight_policy"] == "require-existing"
            assert "private-" not in json.dumps(rows)
        marker.write_text('{"version":999}', encoding="utf-8")
        with pytest.raises(OSError, match="fake-provider-stop"):
            service.agent_run_start(session["session_id"], "private-prompt")
        assert spawned == [True]
        if audit_available:
            rows = [json.loads(line) for line in service.audit.path.read_text(encoding="utf-8").splitlines()]
            assert rows[-1]["runtime_context"]["windows_home_preflight"] == "not_verified"
    finally:
        service.shutdown()


@pytest.fixture
def independent_workspace():
    # Production forbids agent homes beneath the server source tree, including
    # a repository-local pytest basetemp. Exercise that rule without mocking it.
    with tempfile.TemporaryDirectory(prefix="tc-preflight-") as directory:
        yield Path(directory).resolve(strict=True)


def test_default_home_does_not_run_independent_home_check(workspace, tmp_path, monkeypatch):
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True, enable_agent_catalog=False)
    service._exec_commands["codex"] = [sys.executable]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    def unexpected_check(*args):
        raise AssertionError("must not inspect default home")
    def fake_spawn(*args, **kwargs):
        raise OSError("fake-provider-stop")
    monkeypatch.setattr("tiancheng_mcp.service.inspect_windows_codex_home", unexpected_check)
    monkeypatch.setattr("tiancheng_mcp.service.subprocess.Popen", fake_spawn)
    try:
        session = service.agent_session_create()
        with pytest.raises(OSError, match="fake-provider-stop"):
            service.agent_run_start(session["session_id"], "private-prompt")
    finally:
        service.shutdown()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("choice,expected", [({}, "none"), ({"windows_home_preflight": "none"}, "none"),
    ({"windows_home_preflight": "require-existing"}, "require-existing")])
def test_profile_file_selects_preflight(tmp_path, version, choice, expected):
    definition = {"name": "independent", "provider": "codex", "provider_profile": "independent",
                  "codex_home": str(tmp_path), **choice}
    payload = {"version": version, "profiles": [definition]}
    if version == 2:
        payload["inherit_defaults"] = True
        definition["auth"] = {"mode": "existing-login"}
    config = tmp_path / "profiles.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_agent_profile_definitions(config)
    assert loaded.profiles[0]["windows_home_preflight"] == expected
    registry = AgentProfileRegistry(["codex"], profile_definitions=loaded.profiles)
    assert registry.get("independent").windows_home_preflight == expected
    assert registry.profile_summaries()[-1]["windows_home_preflight"] == expected


_INVALID_POLICIES = [None, True, 1, [], {}, "", "auto", "REQUIRE-EXISTING"]


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("policy", _INVALID_POLICIES)
def test_profile_file_rejects_invalid_policy_even_when_disabled(tmp_path, version, policy):
    definition = {"name": "independent", "provider": "codex", "provider_profile": "independent",
                  "codex_home": str(tmp_path), "windows_home_preflight": policy}
    payload = {"version": version, "profiles": [definition]}
    if version == 2:
        payload["inherit_defaults"] = True
        definition.update(auth={"mode": "existing-login"}, enabled=False)
    config = tmp_path / "profiles.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="windows_home_preflight"):
        load_agent_profile_definitions(config)


@pytest.mark.parametrize("policy", _INVALID_POLICIES)
@pytest.mark.parametrize("entry", ["dataclass", "registry", "disabled", "unavailable"])
def test_direct_profiles_cannot_bypass_policy_validation(tmp_path, policy, entry):
    definition = {"name": "independent", "provider": "codex", "provider_profile": "independent",
                  "codex_home": str(tmp_path), "windows_home_preflight": policy}
    with pytest.raises(ValueError, match="windows_home_preflight"):
        if entry == "dataclass":
            AgentProfile(command="codex", **definition)
        else:
            if entry == "disabled":
                definition["enabled"] = False
            AgentProfileRegistry([] if entry == "unavailable" else ["codex"], profile_definitions=[definition])


@pytest.mark.parametrize("provider,home", [("claude-code", "/home"), ("codex", None), ("codex", " ")])
def test_required_preflight_needs_codex_and_explicit_home(tmp_path, provider, home):
    definition = {"name": "independent", "provider": provider, "provider_profile": "independent",
                  "codex_home": home, "windows_home_preflight": "require-existing"}
    with pytest.raises(ValueError, match="requires provider=codex and explicit codex_home"):
        AgentProfile(command="codex", **definition)
    with pytest.raises(ValueError, match="requires provider=codex and explicit codex_home"):
        AgentProfileRegistry(["codex", "claude"], profile_definitions=[definition])
    config = tmp_path / "profiles.json"
    config.write_text(json.dumps({"version": 1, "profiles": [definition]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_agent_profile_definitions(config)


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("policy", ["none", "require-existing"])
def test_fresh_home_checks_only_selected_windows_policy(independent_workspace, tmp_path, monkeypatch, platform, policy):
    home = independent_workspace / "home"
    home.mkdir()
    service = TianChengService(independent_workspace, tmp_path / "audit", allow_exec=True, enable_agent_catalog=False)
    service._exec_commands["codex"] = [sys.executable]
    service.agent_profiles = AgentProfileRegistry(["codex"], profile_definitions=[{
        "name": "independent", "provider": "codex", "provider_profile": "independent",
        "codex_home": str(home), "windows_home_preflight": policy,
    }])
    # Change only this module's platform selector, not os.name used by pathlib.
    monkeypatch.setattr(service_module, "os", SimpleNamespace(**{**vars(os), "name": platform}))
    checks, spawns = [], []
    def check(path):
        checks.append(path)
        return inspect_windows_codex_home(path)
    def fake_spawn(*args, **kwargs):
        spawns.append(True)
        raise OSError("fake-provider-stop")
    monkeypatch.setattr(service_module, "inspect_windows_codex_home", check)
    monkeypatch.setattr(service_module.subprocess, "Popen", fake_spawn)
    blocked = platform == "nt" and policy == "require-existing"
    try:
        session = service.agent_session_create(profile="independent")
        with pytest.raises(PermissionError if blocked else OSError,
                           match="sandbox_directory_missing" if blocked else "fake-provider-stop"):
            service.agent_run_start(session["session_id"], "private-prompt",
                                   codex_options={"config": ['windows_home_preflight="none"']})
        assert checks == ([home] if blocked else [])
        assert spawns == ([] if blocked else [True])
        rows = [json.loads(line) for line in service.audit.path.read_text(encoding="utf-8").splitlines()]
        context = rows[-1]["runtime_context"]
        assert context["windows_home_preflight_policy"] == policy
        assert context["windows_home_preflight"] == (
            "sandbox_directory_missing" if blocked else "not_requested" if policy == "none" else "not_applicable"
        )
        assert not service._get_agent_session(session["session_id"]).runs
    finally:
        service.shutdown()


@pytest.mark.parametrize("action", ["create", "start"])
def test_remote_options_cannot_select_home_policy(workspace, tmp_path, monkeypatch, action):
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True, enable_agent_catalog=False)
    service._exec_commands["codex"] = [sys.executable]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    def unexpected_spawn(*args, **kwargs):
        raise AssertionError("Provider must not start")
    monkeypatch.setattr(service_module.subprocess, "Popen", unexpected_spawn)
    try:
        with pytest.raises(ValueError, match="Unknown Codex option fields: windows_home_preflight"):
            if action == "create":
                service.agent_session_create(codex_defaults={"windows_home_preflight": "none"})
            else:
                session = service.agent_session_create()
                service.agent_run_start(session["session_id"], "private-prompt",
                                        codex_options={"windows_home_preflight": "none"})
    finally:
        service.shutdown()
