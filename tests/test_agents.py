from __future__ import annotations

from dataclasses import replace
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tiancheng_mcp.agents import (
    MAX_AGENT_EVENTS,
    AgentProfileRegistry,
    AgentRunState,
    load_agent_profile_definitions,
)
from tiancheng_mcp.agent_adapters import (
    AdapterCapabilities,
    AgentAdapter,
    AgentProfile,
    ClaudeCodeAdapter,
    ClaudeJsonlParser,
    CodexAdapter,
    CodexJsonlParser,
    NormalizedEvent,
    merge_codex_options,
    normalize_codex_options,
    redact_text,
)
from tiancheng_mcp.agent_sources import AgentSourcePolicy
from tiancheng_mcp.policy import AccessPolicy, AccessRule
from tiancheng_mcp.security import WorkspaceSecurityError
from tiancheng_mcp.service import TianChengService


TERMINAL_STATES = {
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
    "aborted_on_shutdown",
}


def _wait_for_run(
    service: TianChengService, session_id: str, run_id: str, timeout: float = 5
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    inspected: dict[str, object] = {}
    while time.monotonic() < deadline:
        inspected = service.agent_run_inspect(session_id, run_id)
        if inspected["state"] in TERMINAL_STATES:
            return inspected
        time.sleep(0.05)
    raise AssertionError(f"agent run did not finish: {inspected}")


def test_codex_profile_command_is_server_owned_and_bounded() -> None:
    registry = AgentProfileRegistry(["codex"])
    profile = registry.get("codex-default")
    command = registry.build_codex_command(
        profile,
        ["node", "C:/tools/codex.js"],
        prompt="检查项目",
        cwd="E:/ExampleWorkspace",
        sandbox="read-only",
    )
    assert command == [
        "node",
        "C:/tools/codex.js",
        "exec",
        "--json",
        "-s",
        "read-only",
        "-C",
        "E:/ExampleWorkspace",
        "检查项目",
    ]
    with pytest.raises(ValueError, match="sandbox"):
        registry.build_codex_command(
            profile,
            ["node", "codex.js"],
            prompt="x",
            cwd="E:/ExampleWorkspace",
            sandbox="danger-full-access",
        )
    with pytest.raises(ValueError, match="Unknown agent profile"):
        registry.get("arbitrary")


def test_configured_profiles_bind_only_their_own_dotenv_credential(
    monkeypatch, tmp_path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "IsolatedCodex"
    home.mkdir()
    config = tmp_path / "agent-profiles.json"
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "inherit_defaults": True,
                "profiles": [
                    {
                        "name": "codex-default",
                        "provider": "codex",
                        "provider_profile": "example-provider",
                        "auth": {
                            "mode": "env",
                            "credential_env": "EXAMPLE_AGENT_KEY",
                        },
                    },
                    {
                        "name": "isolated-agent",
                        "provider": "codex",
                        "provider_profile": "isolated-agent",
                        "codex_home": str(home),
                        "auth": {
                            "mode": "env",
                            "credential_env": "ISOLATED_AGENT_KEY",
                        },
                    },
                    {
                        "name": "claude-default",
                        "provider": "claude-code",
                        "provider_profile": "local-default",
                        "auth": {"mode": "existing-login"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CONTROL_PLANE_API_KEY=never-copy\n"
        "EXAMPLE_AGENT_KEY=pro-secret\n"
        "ISOLATED_AGENT_KEY=qing-secret\n",
        encoding="utf-8",
    )
    profile_config = load_agent_profile_definitions(config)
    assert profile_config.inherit_defaults is True
    assert [item["name"] for item in profile_config.profiles] == [
        "codex-default",
        "isolated-agent",
        "claude-default",
    ]
    monkeypatch.setattr(
        TianChengService, "_discover_exec_commands", lambda self: {"codex": [sys.executable]}
    )
    monkeypatch.setattr(
        TianChengService,
        "_discover_agent_only_commands",
        lambda self: {"claude": [sys.executable]},
    )
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_exec=True,
        access_policy=AccessPolicy.default(workspace),
        agent_source_policy=AgentSourcePolicy.empty(),
        enable_agent_catalog=False,
        agent_profile_config_path=config,
        agent_env_file=env_file,
    )
    try:
        isolated_agent = service.agent_profiles.get("isolated-agent")
        assert isolated_agent.codex_config_profile == "isolated-agent"
        qing_env = service._execution_environment(
            include_passthrough_env=False,
            profile_credential_env=isolated_agent.credential_env,
            codex_home=isolated_agent.codex_home,
        )
        assert qing_env["ISOLATED_AGENT_KEY"] == "qing-secret"
        assert "EXAMPLE_AGENT_KEY" not in qing_env
        assert "CONTROL_PLANE_API_KEY" not in qing_env
        example_profile = service.agent_profiles.get("codex-default")
        pro_env = service._execution_environment(
            include_passthrough_env=False,
            profile_credential_env=example_profile.credential_env,
        )
        assert pro_env["EXAMPLE_AGENT_KEY"] == "pro-secret"
        assert "ISOLATED_AGENT_KEY" not in pro_env
        claude = service.agent_profiles.get("claude-default")
        assert claude.auth_mode == "existing-login"
        assert claude.credential_env is None
    finally:
        service.shutdown()


def test_profile_overlay_retains_defaults_and_can_explicitly_disable_one() -> None:
    registry = AgentProfileRegistry(
        ["codex", "claude"],
        profile_definitions=(
            {
                "name": "isolated-agent",
                "provider": "codex",
                "provider_profile": "isolated-agent",
                "codex_home": "E:/ExampleWorkspace/AgentHomes/IsolatedCodex",
                "credential_env": "ISOLATED_AGENT_KEY",
                "auth_mode": "env",
                "enabled": True,
            },
        ),
    )
    assert registry.names() == ("claude-default", "codex-default", "isolated-agent")

    disabled = AgentProfileRegistry(
        ["codex", "claude"],
        profile_definitions=(
            {
                "name": "claude-default",
                "provider": "claude-code",
                "provider_profile": "local-default",
                "credential_env": None,
                "auth_mode": "existing-login",
                "enabled": False,
            },
        ),
    )
    assert disabled.names() == ("codex-default",)

    with pytest.raises(ValueError, match="cannot change provider"):
        AgentProfileRegistry(
            ["codex", "claude"],
            profile_definitions=(
                {
                    "name": "claude-default",
                    "provider": "codex",
                    "provider_profile": "example-provider",
                    "credential_env": "EXAMPLE_AGENT_KEY",
                    "auth_mode": "env",
                    "enabled": True,
                },
            ),
            inherit_default_profiles=False,
        )


def test_missing_profile_config_falls_back_to_available_builtin_profiles(
    monkeypatch, tmp_path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        TianChengService,
        "_discover_exec_commands",
        lambda self: {"codex": [sys.executable]},
    )
    monkeypatch.setattr(
        TianChengService,
        "_discover_agent_only_commands",
        lambda self: {"claude": [sys.executable]},
    )
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_exec=True,
        access_policy=AccessPolicy.default(workspace),
        agent_source_policy=AgentSourcePolicy.empty(),
        enable_agent_catalog=False,
        agent_profile_config_path=tmp_path / "missing-agent-profiles.json",
    )
    try:
        assert service.agent_profiles.names() == (
            "claude-default",
            "codex-default",
        )
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    "auth",
    (
        {"mode": "existing-login", "credential_env": "SHOULD_NOT_EXIST"},
        {"mode": "env"},
        {"mode": "unknown"},
    ),
)
def test_profile_config_rejects_ambiguous_or_unknown_auth(
    tmp_path, auth
) -> None:
    config = tmp_path / "agent-profiles.json"
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "inherit_defaults": True,
                "profiles": [
                    {
                        "name": "claude-default",
                        "provider": "claude-code",
                        "provider_profile": "local-default",
                        "auth": auth,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_agent_profile_definitions(config)


def test_codex_native_invocation_options_render_in_cli_0153_order() -> None:
    registry = AgentProfileRegistry(["codex"])
    profile = registry.get("codex-default")
    command = registry.build_codex_command(
        profile,
        ["codex"],
        prompt="检查参数",
        cwd="E:/ExampleWorkspace",
        sandbox="workspace-write",
        invocation_options={
            "ask_for_approval": "never",
            "search": True,
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "model_provider": "example-provider",
            "config": ["web_search=\"cached\"", "features.example=true"],
            "enable": ["alpha_one", "alpha_two"],
            "disable": ["legacy_one"],
            "strict_config": True,
            "images": ["E:/ExampleWorkspace/image.png"],
            "oss": True,
            "local_provider": "ollama",
            "approve_for_me": True,
            "add_dirs": ["E:/ExampleWorkspace/extra"],
            "thread_source": "tiancheng-mcp",
            "skip_git_repo_check": True,
            "ignore_user_config": True,
            "ignore_rules": True,
            "output_schema": "E:/ExampleWorkspace/schema.json",
            "color": "never",
            "output_last_message": "E:/ExampleWorkspace/result.txt",
        },
    )
    assert command[:5] == ["codex", "-a", "never", "--search", "exec"]
    assert command[5:11] == [
        "--json",
        "-s",
        "workspace-write",
        "-C",
        "E:/ExampleWorkspace",
        "-m",
    ]
    assert command[-1] == "检查参数"
    assert command.count("-c") == 4
    assert command[command.index("-m") + 1] == "gpt-5.6-luna"
    assert "model_reasoning_effort=\"max\"" in command
    assert "model_provider=\"example-provider\"" in command
    assert command[command.index("--local-provider") + 1] == "ollama"
    assert command[command.index("--thread-source") + 1] == "tiancheng-mcp"
    assert command[command.index("--output-schema") + 1].endswith("schema.json")

    leading_flag_prompt = registry.build_codex_command(
        profile,
        ["codex"],
        prompt="--help is prompt text",
        cwd="E:/ExampleWorkspace",
        sandbox="read-only",
    )
    assert leading_flag_prompt[-2:] == ["--", "--help is prompt text"]

    fork = registry.build_codex_command(
        profile,
        ["codex"],
        prompt="",
        cwd="E:/ExampleWorkspace",
        sandbox="read-only",
        native_session_id="thr_source",
        invocation_options={"model": "gpt-5.6-sol"},
        action="fork",
    )
    assert fork[-3:] == ["gpt-5.6-sol", "fork", "thr_source"]

    review = registry.build_codex_command(
        profile,
        ["codex"],
        prompt="只看并发问题",
        cwd="E:/ExampleWorkspace",
        sandbox="read-only",
        invocation_options={
            "review_base": "main",
            "review_title": "Native option review",
        },
        action="review",
    )
    assert review[-6:] == [
        "review",
        "--base",
        "main",
        "--title",
        "Native option review",
        "只看并发问题",
    ]


def test_codex_options_validate_merge_clear_and_high_risk_flags() -> None:
    defaults = normalize_codex_options(
        {
            "model": "gpt-5.6-sol",
            "search": True,
            "config": ["model_reasoning_effort=\"high\""],
        }
    )
    merged = merge_codex_options(
        defaults,
        {"model": "gpt-5.6-terra", "search": None},
    )
    assert merged == {
        "model": "gpt-5.6-terra",
        "config": ("model_reasoning_effort=\"high\"",),
    }
    with pytest.raises(ValueError, match="Unknown Codex option"):
        normalize_codex_options({"raw_args": ["--yolo"]})
    with pytest.raises(ValueError, match="dotted key=value"):
        normalize_codex_options({"config": ["not-a-pair"]})
    with pytest.raises(PermissionError, match="server-side policy"):
        normalize_codex_options({"config": ["sandbox_mode=\"danger-full-access\""]})
    with pytest.raises(PermissionError, match="credentials"):
        normalize_codex_options({"config": ["telemetry.api_key=\"secret\""]})
    with pytest.raises(PermissionError, match="server-owned"):
        normalize_codex_options({"thread_source": "spoofed-source"})

    registry = AgentProfileRegistry(["codex"])
    profile = registry.get("codex-default")
    with pytest.raises(PermissionError, match="externally isolated"):
        registry.build_codex_command(
            profile,
            ["codex"],
            prompt="x",
            cwd="E:/ExampleWorkspace",
            sandbox="workspace-write",
            invocation_options={
                "dangerously_bypass_approvals_and_sandbox": True
            },
        )
    with pytest.raises(ValueError, match="JSONL parser"):
        registry.build_codex_command(
            profile,
            ["codex"],
            prompt="x",
            cwd="E:/ExampleWorkspace",
            sandbox="read-only",
            invocation_options={"color": "always"},
        )
    with pytest.raises(ValueError, match="both enabled and disabled"):
        registry.build_codex_command(
            profile,
            ["codex"],
            prompt="x",
            cwd="E:/ExampleWorkspace",
            sandbox="read-only",
            invocation_options={"enable": ["x"], "disable": ["x"]},
        )
    with pytest.raises(ValueError, match="mutually exclusive"):
        registry.build_codex_command(
            profile,
            ["codex"],
            prompt="",
            cwd="E:/ExampleWorkspace",
            sandbox="read-only",
            invocation_options={
                "review_uncommitted": True,
                "review_base": "main",
            },
            action="review",
        )


def test_claude_profile_command_is_server_owned_restricted_and_bounded() -> None:
    registry = AgentProfileRegistry({"claude": ["claude"]})
    profile = registry.get("claude-default")
    command = registry.build_command(
        profile,
        ["C:/tools/claude.exe"],
        prompt="检查项目",
        cwd="E:/ExampleWorkspace",
        sandbox="read-only",
    )
    assert command == [
        "C:/tools/claude.exe",
        "-p",
        "检查项目",
        "--output-format",
        "stream-json",
        "--verbose",
        "--safe-mode",
        "--restricted",
        "--no-chrome",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--permission-mode",
        "plan",
        "--tools",
        "Read,Glob,Grep",
    ]
    resumed = registry.build_command(
        profile,
        ["C:/tools/claude.exe"],
        prompt="继续",
        cwd="E:/ExampleWorkspace",
        sandbox="workspace-write",
        native_session_id="claude_session_1",
    )
    assert resumed[1:3] == ["-p", "继续"]
    assert resumed[-2:] == ["--resume", "claude_session_1"]
    assert resumed[resumed.index("--permission-mode") + 1] == "acceptEdits"
    assert resumed[resumed.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write"
    forbidden = {
        "--settings",
        "--agents",
        "--agent",
        "--plugin-dir",
        "--mcp-config",
        "--add-dir",
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--continue",
    }
    assert forbidden.isdisjoint(resumed)
    with pytest.raises(ValueError, match="NUL"):
        registry.build_command(
            profile,
            ["C:/tools/claude.exe"],
            prompt="bad\x00prompt",
            cwd="E:/ExampleWorkspace",
            sandbox="read-only",
        )


def test_claude_stream_json_parser_ignores_tool_payloads_and_redacts() -> None:
    parser = ClaudeJsonlParser()
    started = parser.feed_line(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "claude_session_1",
            }
        )
    )
    assert started is not None
    assert started.type == "thread_started"
    assert parser.native_session_id == "claude_session_1"
    assert parser.feed_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "input": {"secret": "do-not-store"}}
                    ]
                },
            }
        )
    ) is None
    result = parser.feed_line(
        json.dumps(
            {
                "type": "result",
                "session_id": "claude_session_1",
                "is_error": False,
                "result": "EXAMPLE_AGENT_KEY=fixture-secret 完成",
            }
        )
    )
    assert result is not None
    assert result.type == "agent_message"
    assert "fixture-secret" not in result.summary
    assert parser.final_message is not None
    assert "fixture-secret" not in parser.final_message
    assert isinstance(ClaudeCodeAdapter(), AgentAdapter)


def test_agent_registry_exposes_capabilities_and_stable_unsupported_errors() -> None:
    registry = AgentProfileRegistry({"codex": ["codex"]})
    providers = registry.providers()
    assert providers == (
        {
            "provider": "codex",
            "display_name": "Codex",
            "available": True,
            "profiles": ["codex-default"],
            "tested_cli_version": "0.153.0",
            "capabilities": {
                "create": True,
                "attach": True,
                "resume": True,
                "discover": False,
                "stream": True,
                "cancel": True,
                "steer": False,
                "interaction": False,
                "fork": True,
                "review": True,
            },
        },
    )
    profile = registry.get("codex-default")
    assert profile.provider == "codex"
    assert profile.codex_config_profile == ""
    assert not hasattr(profile, "agent")
    assert not hasattr(profile, "codex_profile")
    assert not hasattr(registry, "get_adapter")
    assert registry.profile_summaries() == (
        {
            "profile": "codex-default",
            "provider": "codex",
            "auth_mode": "existing-login",
            "runtime_home_isolated": False,
        },
    )
    assert registry.adapter_for_profile(profile).provider == "codex"
    assert isinstance(CodexAdapter(), AgentAdapter)
    registry.require_capability(profile, "attach")
    with pytest.raises(ValueError, match="Unknown agent capability"):
        registry.require_capability(profile, "teleport")


class _FakeParser:
    def __init__(self) -> None:
        self.next_seq = 0
        self.native_session_id: str | None = None
        self.final_message: str | None = None

    def _event(self, event_type: str, summary: str) -> NormalizedEvent:
        event = NormalizedEvent(self.next_seq, event_type, summary, {})
        self.next_seq += 1
        return event

    def feed_line(self, line: str) -> NormalizedEvent | None:
        if line.startswith("SESSION:"):
            self.native_session_id = line.removeprefix("SESSION:")
            return self._event("session_started", "Fake session started")
        if line.startswith("MESSAGE:"):
            self.final_message = line.removeprefix("MESSAGE:")
            return self._event("agent_message", self.final_message)
        return None

    def synthetic_event(
        self,
        event_type: str,
        summary_value: object,
        data: dict[str, object] | None = None,
    ) -> NormalizedEvent:
        event = NormalizedEvent(
            self.next_seq,
            event_type,
            str(summary_value),
            {str(key): str(value) for key, value in (data or {}).items()},
        )
        self.next_seq += 1
        return event


class _FakeAdapter:
    provider = "fake"
    display_name = "Fake Agent"
    command = "fake-agent"
    capabilities = AdapterCapabilities(resume=True)

    def profiles(self) -> tuple[AgentProfile, ...]:
        return (
            AgentProfile(
                name="fake-default",
                provider=self.provider,
                command=self.command,
                provider_profile="default",
            ),
        )

    def probe(self, executable_prefix: list[str] | None) -> bool:
        return bool(executable_prefix)

    def new_parser(self) -> _FakeParser:
        return _FakeParser()

    def build_command(
        self,
        profile: AgentProfile,
        executable_prefix: list[str],
        *,
        prompt: str,
        cwd: str,
        sandbox: str,
        native_session_id: str | None = None,
        invocation_options: dict[str, object] | None = None,
        action: str = "continue",
    ) -> list[str]:
        assert profile.provider == self.provider
        assert cwd
        assert not invocation_options
        assert action == "continue"
        profile.validate_sandbox(sandbox)
        return [*executable_prefix, native_session_id or "new", prompt]


class _BadPrefixAdapter(_FakeAdapter):
    def build_command(
        self,
        profile: AgentProfile,
        executable_prefix: list[str],
        *,
        prompt: str,
        cwd: str,
        sandbox: str,
        native_session_id: str | None = None,
        invocation_options: dict[str, object] | None = None,
        action: str = "continue",
    ) -> list[str]:
        return ["unregistered-executable", prompt]


def test_agent_runtime_uses_registered_adapter_and_provider_binding(
    workspace, tmp_path
) -> None:
    script = tmp_path / "fake_agent.py"
    script.write_text(
        "import sys\n"
        "native_id = sys.argv[1]\n"
        "if native_id == 'new':\n"
        "    native_id = 'native_fake_1'\n"
        "print('SESSION:' + native_id, flush=True)\n"
        "print('MESSAGE:' + sys.argv[2], flush=True)\n",
        encoding="utf-8",
    )
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    service._exec_commands["fake-agent"] = [sys.executable, str(script)]
    service.agent_profiles = AgentProfileRegistry(
        service._exec_commands, adapters=(_FakeAdapter(),)
    )
    assert service.workspace_info()["available_agent_providers"][0]["provider"] == "fake"

    session = service.agent_session_create(profile="fake-default")
    assert session["provider"] == "fake"
    assert session["native_session_id"] is None
    assert "thread_id" not in session
    first = service.agent_run_start(session["session_id"], "first")
    completed = _wait_for_run(service, session["session_id"], first["run_id"])
    assert completed["provider"] == "fake"
    assert completed["native_session_id"] == "native_fake_1"
    assert completed["native_session_id"] == "native_fake_1"
    assert "thread_id" not in completed
    result = service.agent_run_result(session["session_id"], first["run_id"])
    assert result["result"] == "first"

    second = service.agent_run_start(session["session_id"], "second")
    _wait_for_run(service, session["session_id"], second["run_id"])
    assert service.agent_run_result(session["session_id"], second["run_id"])[
        "result"
    ] == "second"

    state = service._get_agent_session(session["session_id"])
    state.provider = "codex"
    with pytest.raises(RuntimeError, match="provider binding"):
        service.agent_run_start(session["session_id"], "must fail closed")


def test_agent_runtime_rejects_adapter_executable_prefix_changes(
    workspace, tmp_path
) -> None:
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    service._exec_commands["fake-agent"] = [sys.executable, "missing-fake-agent.py"]
    service.agent_profiles = AgentProfileRegistry(
        service._exec_commands, adapters=(_BadPrefixAdapter(),)
    )
    session = service.agent_session_create(profile="fake-default")
    with pytest.raises(RuntimeError, match="invalid executable prefix"):
        service.agent_run_start(session["session_id"], "must not launch")
    assert service._processes == {}


def test_prepared_process_entrypoint_revalidates_prefix_and_workspace(
    workspace, tmp_path
) -> None:
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    prefix = service._exec_commands["python"]
    with pytest.raises(PermissionError, match="registered executable"):
        service._start_managed_process_prepared(
            "python",
            [sys.executable + ".replaced", "-V"],
            workspace,
            max_runtime_seconds=10,
            output_limit_bytes=4096,
        )
    with pytest.raises(WorkspaceSecurityError, match="outside the workspace"):
        service._start_managed_process_prepared(
            "python",
            [*prefix, "-V"],
            tmp_path,
            max_runtime_seconds=10,
            output_limit_bytes=4096,
        )


def test_codex_jsonl_parser_extracts_thread_and_final_message() -> None:
    parser = CodexJsonlParser()
    assert not hasattr(parser, "feed")
    events = [
        event
        for line in [
            "not json",
            '{"type":"thread.started","thread_id":"thr_123"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"完成检查"}}',
            '{"type":"turn.completed"}',
        ]
        if (event := parser.feed_line(line)) is not None
    ]
    assert [event.type for event in events] == ["thread_started", "agent_message", "status"]
    assert parser.native_session_id == "thr_123"
    assert events[0].as_dict()["data"] == {"native_session_id": "thr_123"}
    assert parser.final_message == "完成检查"
    assert events[1].as_dict()["data"] == {"text": "完成检查"}
    assert events[-1].seq == 2


def test_event_parser_redacts_secrets_and_bounds_data() -> None:
    redacted, clipped = redact_text("Authorization: Bearer abcdefghijklmnop")
    assert "abcdefghijklmnop" not in redacted
    assert "<redacted>" in redacted
    assert clipped is False
    parser = CodexJsonlParser()
    event = parser.feed_line(
        '{"type":"error","message":"EXAMPLE_AGENT_KEY=super-secret-value"}'
    )
    assert event is not None
    assert "super-secret-value" not in event.summary
    assert "super-secret-value" not in str(event.data)


@pytest.mark.parametrize(
    "secret",
    [
        "sk-example1234567890",
        "github_pat_example1234567890",
        "ghp_example1234567890",
        "gho_example1234567890",
        "glpat-example1234567890",
        "xoxb-example-1234567890",
    ],
)
def test_agent_redaction_removes_bare_provider_tokens(secret: str) -> None:
    redacted, clipped = redact_text(f"provider error: {secret}")
    assert secret not in redacted
    assert redacted == "provider error: <redacted>"
    assert clipped is False


def test_agent_session_is_workspace_bound_and_closable(workspace, tmp_path) -> None:
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    if not service.agent_profiles.names():
        pytest.skip("Codex executable is unavailable")
    session = service.agent_session_create()
    assert session["profile"] == "codex-default"
    assert session["cwd"] == "."
    assert session["sandbox"] == "workspace-write"
    assert service.agent_session_inspect(session["session_id"])["closed"] is False
    with pytest.raises((ValueError, PermissionError)):
        service.agent_session_create(cwd="..")
    with pytest.raises((ValueError, PermissionError)):
        service.agent_session_create(cwd=str(tmp_path))
    closed = service.agent_session_close(session["session_id"])
    assert closed["closed"] is True
    with pytest.raises(PermissionError, match="closed"):
        service.agent_run_start(session["session_id"], "hello")


def test_agent_run_mvp_returns_and_pages_normalized_events(workspace, tmp_path) -> None:
    script = tmp_path / "fake_codex.py"
    script.write_text(
        "import json; print(json.dumps({'type':'thread.started','thread_id':'thr_fake'}), flush=True); "
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'完成'}}), flush=True)",
        encoding="utf-8",
    )
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    service._exec_commands["codex"] = [sys.executable, str(script)]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    session = service.agent_session_create()
    started = service.agent_run_start(session["session_id"], "say hello")
    deadline = time.monotonic() + 5
    page = None
    while time.monotonic() < deadline:
        page = service.agent_run_events(session["session_id"], started["run_id"])
        if page["state"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert page is not None
    assert page["state"] == "succeeded"
    assert page["native_session_id"] == "thr_fake"
    assert "thread_id" not in page
    assert [event["type"] for event in page["events"]] == [
        "thread_started",
        "agent_message",
        "completed",
    ]
    assert page["events"][1]["data"]["text"] == "完成"
    inspected = service.agent_run_inspect(session["session_id"], started["run_id"])
    assert inspected["result_ready"] is True
    assert inspected["has_result"] is True
    result = service.agent_run_result(session["session_id"], started["run_id"], max_bytes=3)
    assert result["result"] == "完"
    assert result["truncated"] is True


def test_agent_run_receives_immediate_stdin_eof(workspace, tmp_path) -> None:
    """Codex exec must not wait for an open managed-process stdin pipe."""

    script = tmp_path / "fake_codex_stdin.py"
    script.write_text(
        "import json, sys\n"
        "extra = sys.stdin.read()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'thr_eof'}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':extra or 'eof'}}), flush=True)\n",
        encoding="utf-8",
    )
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    service._exec_commands["codex"] = [sys.executable, str(script)]
    service.agent_profiles = AgentProfileRegistry(["codex"])

    session = service.agent_session_create()
    started = service.agent_run_start(session["session_id"], "say hello")
    completed = _wait_for_run(service, session["session_id"], started["run_id"])

    assert completed["state"] == "succeeded"
    assert service.agent_run_result(session["session_id"], started["run_id"])[
        "result"
    ] == "eof"
    assert "process_id" not in started
    process = service._get_managed_process(
        service._get_agent_run(session["session_id"], started["run_id"])[1].process_id
    )
    assert process.stdin_closed is True


def test_isolated_agent_profile_freezes_and_injects_isolated_codex_home(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(prefix="tc-agent-home-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        home = workspace / "AgentHomes" / "IsolatedCodex"
        room = workspace / "Rooms" / "示例目录"
        home.mkdir(parents=True)
        room.mkdir(parents=True)
        script = root / "fake_codex_home.py"
        script.write_text(
            "import json, os\n"
            "print(json.dumps({'type':'thread.started','thread_id':'thr_home'}), flush=True)\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':os.environ.get('CODEX_HOME', '')}}), flush=True)\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(root / "parent-codex-home"))
        service = TianChengService(
            workspace,
            root / "audit",
            allow_exec=True,
            access_policy=AccessPolicy.default(workspace),
            agent_source_policy=AgentSourcePolicy.empty(),
            enable_agent_catalog=False,
        )
        service._exec_commands["codex"] = [sys.executable, str(script)]
        service.agent_profiles = AgentProfileRegistry(
            {"codex": [sys.executable, str(script)]},
            profile_definitions=(
                {
                    "name": "isolated-agent",
                    "provider": "codex",
                    "provider_profile": "isolated-agent",
                    "codex_home": str(home),
                    "credential_env": "ISOLATED_AGENT_KEY",
                },
            ),
        )
        try:
            info = service.workspace_info()
            assert "isolated-agent" in info["available_agent_profiles"]
            assert {
                "profile": "isolated-agent",
                "provider": "codex",
                "auth_mode": "env",
                "runtime_home_isolated": True,
            } in info["agent_profile_metadata"]

            session = service.agent_session_create(
                profile="isolated-agent", cwd="Rooms/示例目录"
            )
            assert session["runtime_home_isolated"] is True
            assert "codex_home" not in session
            inspected = service.agent_session_inspect(session["session_id"])
            assert inspected["runtime_home_isolated"] is True
            assert "codex_home" not in inspected

            started = service.agent_run_start(session["session_id"], "wake up")
            completed = _wait_for_run(service, session["session_id"], started["run_id"])
            assert completed["state"] == "succeeded"
            result = service.agent_run_result(session["session_id"], started["run_id"])
            assert Path(result["result"]).resolve() == home.resolve()
        finally:
            service.shutdown()


def test_isolated_codex_home_is_validated_at_create_and_each_run() -> None:
    with tempfile.TemporaryDirectory(prefix="tc-agent-home-policy-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        home = workspace / "AgentHomes" / "IsolatedCodex"
        home.mkdir(parents=True)
        service = TianChengService(
            workspace,
            root / "audit",
            allow_exec=True,
            access_policy=AccessPolicy.default(workspace),
            agent_source_policy=AgentSourcePolicy.empty(),
            enable_agent_catalog=False,
        )
        service.agent_profiles = AgentProfileRegistry(
            ["codex"],
            profile_definitions=(
                {
                    "name": "isolated-agent",
                    "provider": "codex",
                    "provider_profile": "isolated-agent",
                    "codex_home": str(home),
                    "credential_env": "ISOLATED_AGENT_KEY",
                },
            ),
        )
        original = service.agent_profiles.get("isolated-agent")
        service.agent_profiles._profiles["isolated-agent"] = replace(
            original, codex_home=str(home)
        )
        try:
            session = service.agent_session_create(profile="isolated-agent")
            service.access_policy = AccessPolicy(
                workspace,
                [
                    AccessRule(path=workspace, mode="full"),
                    AccessRule(path=home, mode="deny"),
                ],
            )
            with pytest.raises(PermissionError):
                service.agent_run_start(session["session_id"], "must fail closed")

            sensitive = workspace / "secrets" / "IsolatedCodex"
            sensitive.mkdir(parents=True)
            service.access_policy = AccessPolicy.default(workspace)
            service.agent_profiles._profiles["isolated-agent"] = replace(
                original, codex_home=str(sensitive)
            )
            with pytest.raises(PermissionError, match="sensitive path component"):
                service.agent_session_create(profile="isolated-agent")

            service.agent_profiles._profiles["isolated-agent"] = replace(
                original, codex_home="relative-home"
            )
            with pytest.raises(ValueError, match="absolute path"):
                service.agent_session_create(profile="isolated-agent")

            service.agent_profiles._profiles["isolated-agent"] = replace(
                original, codex_home=str(workspace / "AgentHomes" / "missing")
            )
            with pytest.raises(FileNotFoundError, match="must already exist"):
                service.agent_session_create(profile="isolated-agent")
        finally:
            service.shutdown()


def test_agent_run_resumes_only_its_bound_thread(workspace, tmp_path) -> None:
    script = tmp_path / "fake_resume_codex.py"
    argument_log = tmp_path / "arguments.jsonl"
    script.write_text(
        "import json, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "with path.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(sys.argv[2:]) + '\\n')\n"
        "if 'resume' not in sys.argv:\n"
        "    print(json.dumps({'type':'thread.started','thread_id':'thr_resume'}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}), flush=True)\n",
        encoding="utf-8",
    )
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    service._exec_commands["codex"] = [sys.executable, str(script), str(argument_log)]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    session = service.agent_session_create()
    first = service.agent_run_start(session["session_id"], "first")
    _wait_for_run(service, session["session_id"], first["run_id"])
    second = service.agent_run_start(session["session_id"], "second")
    _wait_for_run(service, session["session_id"], second["run_id"])

    calls = [json.loads(line) for line in argument_log.read_text(encoding="utf-8").splitlines()]
    assert "resume" not in calls[0]
    resume_index = calls[1].index("resume")
    assert calls[1][resume_index : resume_index + 3] == ["resume", "thr_resume", "second"]


def test_codex_run_fork_and_review_update_the_managed_binding(workspace, tmp_path) -> None:
    script = tmp_path / "fake_codex_actions.py"
    argument_log = tmp_path / "codex-actions.jsonl"
    script.write_text(
        "import json, pathlib, sys\n"
        "args = sys.argv[2:]\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "with path.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(args) + '\\n')\n"
        "thread_id = 'thr_reviewed' if 'review' in args else "
        "('thr_forked' if 'fork' in args else 'thr_base')\n"
        "print(json.dumps({'type':'thread.started','thread_id':thread_id}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}), flush=True)\n",
        encoding="utf-8",
    )
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    service._exec_commands["codex"] = [sys.executable, str(script), str(argument_log)]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    session = service.agent_session_create()

    created = service.agent_run_start(session["session_id"], "create")
    _wait_for_run(service, session["session_id"], created["run_id"])
    forked = service.agent_run_start(
        session["session_id"], "", {"model": "gpt-5.6-sol"}, "fork"
    )
    _wait_for_run(service, session["session_id"], forked["run_id"])
    reviewed = service.agent_run_start(
        session["session_id"],
        "focus on races",
        {"review_base": "main", "review_title": "Concurrency"},
        "review",
    )
    _wait_for_run(service, session["session_id"], reviewed["run_id"])

    calls = [
        json.loads(line)
        for line in argument_log.read_text(encoding="utf-8").splitlines()
    ]
    fork_index = calls[1].index("fork")
    assert calls[1][fork_index:] == ["fork", "thr_base"]
    review_index = calls[2].index("review")
    assert calls[2][review_index:] == [
        "review",
        "--base",
        "main",
        "--title",
        "Concurrency",
        "focus on races",
    ]
    inspected = service.agent_session_inspect(session["session_id"])
    assert inspected["native_session_id"] == "thr_reviewed"
    assert reviewed["codex_action"] == "review"
    assert reviewed["codex_options"]["review_base_configured"] is True

    ephemeral_session = service.agent_session_create()
    ephemeral = service.agent_run_start(
        ephemeral_session["session_id"],
        "one shot",
        {"ephemeral": True},
    )
    _wait_for_run(
        service, ephemeral_session["session_id"], ephemeral["run_id"]
    )
    assert service.agent_session_inspect(ephemeral_session["session_id"])[
        "native_session_id"
    ] is None


def test_codex_session_defaults_run_overrides_paths_and_route_environment(
    workspace, tmp_path
) -> None:
    image = workspace / "input.png"
    schema = workspace / "schema.json"
    extra = workspace / "extra"
    image.write_bytes(b"fixture")
    schema.write_text('{"type":"object"}', encoding="utf-8")
    extra.mkdir()
    argument_log = tmp_path / "codex-options.jsonl"
    script = tmp_path / "fake_codex_options.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "with path.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'args': sys.argv[2:], 'route': os.environ.get('AWZ_ROUTE')}) + '\\n')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'thr_options'}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}), flush=True)\n",
        encoding="utf-8",
    )
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    service._exec_commands["codex"] = [sys.executable, str(script), str(argument_log)]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    session = service.agent_session_create(
        sandbox="workspace-write",
        codex_defaults={
            "model": "gpt-5.6-sol",
            "route": "primary",
            "images": ["input.png"],
            "add_dirs": ["extra"],
            "output_schema": "schema.json",
            "output_last_message": "last.txt",
        },
    )
    assert session["codex_defaults"] == {
        "model": "gpt-5.6-sol",
        "route_configured": True,
        "images_count": 1,
        "add_dirs_count": 1,
        "output_schema_configured": True,
        "output_last_message_configured": True,
    }

    first = service.agent_run_start(
        session["session_id"],
        "first",
        {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
    )
    _wait_for_run(service, session["session_id"], first["run_id"])
    assert first["codex_options"]["model"] == "gpt-5.6-luna"
    assert first["codex_options"]["reasoning_effort"] == "max"
    inspected = service.agent_run_inspect(session["session_id"], first["run_id"])
    assert inspected["codex_options"] == first["codex_options"]

    second = service.agent_run_start(
        session["session_id"],
        "second",
        {"route": None, "model": "gpt-5.6-terra"},
    )
    _wait_for_run(service, session["session_id"], second["run_id"])

    calls = [
        json.loads(line)
        for line in argument_log.read_text(encoding="utf-8").splitlines()
    ]
    assert calls[0]["route"] == "primary"
    assert calls[1]["route"] is None
    assert calls[0]["args"][calls[0]["args"].index("-m") + 1] == "gpt-5.6-luna"
    assert calls[1]["args"][calls[1]["args"].index("-m") + 1] == "gpt-5.6-terra"
    assert str(image.resolve()) in calls[0]["args"]
    assert str(schema.resolve()) in calls[0]["args"]
    assert str(extra.resolve()) in calls[0]["args"]
    assert str((workspace / "last.txt").resolve()) in calls[0]["args"]


def test_codex_option_paths_reject_escape_and_claude_options(workspace, tmp_path) -> None:
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    service._exec_commands["codex"] = [sys.executable, "fake-codex.py"]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    with pytest.raises((PermissionError, WorkspaceSecurityError)):
        service.agent_session_create(codex_defaults={"images": ["../secret.png"]})
    with pytest.raises(ValueError, match="per-run"):
        service.agent_session_create(
            codex_defaults={"review_uncommitted": True}
        )

    claude = TianChengService(workspace, tmp_path / "claude-audit", allow_exec=False)
    claude.agent_profiles = AgentProfileRegistry(
        {"claude": [sys.executable]}, adapters=(ClaudeCodeAdapter(),)
    )
    with pytest.raises(NotImplementedError, match="Codex options"):
        claude.agent_session_create(
            profile="claude-default",
            codex_defaults={"model": "gpt-5.6-sol"},
        )


def test_agent_session_attaches_catalog_ref_and_reauthorizes_each_run(
    workspace, tmp_path
) -> None:
    codex_root = tmp_path / ".codex" / "sessions"
    rollout = codex_root / "2026" / "08" / "30" / "rollout-attached.jsonl"
    rollout.parent.mkdir(parents=True)
    session_meta = {
        "timestamp": "2026-08-30T10:00:00Z",
        "type": "session_meta",
        "payload": {"id": "thr_catalog_attach", "cwd": str(workspace)},
    }
    rollout.write_text(json.dumps(session_meta) + "\n", encoding="utf-8")
    policy = AgentSourcePolicy.from_payload(
        {
            "schema_version": 1,
            "sources": [
                {
                    "source_id": "src_codex_attach",
                    "provider": "codex",
                    "root": str(codex_root),
                    "mode": "catalog-read",
                }
            ],
        }
    )
    argument_log = tmp_path / "attach-arguments.jsonl"
    script = tmp_path / "fake_attach_codex.py"
    script_body = (
        "import json, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1])\n"
        "with path.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(sys.argv[2:]) + '\\n')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'thr_catalog_attach'}), flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ok'}}), flush=True)\n"
    )
    script.write_text(script_body, encoding="utf-8")
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_exec=True,
        agent_source_policy=policy,
        agent_catalog_path=tmp_path / "state" / "catalog.sqlite3",
    )
    service._exec_commands["codex"] = [
        sys.executable,
        str(script),
        str(argument_log),
    ]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    service.agent_catalog_refresh("src_codex_attach")
    record = service.agent_catalog_list(provider="codex")["conversations"][0]
    assert record["attachable"] is True
    with pytest.raises(FileNotFoundError):
        service.agent_session_attach("convref_" + ("0" * 32))
    with pytest.raises(ValueError, match="conversation_ref"):
        service.agent_session_attach("thr_catalog_attach")

    attached = service.agent_session_attach(record["conversation_ref"])
    assert attached["origin"] == "catalog"
    assert attached["conversation_ref"] == record["conversation_ref"]
    assert attached["native_session_id"] == "thr_catalog_attach"
    first = service.agent_run_start(attached["session_id"], "first")
    _wait_for_run(service, attached["session_id"], first["run_id"])

    with rollout.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "response_item", "payload": {}}) + "\n")
    second = service.agent_run_start(attached["session_id"], "second")
    _wait_for_run(service, attached["session_id"], second["run_id"])
    calls = [
        json.loads(line)
        for line in argument_log.read_text(encoding="utf-8").splitlines()
    ]
    for call, prompt in zip(calls, ("first", "second"), strict=True):
        assert "--last" not in call
        resume_index = call.index("resume")
        assert call[resume_index : resume_index + 3] == [
            "resume",
            "thr_catalog_attach",
            prompt,
        ]

    script.write_text(
        script_body.replace("thr_catalog_attach'}", "thr_wrong_binding'}"),
        encoding="utf-8",
    )
    mismatch = service.agent_run_start(attached["session_id"], "mismatch")
    mismatch_result = _wait_for_run(
        service, attached["session_id"], mismatch["run_id"]
    )
    assert mismatch_result["state"] == "failed"
    assert "different native session id" in str(mismatch_result["error"])
    assert service.agent_session_inspect(attached["session_id"])[
        "native_session_id"
    ] == "thr_catalog_attach"

    disabled_policy = AgentSourcePolicy.from_payload(
        {
            "schema_version": 1,
            "sources": [
                {
                    "source_id": "src_codex_attach",
                    "provider": "codex",
                    "root": str(codex_root),
                    "mode": "catalog-read",
                    "enabled": False,
                }
            ],
        }
    )
    service.agent_source_policy = disabled_policy
    with pytest.raises(PermissionError, match="disabled"):
        service.agent_run_start(attached["session_id"], "disabled")
    service.agent_source_policy = policy

    preserved = rollout.with_suffix(".preserved")
    rollout.rename(preserved)
    rollout.write_text(json.dumps(session_meta) + "\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="identity changed"):
        service.agent_run_start(attached["session_id"], "third")


def test_agent_session_attach_rejects_history_outside_workspace(
    workspace, tmp_path
) -> None:
    codex_root = tmp_path / ".codex" / "sessions"
    rollout = codex_root / "2026" / "08" / "30" / "rollout-external-cwd.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "thr_external_cwd",
                    "cwd": str(tmp_path / "outside-workspace"),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    policy = AgentSourcePolicy.from_payload(
        {
            "schema_version": 1,
            "sources": [
                {
                    "source_id": "src_codex_external",
                    "provider": "codex",
                    "root": str(codex_root),
                    "mode": "catalog-read",
                }
            ],
        }
    )
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_exec=True,
        agent_source_policy=policy,
        agent_catalog_path=tmp_path / "state" / "catalog.sqlite3",
    )
    service._exec_commands["codex"] = [sys.executable, "unused.py"]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    service.agent_catalog_refresh("src_codex_external")
    record = service.agent_catalog_list(provider="codex")["conversations"][0]
    assert record["attachable"] is False
    with pytest.raises(PermissionError, match="not covered by the workspace"):
        service.agent_session_attach(record["conversation_ref"])


def test_claude_agent_only_runtime_creates_resumes_and_attaches_catalog(
    workspace, tmp_path, monkeypatch
) -> None:
    claude_root = tmp_path / ".claude" / "projects"
    history = claude_root / "project" / "claude_catalog_1.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": "claude_catalog_1",
                "cwd": str(workspace),
                "timestamp": "2026-08-30T11:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    policy = AgentSourcePolicy.from_payload(
        {
            "schema_version": 1,
            "sources": [
                {
                    "source_id": "src_claude_runtime",
                    "provider": "claude-code",
                    "root": str(claude_root),
                    "mode": "catalog-read",
                }
            ],
        }
    )
    argument_log = tmp_path / "claude-arguments.jsonl"
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "log = pathlib.Path(sys.argv[1])\n"
        "args = sys.argv[2:]\n"
        "if os.environ.get('EXAMPLE_AGENT_KEY'):\n"
        "    args.append('UNEXPECTED_AGENT_ENV')\n"
        "with log.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(args) + '\\n')\n"
        "session_id = args[args.index('--resume') + 1] if '--resume' in args else 'claude_new_1'\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':session_id}), flush=True)\n"
        "print(json.dumps({'type':'result','session_id':session_id,'is_error':False,'result':'claude ok'}), flush=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EXAMPLE_AGENT_KEY", "fixture-secret-never-log")
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_exec=True,
        passthrough_env=("EXAMPLE_AGENT_KEY",),
        agent_source_policy=policy,
        agent_catalog_path=tmp_path / "state" / "catalog.sqlite3",
    )
    claude_prefix = [sys.executable, str(script), str(argument_log)]
    service._agent_only_commands["claude"] = claude_prefix
    service.agent_profiles = AgentProfileRegistry({"claude": claude_prefix})
    with pytest.raises(PermissionError, match="not allowlisted"):
        service.run_command("claude", ["--dangerously-skip-permissions"])

    created = service.agent_session_create(profile="claude-default")
    assert created["provider"] == "claude-code"
    first = service.agent_run_start(created["session_id"], "first")
    _wait_for_run(service, created["session_id"], first["run_id"])
    assert service.agent_run_result(created["session_id"], first["run_id"])[
        "result"
    ] == "claude ok"
    second = service.agent_run_start(created["session_id"], "second")
    _wait_for_run(service, created["session_id"], second["run_id"])

    service.agent_catalog_refresh("src_claude_runtime")
    record = service.agent_catalog_list(provider="claude-code")["conversations"][0]
    assert record["attachable"] is True
    attached = service.agent_session_attach(
        record["conversation_ref"], profile="claude-default"
    )
    attached_run = service.agent_run_start(attached["session_id"], "attached")
    _wait_for_run(service, attached["session_id"], attached_run["run_id"])

    calls = [
        json.loads(line)
        for line in argument_log.read_text(encoding="utf-8").splitlines()
    ]
    assert "--resume" not in calls[0]
    assert calls[1][calls[1].index("--resume") + 1] == "claude_new_1"
    assert calls[2][calls[2].index("--resume") + 1] == "claude_catalog_1"
    for call in calls:
        assert "--restricted" in call
        assert "--safe-mode" in call
        assert "--strict-mcp-config" in call
        assert "--dangerously-skip-permissions" not in call
        assert "UNEXPECTED_AGENT_ENV" not in call


def test_agent_failure_is_bounded_and_redacted(workspace, tmp_path) -> None:
    script = tmp_path / "fake_failed_codex.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('EXAMPLE_AGENT_KEY=super-secret-value ' + ('x' * 50000))\n"
        "raise SystemExit(3)\n",
        encoding="utf-8",
    )
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    service._exec_commands["codex"] = [sys.executable, str(script)]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    session = service.agent_session_create()
    started = service.agent_run_start(session["session_id"], "fail")
    inspected = _wait_for_run(service, session["session_id"], started["run_id"])
    page = service.agent_run_events(session["session_id"], started["run_id"])

    assert inspected["state"] == "failed"
    assert inspected["exit_code"] == 3
    assert len(page["events"]) == 1
    error = page["events"][0]
    assert error["type"] == "error"
    assert "super-secret-value" not in json.dumps(error)
    assert "<redacted>" in json.dumps(error)
    assert error["truncated"] is True


def test_agent_events_wait_and_retention_cursor_gap(workspace, tmp_path) -> None:
    script = tmp_path / "fake_delayed_codex.py"
    script.write_text(
        "import json, time\n"
        "time.sleep(0.2)\n"
        "print(json.dumps({'type':'thread.started','thread_id':'thr_wait'}), flush=True)\n",
        encoding="utf-8",
    )
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    service._exec_commands["codex"] = [sys.executable, str(script)]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    session = service.agent_session_create()
    started = service.agent_run_start(session["session_id"], "wait")
    page = service.agent_run_events(
        session["session_id"], started["run_id"], after_seq=0, wait_ms=2_000
    )
    assert page["events"][0]["type"] == "thread_started"
    with pytest.raises(ValueError, match="wait_ms"):
        service.agent_run_events(
            session["session_id"], started["run_id"], wait_ms=10_001
        )

    retained = AgentRunState("run_" + "a" * 32, "sess_" + "b" * 32, "c" * 32)
    for index in range(MAX_AGENT_EVENTS + 5):
        TianChengService._append_agent_event(
            retained,
            retained.parser.synthetic_event("status", f"event {index}"),
        )
    assert len(retained.events) == MAX_AGENT_EVENTS
    assert retained.events[0].seq == 5


def test_agent_cancel_timeout_and_shutdown_are_distinct(workspace, tmp_path) -> None:
    script = tmp_path / "fake_sleep_codex.py"
    script.write_text("import time; time.sleep(30)\n", encoding="utf-8")

    cancel_service = TianChengService(workspace, tmp_path / "audit-cancel", allow_exec=True)
    cancel_service._exec_commands["codex"] = [sys.executable, str(script)]
    cancel_service.agent_profiles = AgentProfileRegistry(["codex"])
    cancel_session = cancel_service.agent_session_create()
    cancel_run = cancel_service.agent_run_start(cancel_session["session_id"], "cancel")
    assert cancel_run["max_runtime_seconds"] == 3600
    assert "process_id" not in cancel_run
    agent_process_id = cancel_service._get_agent_run(
        cancel_session["session_id"], cancel_run["run_id"]
    )[1].process_id
    assert cancel_service.list_processes()["count"] == 0
    for operation in (
        lambda: cancel_service.process_status(agent_process_id),
        lambda: cancel_service.process_output(agent_process_id),
        lambda: cancel_service.process_input(agent_process_id, "secret"),
        lambda: cancel_service.stop_process(agent_process_id, force=True),
    ):
        with pytest.raises(PermissionError, match="agent_run"):
            operation()
    assert cancel_service.agent_run_inspect(
        cancel_session["session_id"], cancel_run["run_id"]
    )["state"] == "running"
    assert "process_id" not in cancel_service.agent_run_inspect(
        cancel_session["session_id"], cancel_run["run_id"]
    )
    with pytest.raises(RuntimeError, match="one active run"):
        cancel_service.agent_run_start(cancel_session["session_id"], "overlap")
    cancelled = cancel_service.agent_run_cancel(
        cancel_session["session_id"], cancel_run["run_id"], "EXAMPLE_AGENT_KEY=hidden"
    )
    assert cancelled["state"] == "cancelled"
    assert "hidden" not in cancelled["reason"]

    timeout_service = TianChengService(workspace, tmp_path / "audit-timeout", allow_exec=True)
    timeout_service._exec_commands["codex"] = [sys.executable, str(script)]
    timeout_service.agent_profiles = AgentProfileRegistry(["codex"])
    timeout_session = timeout_service.agent_session_create()
    timeout_run = timeout_service.agent_run_start(
        timeout_session["session_id"], "timeout", max_runtime_seconds=1
    )
    assert timeout_run["max_runtime_seconds"] == 1
    timed_out = _wait_for_run(
        timeout_service, timeout_session["session_id"], timeout_run["run_id"], timeout=5
    )
    assert timed_out["state"] == "timed_out"
    assert timed_out["max_runtime_seconds"] == 1

    limit_session = timeout_service.agent_session_create()
    with pytest.raises(ValueError, match="between 1 and 10800"):
        timeout_service.agent_run_start(
            limit_session["session_id"], "too long", max_runtime_seconds=10_801
        )

    shutdown_service = TianChengService(workspace, tmp_path / "audit-shutdown", allow_exec=True)
    shutdown_service._exec_commands["codex"] = [sys.executable, str(script)]
    shutdown_service.agent_profiles = AgentProfileRegistry(["codex"])
    shutdown_session = shutdown_service.agent_session_create()
    shutdown_run = shutdown_service.agent_run_start(shutdown_session["session_id"], "shutdown")
    shutdown_service.shutdown()
    assert shutdown_service.agent_session_inspect(shutdown_session["session_id"])["closed"] is True
    stopped = shutdown_service.agent_run_inspect(
        shutdown_session["session_id"], shutdown_run["run_id"]
    )
    assert stopped["state"] == "aborted_on_shutdown"
    assert shutdown_service._process_status(
        shutdown_service._get_agent_run(
            shutdown_session["session_id"], shutdown_run["run_id"]
        )[1].process_id
    )["running"] is False


def test_agent_session_serializes_concurrent_starts(workspace, tmp_path, monkeypatch) -> None:
    script = tmp_path / "fake_concurrent_codex.py"
    script.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    service = TianChengService(workspace, tmp_path / "audit", allow_exec=True)
    service._exec_commands["codex"] = [sys.executable, str(script)]
    service.agent_profiles = AgentProfileRegistry(["codex"])
    session = service.agent_session_create()

    original_start = service._start_managed_process_prepared
    entered = threading.Event()
    release = threading.Event()

    def delayed_start(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_start(*args, **kwargs)

    monkeypatch.setattr(service, "_start_managed_process_prepared", delayed_start)
    outcomes: list[object] = []

    def start(prompt: str) -> None:
        try:
            outcomes.append(service.agent_run_start(session["session_id"], prompt))
        except Exception as exc:  # the second caller must receive the active-run error
            outcomes.append(exc)

    first = threading.Thread(target=start, args=("first",))
    second = threading.Thread(target=start, args=("second",))
    first.start()
    assert entered.wait(5)
    second.start()
    time.sleep(0.1)
    assert second.is_alive()
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    started = next(item for item in outcomes if isinstance(item, dict))
    error = next(item for item in outcomes if isinstance(item, Exception))
    assert isinstance(error, RuntimeError)
    assert "one active run" in str(error)
    service.agent_run_cancel(session["session_id"], started["run_id"])


def _fake_agent_service(workspace, tmp_path, policy):
    script = tmp_path / "fake_policy_agent.py"
    script.write_text(
        "import os, sys\n"
        "native_id = sys.argv[1]\n"
        "if native_id == 'new':\n"
        "    native_id = 'native_policy_1'\n"
        "print('SESSION:' + native_id, flush=True)\n"
        "print('MESSAGE:' + os.getcwd(), flush=True)\n",
        encoding="utf-8",
    )
    service = TianChengService(
        workspace, tmp_path / "audit", allow_exec=True, access_policy=policy
    )
    service._exec_commands["fake-agent"] = [sys.executable, str(script)]
    service.agent_profiles = AgentProfileRegistry(
        service._exec_commands, adapters=(_FakeAdapter(),)
    )
    return service


def test_agent_runs_inside_a_whitelisted_directory(workspace, tmp_path) -> None:
    outside = tmp_path / "external-project"
    outside.mkdir()
    policy = AccessPolicy(
        workspace,
        [
            AccessRule(path=Path(workspace), mode="full"),
            AccessRule(path=outside, mode="write"),
        ],
    )
    service = _fake_agent_service(workspace, tmp_path, policy)
    try:
        session = service.agent_session_create(
            profile="fake-default", cwd=str(outside), sandbox="workspace-write"
        )
        assert session["cwd_scope"] == "access-policy"
        assert Path(session["cwd_policy_root"]) == outside.resolve()

        started = service.agent_run_start(session["session_id"], "go")
        completed = _wait_for_run(service, session["session_id"], started["run_id"])
        assert completed["state"] == "succeeded"
        # The fake agent echoes its own cwd, proving the process really ran
        # outside the workspace rather than being silently redirected.
        result = service.agent_run_result(session["session_id"], started["run_id"])
        assert Path(result["result"]).resolve() == outside.resolve()
    finally:
        service.shutdown()


def test_agent_cwd_is_reauthorized_when_the_policy_is_reloaded(
    workspace, tmp_path
) -> None:
    outside = tmp_path / "external-project"
    outside.mkdir()
    allowed = AccessPolicy(
        workspace,
        [
            AccessRule(path=Path(workspace), mode="full"),
            AccessRule(path=outside, mode="write"),
        ],
    )
    service = _fake_agent_service(workspace, tmp_path, allowed)
    try:
        session = service.agent_session_create(
            profile="fake-default", cwd=str(outside), sandbox="workspace-write"
        )
        first = service.agent_run_start(session["session_id"], "before")
        assert _wait_for_run(service, session["session_id"], first["run_id"])[
            "state"
        ] == "succeeded"

        # Hot reload revokes the rule; the already-open session must not keep
        # working outside the workspace on its next run.
        service.access_policy = AccessPolicy(
            workspace, [AccessRule(path=Path(workspace), mode="full")]
        )
        with pytest.raises(PermissionError):
            service.agent_run_start(session["session_id"], "after")
    finally:
        service.shutdown()


def test_browse_rule_cannot_host_an_agent(workspace, tmp_path) -> None:
    outside = tmp_path / "browse-only"
    outside.mkdir()
    policy = AccessPolicy(
        workspace,
        [
            AccessRule(path=Path(workspace), mode="full"),
            AccessRule(path=outside, mode="browse"),
        ],
    )
    service = _fake_agent_service(workspace, tmp_path, policy)
    try:
        with pytest.raises(PermissionError):
            service.agent_session_create(
                profile="fake-default", cwd=str(outside), sandbox="read-only"
            )
    finally:
        service.shutdown()


def test_attach_resumes_history_from_a_whitelisted_directory(
    workspace, tmp_path
) -> None:
    """A conversation that ran outside TianCheng becomes attachable once the
    access policy covers its directory, and stops being attachable when the
    rule is withdrawn."""

    project = tmp_path / "external-project"
    project.mkdir()
    codex_root = tmp_path / ".codex" / "sessions"
    rollout = codex_root / "2026" / "08" / "30" / "rollout-whitelisted.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "thr_whitelisted", "cwd": str(project)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    source_policy = AgentSourcePolicy.from_payload(
        {
            "schema_version": 1,
            "sources": [
                {
                    "source_id": "src_codex_wl",
                    "provider": "codex",
                    "root": str(codex_root),
                    "mode": "catalog-read",
                }
            ],
        }
    )
    service = TianChengService(
        workspace,
        tmp_path / "audit",
        allow_exec=True,
        agent_source_policy=source_policy,
        agent_catalog_path=tmp_path / "state" / "catalog.sqlite3",
        access_policy=AccessPolicy(
            workspace,
            [
                AccessRule(path=Path(workspace), mode="full"),
                AccessRule(path=project, mode="write"),
            ],
        ),
    )
    try:
        service._exec_commands["codex"] = [sys.executable, "unused.py"]
        service.agent_profiles = AgentProfileRegistry(["codex"])
        service.agent_catalog_refresh("src_codex_wl")
        record = service.agent_catalog_list(provider="codex")["conversations"][0]
        assert record["cwd_scope"] == "access-policy"
        assert record["attachable"] is True
        # The absolute path must not travel to the caller.
        assert "cwd_absolute" not in record
        assert str(project) not in json.dumps(record, ensure_ascii=False)

        session = service.agent_session_attach(
            record["conversation_ref"], sandbox="workspace-write"
        )
        assert session["cwd_scope"] == "access-policy"
        assert session["native_session_id"] == "thr_whitelisted"

        # Withdrawing the rule must make the same conversation unusable.
        service.access_policy = AccessPolicy(
            workspace, [AccessRule(path=Path(workspace), mode="full")]
        )
        withdrawn = service.agent_catalog_list(provider="codex")["conversations"][0]
        assert withdrawn["attachable"] is False
        with pytest.raises(PermissionError):
            service.agent_session_attach(record["conversation_ref"])
    finally:
        service.shutdown()
