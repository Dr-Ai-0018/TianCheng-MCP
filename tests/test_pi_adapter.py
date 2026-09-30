"""Pi's opt-in, tool-free Agent runtime contract."""

from __future__ import annotations

import json
import sys
import time

import pytest

from tiancheng_mcp.agents import AgentProfileRegistry, load_agent_profile_definitions
from tiancheng_mcp.pi_adapter import PiJsonlParser
from tiancheng_mcp.agent_sources import AgentSourcePolicy
from tiancheng_mcp.policy import AccessPolicy
from tiancheng_mcp.service import TianChengService


def _profile_file(tmp_path, *, model="example-model"):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({
        "version": 3,
        "inherit_defaults": True,
        "profiles": [{
            "name": "pi-qwen",
            "provider": "pi",
            "pi": {"provider": "example-provider", "model": model},
            "auth": {"mode": "env", "credential_env": "EXAMPLE_PI_KEY"},
        }],
    }), encoding="utf-8")
    return path


def test_pi_profile_is_server_owned_and_tool_free(tmp_path):
    definitions = load_agent_profile_definitions(_profile_file(tmp_path)).profiles
    registry = AgentProfileRegistry({"pi": ["node", "C:/pi/cli.js"]},
                                    profile_definitions=definitions)
    profile = registry.get("pi-qwen")
    command = registry.build_command(
        profile, ["node", "C:/pi/cli.js"], prompt="say hello",
        cwd="C:/workspace", sandbox="read-only",
    )
    assert command[:6] == [
        "node", "C:/pi/cli.js", "--provider", "example-provider",
        "--model", "example-model",
    ]
    assert "--no-tools" in command
    assert "--no-context-files" in command
    assert "--no-extensions" in command
    assert "--no-session" in command
    assert "EXAMPLE_PI_KEY" not in command
    assert registry.providers()[0]["capabilities"]["resume"] is False
    with pytest.raises(ValueError, match="sandbox"):
        registry.build_command(profile, ["node", "C:/pi/cli.js"],
                               prompt="x", cwd="C:/workspace",
                               sandbox="workspace-write")
    with pytest.raises(NotImplementedError, match="resume"):
        registry.build_command(profile, ["node", "C:/pi/cli.js"],
                               prompt="x", cwd="C:/workspace",
                               sandbox="read-only", native_session_id="other")


@pytest.mark.parametrize("model", ["", "--api-key", "model\n--tools", "a" * 300])
def test_pi_profile_rejects_invalid_model(tmp_path, model):
    with pytest.raises(ValueError, match="Pi provider or model"):
        load_agent_profile_definitions(_profile_file(tmp_path, model=model))


def test_pi_parser_retains_only_final_text_and_rejects_tool_events():
    parser = PiJsonlParser()
    assert parser.feed_line("not JSON") is None
    assert parser.feed_line(json.dumps({"type": "session", "id": "private-id"})).type == "thread_started"
    assert parser.native_session_id is None
    assert parser.feed_line(json.dumps({"type": "message_update", "secret": "ignore"})) is None
    final = parser.feed_line(json.dumps({
        "type": "message_end",
        "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "private"},
            {"type": "text", "text": "done"},
        ]},
    }))
    assert final.type == "agent_message"
    assert final.data == {"text": "done"}
    assert "private" not in str(final.as_dict())
    assert parser.feed_line('{"type":"agent_end","messages":[]}').type == "completed"
    assert parser.done is True
    assert parser.final_message == "done"
    violation = parser.feed_line('{"type":"tool_execution_start","toolName":"bash"}')
    assert violation.type == "error"
    assert parser.policy_violation is True


def test_pi_parser_rejects_embedded_tool_call_and_oversized_line():
    parser = PiJsonlParser()
    event = parser.feed_line(json.dumps({
        "type": "message_end", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "fake success"},
            {"type": "toolCall", "name": "bash", "arguments": {}},
        ]},
    }))
    assert event.type == "error"
    assert parser.final_message is None
    assert parser.policy_violation
    oversized = PiJsonlParser()
    assert oversized.feed_line("x" * (256 * 1024 + 1)).type == "error"
    assert oversized.policy_violation


def test_pi_parser_reports_sparse_progress_without_leaking_deltas():
    parser = PiJsonlParser()
    update = json.dumps({
        "type": "message_update",
        "assistantMessageEvent": {"type": "thinking_delta", "delta": "private canary"},
    })
    assert all(parser.feed_line(update) is None for _ in range(31))
    progress = parser.feed_line(update)
    assert progress.type == "status"
    assert "private canary" not in str(progress.as_dict())
    assert parser.update_count == 32


def test_pi_fake_cli_runs_through_managed_service(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entry = tmp_path / "cli.js"
    entry.write_text(
        "import json,os,sys\n"
        "assert '--no-tools' in sys.argv and '--no-context-files' in sys.argv\n"
        "assert os.environ.get('EXAMPLE_PI_KEY') == 'fixture-key'\n"
        "print(json.dumps({'type':'session','id':'fixture-id'}),flush=True)\n"
        "print(json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'PI_OK'}]}}),flush=True)\n"
        "print(json.dumps({'type':'agent_end','messages':[]}),flush=True)\n",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("EXAMPLE_PI_KEY=fixture-key\nOTHER_KEY=never-copy\n", encoding="utf-8")
    monkeypatch.setattr(TianChengService, "_discover_exec_commands",
                        lambda self: {"node": [sys.executable]})
    service = TianChengService(
        workspace, tmp_path / "audit", allow_exec=True,
        access_policy=AccessPolicy.default(workspace),
        agent_source_policy=AgentSourcePolicy.empty(),
        enable_agent_catalog=False,
        agent_profile_config_path=_profile_file(tmp_path),
        agent_env_file=env_file, pi_cli_entry=entry,
    )
    try:
        created = service.agent_session_create(profile="pi-qwen", sandbox="read-only")
        started = service.agent_run_start(created["session_id"], "reply")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            inspected = service.agent_run_inspect(created["session_id"], started["run_id"])
            if inspected["state"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert inspected["state"] == "succeeded"
        result = service.agent_run_result(created["session_id"], started["run_id"])
        assert result["result"] == "PI_OK"
        assert "OTHER_KEY" not in str(result)
        with pytest.raises(NotImplementedError, match="continuation"):
            service.agent_run_start(created["session_id"], "again")
        entry.write_text("print('replaced')\n", encoding="utf-8")
        second = service.agent_session_create(profile="pi-qwen", sandbox="read-only")
        with pytest.raises(RuntimeError, match="changed since service startup"):
            service.agent_run_start(second["session_id"], "reply")
    finally:
        service.shutdown()


def test_pi_cancel_and_hard_timeout_stop_managed_process(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entry = tmp_path / "cli.js"
    entry.write_text(
        "import json,time\n"
        "print(json.dumps({'type':'session','id':'fixture-id'}),flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("EXAMPLE_PI_KEY=fixture-key\n", encoding="utf-8")
    monkeypatch.setattr(TianChengService, "_discover_exec_commands",
                        lambda self: {"node": [sys.executable]})
    service = TianChengService(
        workspace, tmp_path / "audit", allow_exec=True,
        access_policy=AccessPolicy.default(workspace),
        agent_source_policy=AgentSourcePolicy.empty(),
        enable_agent_catalog=False,
        agent_profile_config_path=_profile_file(tmp_path),
        agent_env_file=env_file, pi_cli_entry=entry,
    )
    try:
        session = service.agent_session_create(profile="pi-qwen", sandbox="read-only")
        run = service.agent_run_start(session["session_id"], "wait")
        cancelled = service.agent_run_cancel(session["session_id"], run["run_id"])
        assert cancelled["state"] == "cancelled"
        _, record = service._get_agent_run(session["session_id"], run["run_id"])
        assert service._process_status(record.process_id)["running"] is False

        session = service.agent_session_create(profile="pi-qwen", sandbox="read-only")
        run = service.agent_run_start(
            session["session_id"], "wait", max_runtime_seconds=1,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            inspected = service.agent_run_inspect(session["session_id"], run["run_id"])
            if inspected["state"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert inspected["state"] == "timed_out"
        _, record = service._get_agent_run(session["session_id"], run["run_id"])
        assert service._process_status(record.process_id)["running"] is False
    finally:
        service.shutdown()
