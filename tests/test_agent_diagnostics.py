from __future__ import annotations

import hashlib
import json
import sys
import time

import pytest

from tiancheng_mcp.agent_adapters import CodexJsonlParser
from tiancheng_mcp.agent_diagnostics import bounded_launch_context, launch_metadata
from tiancheng_mcp.agents import AgentProfileRegistry, AgentRunState, MAX_AGENT_EVENTS
from tiancheng_mcp.service import TianChengService


@pytest.mark.parametrize("code,status,expected", [
    (0, "completed", (0, "completed")),
    (5, "failed", (5, "failed")),
    (None, "completed", (None, "completed")),
    (False, {"secret": "not-a-status"}, (None, "unknown")),
])
def test_command_events_retain_only_outcome(code, status, expected):
    parser = CodexJsonlParser()
    event = parser.feed_line(json.dumps({
        "type": "item.completed", "item": {
            "type": "command_execution", "exit_code": code, "status": status,
            "command": "secret-command", "aggregated_output": "secret-output",
        },
    }))
    assert event.type == "command_completed"
    assert (event.data["exit_code"], event.data["status"]) == expected
    assert "secret" not in json.dumps(event.as_dict())


def test_launch_metadata_uses_exact_environment_and_never_stores_argv(tmp_path):
    launcher = tmp_path / "bin" / "codex.js"
    launcher.parent.mkdir()
    launcher.write_text("// launcher", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "@openai/codex", "version": "1.2.3"}), encoding="utf-8"
    )
    environment = {"CODEX_HOME": "relative-home", "USERPROFILE": "ignored", "KEY": "secret-env"}
    metadata = launch_metadata(
        [sys.executable, str(launcher), "secret-arg"], environment,
        cwd=tmp_path, command_key="codex", profile_home=None,
    )
    assert metadata["home"] == str(tmp_path / "relative-home")
    assert metadata["home_source"] == "parent_environment"
    assert metadata["launcher"]["sha256"] == hashlib.sha256(launcher.read_bytes()).hexdigest()
    assert metadata["launcher_package_version"] == "1.2.3"
    assert metadata["runtime_version"] is None
    assert "secret" not in json.dumps(metadata)
    metadata = launch_metadata(
        [sys.executable], environment, cwd=tmp_path, command_key="codex", profile_home="relative-home"
    )
    assert metadata["home_source"] == "profile"


def test_missing_metadata_is_nonfatal_and_default_home_is_only_a_candidate(tmp_path):
    metadata = launch_metadata(
        [str(tmp_path / "missing")], {"USERPROFILE": str(tmp_path)},
        cwd=tmp_path, command_key="codex", profile_home=None,
    )
    assert metadata["executable"]["sha256"] is None
    assert metadata["home_source"] == "user_default_candidate"
    assert metadata["home"] == str(tmp_path / ".codex")


def test_command_failure_survives_event_eviction_and_is_not_task_failure():
    run = AgentRunState("run", "session", "process")
    event = run.parser.feed_line(json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "exit_code": 5, "status": "failed",
    }}))
    TianChengService._append_agent_event(run, event)
    for _ in range(MAX_AGENT_EVENTS + 1):
        TianChengService._append_agent_event(run, run.parser.synthetic_event("status", "later"))
    run.state = "succeeded"
    outcomes = TianChengService._agent_outcomes(run, {"exit_code": 0})
    assert outcomes["process"] == {"state": "succeeded", "exit_code": 0}
    assert outcomes["commands"]["failed_events"] == 1
    assert outcomes["commands"]["status"] == "failures_observed"
    assert outcomes["task"] == "unverified"


def test_no_command_events_and_output_gaps_are_not_success():
    run = AgentRunState("run", "session", "process")
    assert TianChengService._agent_outcomes(run, {})["commands"]["status"] == "unknown"
    event = run.parser.feed_line(json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "exit_code": 0, "status": "completed",
    }}))
    TianChengService._append_agent_event(run, event)
    assert TianChengService._agent_outcomes(run, {})["commands"]["status"] == "no_failures_observed"
    run.output_gap_observed = True
    assert TianChengService._agent_outcomes(run, {})["commands"]["status"] == "unknown"


@pytest.mark.parametrize("audit_available", [True, False])
def test_real_process_exit_zero_preserves_inner_command_failure(workspace, tmp_path, monkeypatch, audit_available):
    script = tmp_path / "fake_codex.py"
    payload = {"type": "item.completed", "item": {
        "type": "command_execution", "exit_code": 5, "status": "failed",
        "command": "must-not-retain", "aggregated_output": "must-not-retain",
    }}
    script.write_text(f"print({json.dumps(payload)!r}, flush=True)\n", encoding="utf-8")
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True, enable_agent_catalog=False)
    service._exec_commands["codex"] = [sys.executable, str(script)]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    if not audit_available:
        def fail_audit(**kwargs):
            raise OSError("private-audit-failure")
        monkeypatch.setattr(service.audit, "record", fail_audit)
    try:
        session = service.agent_session_create(codex_defaults={"ask_for_approval": "never"})
        started = service.agent_run_start(session["session_id"], "private-prompt")
        assert started["runtime_context"]["requested_additional_write_roots"] == []
        assert started["runtime_context"]["resolved_sandbox_policy"] == "not_observed"
        assert started["runtime_context"]["requested_approval_policy"] == "never"
        assert started["runtime_context"]["resolved_approval_policy"] == "not_observed"
        assert started["runtime_context"]["requested_auto_review"] is False
        deadline = time.monotonic() + 5
        while True:
            result = service.agent_run_result(session["session_id"], started["run_id"])
            if result["result_ready"]:
                break
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert result["state"] == "succeeded"
        assert result["outcomes"]["commands"]["failed_events"] == 1
        assert result["outcomes"]["task"] == "unverified"
        assert "private-prompt" not in json.dumps(result)
        assert "must-not-retain" not in json.dumps(result)
        assert result["runtime_context"] == started["runtime_context"]
        if audit_available:
            rows = [json.loads(line) for line in service.audit.path.read_text(encoding="utf-8").splitlines()]
            launches = [row for row in rows if row["tool"] == "agent_launch"]
            assert [row["state"] for row in launches] == ["prepared", "spawned"]
            assert {row["runtime_context"]["launch_id"] for row in launches} == {started["runtime_context"]["launch_id"]}
            assert all(row["runtime_context"]["requested_approval_policy"] == "never" for row in launches)
            assert "private-prompt" not in json.dumps(rows)
            assert "must-not-retain" not in json.dumps(rows)
    finally:
        service.shutdown()


def test_spawn_failure_is_recorded_without_exception_text_or_prompt(workspace, tmp_path, monkeypatch):
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True, enable_agent_catalog=False)
    service._exec_commands["codex"] = [sys.executable]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    def fail_spawn(*args, **kwargs):
        raise PermissionError("private-error-with-credential")
    monkeypatch.setattr("tiancheng_mcp.service.subprocess.Popen", fail_spawn)
    try:
        session = service.agent_session_create(codex_defaults={"ask_for_approval": "never"})
        with pytest.raises(PermissionError):
            service.agent_run_start(session["session_id"], "private-user-prompt")
        rows = [json.loads(line) for line in service.audit.path.read_text(encoding="utf-8").splitlines()]
        assert [row["state"] for row in rows] == ["prepared", "spawn_failed"]
        assert rows[-1]["error_type"] == "PermissionError"
        assert rows[-1]["success"] is False
        assert rows[0]["runtime_context"]["launch_id"] == rows[1]["runtime_context"]["launch_id"]
        assert rows[-1]["runtime_context"]["requested_approval_policy"] == "never"
        assert "private-" not in json.dumps(rows)
    finally:
        service.shutdown()


def test_persisted_launch_context_is_allowlisted_and_bounded():
    context = bounded_launch_context({
        "cwd": "x" * 2000, "prompt": "private-prompt", "env": {"KEY": "private-key"},
        "argv": ["private-arg"], "requested_additional_write_roots": ["x" * 2000] * 40,
        "executable": {"path": "python", "args": ["private-arg"], "sha256": None},
        "profile": {"secret": "private-value"},
        "windows_home_preflight_policy": "require-existing",
    })
    assert "private" not in json.dumps(context)
    assert len(context["cwd"]) == 1024
    assert len(context["requested_additional_write_roots"]) == 32
    assert all(len(root) == 1024 for root in context["requested_additional_write_roots"])
    assert "profile" not in context
    assert context["windows_home_preflight_policy"] == "require-existing"


@pytest.mark.parametrize("action", ["create", "start"])
@pytest.mark.parametrize("options", [
    {"config": ['notify=["tiancheng-review-nonexistent-command"]']},
    {"config": ['notify=[]']},
    {"config": ['windows.sandbox="unelevated"']},
    {"disable": ["elevated_windows_sandbox"]},
    {"ignore_rules": True}, {"ignore_user_config": True},
])
def test_security_overrides_are_rejected_before_provider_spawn(workspace, tmp_path, monkeypatch, action, options):
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True, enable_agent_catalog=False)
    service._exec_commands["codex"] = [sys.executable]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    def unexpected_spawn(*args, **kwargs):
        raise AssertionError("provider must not start")
    monkeypatch.setattr("tiancheng_mcp.service.subprocess.Popen", unexpected_spawn)
    try:
        if action == "create":
            with pytest.raises(PermissionError, match="server-side policy"):
                service.agent_session_create(codex_defaults=options)
        else:
            session = service.agent_session_create()
            with pytest.raises(PermissionError, match="server-side policy"):
                service.agent_run_start(session["session_id"], "must-not-run", codex_options=options)
        assert not service.audit.path.exists()
    finally:
        service.shutdown()
