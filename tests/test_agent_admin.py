from __future__ import annotations

import json
from pathlib import Path

import pytest

from tiancheng_mcp.agent_admin import (
    AgentSourceAdminError,
    add_source,
    discover_local_agents,
    rebuild_catalog,
    refresh_source,
    remove_source,
    run_agent_smoke,
    set_source_enabled,
    source_status,
)
from tiancheng_mcp.agent_sources import AgentSourcePolicy


def _write_codex(path: Path, native_id: str, workspace: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "timestamp": "2026-08-30T12:00:00Z",
                "payload": {
                    "id": native_id,
                    "cwd": str(workspace),
                    "timestamp": "2026-08-30T12:00:00Z",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_agent_admin_discovery_checks_only_fixed_provider_roots(tmp_path: Path) -> None:
    codex = tmp_path / "codex-home" / "sessions"
    claude = tmp_path / "claude-home" / "projects"
    codex.mkdir(parents=True)
    claude.mkdir(parents=True)
    unrelated = tmp_path / "other" / "sessions"
    unrelated.mkdir(parents=True)

    result = discover_local_agents(
        home=tmp_path / "unused-home",
        environment={
            "CODEX_HOME": str(codex.parent),
            "CLAUDE_CONFIG_DIR": str(claude.parent),
        },
        probe_versions=False,
    )

    providers = {item["provider"]: item for item in result["providers"]}
    assert providers["codex"]["suggested_root"] == str(codex)
    assert providers["claude-code"]["suggested_root"] == str(claude)
    assert all(item["source_exists"] for item in providers.values())
    assert str(unrelated) not in json.dumps(result)


def test_agent_admin_mutations_use_validated_atomic_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex = tmp_path / ".codex" / "sessions"
    claude = tmp_path / ".claude" / "projects"
    codex.mkdir(parents=True)
    claude.mkdir(parents=True)
    config = tmp_path / "config" / "agent-sources.json"
    catalog = tmp_path / "state" / "agent-catalog.sqlite3"
    hardened: list[Path] = []

    add_source(
        config,
        source_id="src_codex_local",
        provider="codex",
        root=codex,
        acl_hardener=hardened.append,
    )
    add_source(
        config,
        source_id="src_claude_local",
        provider="claude-code",
        root=claude,
        acl_hardener=hardened.append,
    )
    set_source_enabled(
        config,
        source_id="src_claude_local",
        enabled=False,
        acl_hardener=hardened.append,
    )

    status = source_status(config, catalog, workspace)
    assert status["source_count"] == 2
    assert status["enabled_source_count"] == 1
    assert {item["source_id"] for item in status["sources"]} == {
        "src_codex_local",
        "src_claude_local",
    }
    assert all(item["mode"] == "catalog-read" for item in status["sources"])
    assert config in hardened
    assert config.with_name("agent-sources.json.bak") in hardened

    remove_source(
        config,
        source_id="src_claude_local",
        acl_hardener=hardened.append,
    )
    assert AgentSourcePolicy.load(config).summary()["source_count"] == 1


def test_agent_admin_refresh_and_rebuild_keep_catalog_backup(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex = tmp_path / ".codex" / "sessions"
    rollout = codex / "2026" / "08" / "30" / "rollout-admin.jsonl"
    _write_codex(rollout, "codex_admin_1", workspace)
    config = tmp_path / "config" / "agent-sources.json"
    catalog = tmp_path / "state" / "agent-catalog.sqlite3"
    add_source(
        config,
        source_id="src_codex_local",
        provider="codex",
        root=codex,
    )

    refreshed = refresh_source(config, catalog, workspace, "src_codex_local")
    assert refreshed["parsed_files"] == 1
    assert catalog.exists()

    rebuilt = rebuild_catalog(config, catalog, workspace)
    assert rebuilt["rebuilt"] is True
    assert len(rebuilt["backup_files"]) >= 1
    assert all(Path(path).exists() for path in rebuilt["backup_files"])
    assert rebuilt["refreshes"][0]["parsed_files"] == 1
    assert source_status(config, catalog, workspace)["sources"][0][
        "record_status_counts"
    ] == {"ready": 1}


def test_agent_admin_smoke_uses_fixed_read_only_request(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[object, ...]] = []

    class FakeService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append(("init", *args, kwargs))

        def agent_session_create(self, **kwargs: object) -> dict[str, str]:
            calls.append(("create", kwargs))
            return {"session_id": "sess_fixture", "provider": "claude-code"}

        def agent_run_start(self, session_id: str, prompt: str) -> dict[str, str]:
            calls.append(("start", session_id, prompt))
            return {"run_id": "run_fixture"}

        def agent_run_inspect(self, session_id: str, run_id: str) -> dict[str, str]:
            calls.append(("inspect", session_id, run_id))
            return {"state": "succeeded"}

        def agent_run_result(self, session_id: str, run_id: str) -> dict[str, str]:
            return {"state": "succeeded", "result": "TIANCHENG_SMOKE_OK"}

        def agent_session_close(self, session_id: str) -> None:
            calls.append(("close", session_id))

        def shutdown(self) -> None:
            calls.append(("shutdown",))

    result = run_agent_smoke(
        workspace,
        profile="claude-default",
        passthrough_env=("EXAMPLE_SERVICE_KEY",),
        service_factory=FakeService,
    )

    assert result["marker_verified"] is True
    create = next(call for call in calls if call[0] == "create")
    assert create[1] == {
        "profile": "claude-default",
        "cwd": ".",
        "sandbox": "read-only",
    }
    start = next(call for call in calls if call[0] == "start")
    assert start[1] == "sess_fixture"
    assert start[2] == (
        "Do not use any tools. Reply with exactly TIANCHENG_SMOKE_OK and nothing else."
    )
    init = next(call for call in calls if call[0] == "init")
    assert init[-1]["allow_exec"] is True
    assert init[-1]["enable_agent_catalog"] is False
    assert init[-1]["enable_jobs"] is False
    assert calls[-1] == ("shutdown",)


def test_agent_admin_smoke_reports_redacted_runtime_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class FailedService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def agent_session_create(self, **kwargs: object) -> dict[str, str]:
            return {"session_id": "sess_fixture", "provider": "claude-code"}

        def agent_run_start(self, session_id: str, prompt: str) -> dict[str, str]:
            return {"run_id": "run_fixture"}

        def agent_run_inspect(self, session_id: str, run_id: str) -> dict[str, str]:
            return {"state": "failed"}

        def agent_run_result(self, session_id: str, run_id: str) -> dict[str, str]:
            return {
                "state": "failed",
                "error": "token=do-not-leak Input must be provided through stdin",
            }

        def agent_session_close(self, session_id: str) -> None:
            pass

        def shutdown(self) -> None:
            pass

    with pytest.raises(AgentSourceAdminError) as raised:
        run_agent_smoke(
            workspace,
            profile="claude-default",
            service_factory=FailedService,
        )

    message = str(raised.value)
    assert "Input must be provided through stdin" in message
    assert "do-not-leak" not in message
    assert "<redacted>" in message
