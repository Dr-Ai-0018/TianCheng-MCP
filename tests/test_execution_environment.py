from __future__ import annotations

import os
import time

import pytest

from tiancheng_mcp.execution_environment import (
    base_environment, WINDOWS_INSTALLATION_NAMES, EnvironmentPolicy, validate_names,
)
from tiancheng_mcp.service import TianChengService
from tiancheng_mcp.access_context import AccessContext
from tiancheng_mcp.policy import AccessPolicy


def test_location_hints_are_case_insensitive_on_windows_without_guessing():
    assert base_environment({"programw6432": "X:/Programs", "SECRET": "sentinel"}, windows=True) == {
        "ProgramW6432": "X:/Programs",
    }
    assert base_environment({}, windows=True) == {}
    assert base_environment({"home": "/wrong", "HOME": "/right"}, windows=False) == {"HOME": "/right"}


@pytest.mark.skipif(os.name != "nt", reason="Windows installation variables")
def test_basic_locations_reach_real_child_without_extra_allowlist(workspace, tmp_path, monkeypatch):
    for name in WINDOWS_INSTALLATION_NAMES:
        monkeypatch.setenv(name, "X:/synthetic-install")
    monkeypatch.setenv("UNRELATED_ENV_SECRET", "must-not-copy")
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    try:
        result = service.run_command("python", ["-c", "import os; assert os.environ['ProgramW6432']=='X:/synthetic-install'; assert os.environ['ProgramFiles(x86)']=='X:/synthetic-install'; assert 'UNRELATED_ENV_SECRET' not in os.environ"])
        assert result["exit_code"] == 0, result["stderr"]
        assert service._git_environment()["ProgramW6432"] == "X:/synthetic-install"
        agent = service._execution_environment(include_passthrough_env=False)
        assert agent["ProgramFiles"] == "X:/synthetic-install"
        assert "UNRELATED_ENV_SECRET" not in agent
    finally:
        service.shutdown()


def test_levels_and_forced_environment_have_distinct_contracts():
    parent = {"ProgramW6432": "X:/Programs", "BUSINESS_KEY": "synthetic", "EMPTY": "",
              "CONTROL_PLANE_API_KEY": "secret", "PROFILE_KEY": "profile-secret",
              "TEMP": "outside", "GIT_TERMINAL_PROMPT": "1", "PATH": "parent-bin"}
    forced = {"TEMP": "workspace-temp", "GIT_TERMINAL_PROMPT": "0", "PATH": "tool-bin"}
    minimal = EnvironmentPolicy.parse({"mode": "minimal"}).build(forced, source=parent)
    basic = EnvironmentPolicy().build(forced, source=parent)
    inherited = EnvironmentPolicy.parse({"mode": "inherit", "deny": ["BUSINESS_KEY"]}).build(
        forced, source=parent, credential_names=("PROFILE_KEY",))
    assert "ProgramW6432" not in minimal
    assert ("ProgramW6432" in basic) == (os.name == "nt")
    assert "BUSINESS_KEY" not in basic
    assert "BUSINESS_KEY" not in inherited and "PROFILE_KEY" not in inherited
    assert "CONTROL_PLANE_API_KEY" not in inherited
    assert inherited["EMPTY"] == ""
    assert inherited["TEMP"] == "workspace-temp" and inherited["GIT_TERMINAL_PROMPT"] == "0"
    assert inherited["PATH"].split(os.pathsep) == ["tool-bin", "parent-bin"]


def test_explicit_names_empty_values_and_deny_priority():
    policy = EnvironmentPolicy.parse({"allow": ["CUSTOM", "EMPTY"], "deny": ["CUSTOM"]})
    result = policy.build({}, source={"CUSTOM": "secret", "EMPTY": ""})
    assert result == {"EMPTY": ""}
    assert validate_names(["ProgramFiles(x86)", "programfiles(X86)"], windows=True) == ("ProgramFiles(x86)",)
    assert validate_names(["HOME", "home"], windows=False) == ("HOME", "home")


@pytest.mark.parametrize("document", [{"mode": "other"}, {"allow": "CUSTOM"}, {"unknown": True},
    {"allow": ["bad=name"]}, {"allow": ["PATH"]}, {"deny": ["TEMP"]},
    {"allow": ["openai_api_key"]}, {"allow": ["PYTHONUTF8"]}])
def test_invalid_policy_rejected(document):
    with pytest.raises((ValueError, PermissionError)):
        EnvironmentPolicy.parse(document)


def test_normal_and_agent_execution_do_not_share_inherit_policy(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("BUSINESS_ENV", "synthetic")
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "never-copy")
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True,
        execution_environment={"mode": "inherit"})
    try:
        result = service.run_command("python", ["-c", "import os; assert os.environ['BUSINESS_ENV']=='synthetic'; assert 'CONTROL_PLANE_API_KEY' not in os.environ"])
        assert result["exit_code"] == 0, result["stderr"]
        assert "BUSINESS_ENV" not in service._execution_environment(include_passthrough_env=False)
        assert "BUSINESS_ENV" not in service._git_environment()
    finally:
        service.shutdown()


@pytest.mark.parametrize("mode", ["basic", "inherit"])
def test_managed_and_delegated_commands_share_environment_and_filters(workspace, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("BUSINESS", "synthetic")
    monkeypatch.setenv("DROP", "synthetic-excluded")
    monkeypatch.setenv("OTHER_PROFILE_KEY", "synthetic-profile-secret")
    service = TianChengService(workspace, None, allow_exec=True,
        execution_environment={"mode": mode, "allow": ["BUSINESS", "DROP"], "deny": ["DROP"]})
    service._environment_credential_names = ("OTHER_PROFILE_KEY",)
    external = tmp_path / "external"
    external.mkdir()
    scoped = service._scoped_service(AccessContext(external, "exec", lambda: AccessPolicy.default(external),
        lambda *_: None), allow_exec=True)
    code = "import os; assert os.environ['BUSINESS']=='synthetic'; assert 'DROP' not in os.environ; assert 'OTHER_PROFILE_KEY' not in os.environ; assert os.environ['GIT_TERMINAL_PROMPT']=='0'; print('checked')"
    try:
        assert scoped.execution_environment.document() == service.execution_environment.document()
        direct = scoped.run_command("python", ["-c", code])
        assert direct["exit_code"] == 0, direct["stderr"]
        started = service.start_process("python", ["-c", code], max_runtime_seconds=10)
        deadline = time.monotonic() + 10
        while service.process_status(started["process_id"])["running"]:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert service.process_status(started["process_id"])["exit_code"] == 0
        assert "checked" in service.process_output(started["process_id"])["stdout"]
    finally:
        scoped.shutdown()
        service.shutdown()
