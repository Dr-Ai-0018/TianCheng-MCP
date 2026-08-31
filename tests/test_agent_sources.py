from __future__ import annotations

import json
from pathlib import Path

import pytest

from tiancheng_mcp import agent_sources
from tiancheng_mcp.agent_sources import AgentSourcePolicy, AgentSourcePolicyError
from tiancheng_mcp.security import WorkspaceSecurityError


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    codex = tmp_path / ".codex" / "sessions"
    claude = tmp_path / ".claude" / "projects"
    codex.mkdir(parents=True)
    claude.mkdir(parents=True)
    return codex, claude


def _payload(codex: Path, claude: Path | None = None) -> dict[str, object]:
    sources: list[dict[str, object]] = [
        {
            "source_id": "src_codex_local",
            "provider": "codex",
            "root": str(codex),
            "enabled": True,
            "mode": "catalog-read",
        }
    ]
    if claude is not None:
        sources.append(
            {
                "source_id": "src_claude_local",
                "provider": "claude-code",
                "root": str(claude),
                "enabled": False,
                "mode": "catalog-read",
                "max_files": 500,
            }
        )
    return {"schema_version": 1, "sources": sources}


def test_agent_source_policy_loads_strict_provider_roots_without_exposing_paths(
    tmp_path: Path,
) -> None:
    codex, claude = _roots(tmp_path)
    config = tmp_path / "agent-sources.json"
    config.write_text(
        "\ufeff" + json.dumps(_payload(codex, claude), ensure_ascii=False),
        encoding="utf-8",
    )
    policy = AgentSourcePolicy.load(config)

    assert policy.summary() == {
        "schema_version": 1,
        "source_count": 2,
        "enabled_source_count": 1,
        "providers": ["claude-code", "codex"],
    }
    summaries = policy.summaries()
    assert summaries[0]["source_id"] == "src_codex_local"
    assert "root" not in summaries[0]
    assert str(tmp_path) not in json.dumps(summaries)
    assert policy.get("src_codex_local").root == codex.resolve()
    with pytest.raises(PermissionError, match="disabled"):
        policy.get("src_claude_local")
    assert policy.get("src_claude_local", require_enabled=False).enabled is False


def test_agent_source_policy_absent_file_is_empty(tmp_path: Path) -> None:
    policy = AgentSourcePolicy.load(tmp_path / "missing-agent-sources.json")
    assert policy.sources == ()
    assert policy.summary()["source_count"] == 0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update({"command": "codex"}), "unknown fields"),
        (
            lambda payload: payload["sources"][0].update({"env": {"KEY": "value"}}),
            "unknown fields",
        ),
        (
            lambda payload: payload["sources"][0].update({"mode": "read"}),
            "catalog-read",
        ),
        (
            lambda payload: payload["sources"][0].update({"provider": "unknown"}),
            "provider",
        ),
        (
            lambda payload: payload["sources"][0].update({"max_files": 0}),
            "max_files",
        ),
    ],
)
def test_agent_source_policy_rejects_unknown_or_unsafe_fields(
    tmp_path: Path, mutation, message: str
) -> None:
    codex, _ = _roots(tmp_path)
    payload = _payload(codex)
    mutation(payload)
    with pytest.raises(AgentSourcePolicyError, match=message):
        AgentSourcePolicy.from_payload(payload)


def test_agent_source_policy_rejects_broad_or_sensitive_roots(tmp_path: Path) -> None:
    codex, _ = _roots(tmp_path)
    broad = _payload(codex)
    broad["sources"][0]["root"] = str(codex.parent)
    with pytest.raises(AgentSourcePolicyError, match="sessions directory"):
        AgentSourcePolicy.from_payload(broad)

    claude_sessions = tmp_path / ".claude-other" / "sessions"
    claude_sessions.mkdir(parents=True)
    wrong_claude = _payload(codex)
    wrong_claude["sources"][0].update(
        {"provider": "claude-code", "root": str(claude_sessions)}
    )
    with pytest.raises(AgentSourcePolicyError, match="projects directory"):
        AgentSourcePolicy.from_payload(wrong_claude)

    sensitive = tmp_path / "cache" / "sessions"
    sensitive.mkdir(parents=True)
    secret_root = _payload(sensitive)
    with pytest.raises(AgentSourcePolicyError, match="sensitive component"):
        AgentSourcePolicy.from_payload(secret_root)


def test_agent_source_policy_rejects_duplicates_and_reparse_roots(
    tmp_path: Path, monkeypatch
) -> None:
    codex, _ = _roots(tmp_path)
    duplicate = _payload(codex)
    duplicate["sources"].append(dict(duplicate["sources"][0]))
    with pytest.raises(AgentSourcePolicyError, match="Duplicate agent source_id"):
        AgentSourcePolicy.from_payload(duplicate)

    original = agent_sources._is_reparse

    def fake_reparse(path: Path) -> bool:
        return path == codex or original(path)

    monkeypatch.setattr(agent_sources, "_is_reparse", fake_reparse)
    with pytest.raises(AgentSourcePolicyError, match="reparse"):
        AgentSourcePolicy.from_payload(_payload(codex))


def test_agent_source_jail_rejects_escape(tmp_path: Path) -> None:
    codex, _ = _roots(tmp_path)
    source = AgentSourcePolicy.from_payload(_payload(codex)).get("src_codex_local")
    with pytest.raises(WorkspaceSecurityError):
        source.jail().resolve("..\\auth.json", must_exist=False)


def test_agent_source_policy_atomic_save_keeps_backup_and_acl_hook(
    tmp_path: Path,
) -> None:
    codex, claude = _roots(tmp_path)
    config = tmp_path / "config" / "agent-sources.json"
    hardened: list[Path] = []
    first = AgentSourcePolicy.from_payload(_payload(codex))
    result = first.save_atomic(config, acl_hardener=hardened.append)
    assert result["backup_created"] is False
    assert AgentSourcePolicy.load(config).summary()["source_count"] == 1

    first_bytes = config.read_bytes()
    second = AgentSourcePolicy.from_payload(_payload(codex, claude))
    result = second.save_atomic(config, acl_hardener=hardened.append)
    assert result["backup_created"] is True
    assert config.with_name("agent-sources.json.bak").read_bytes() == first_bytes
    assert AgentSourcePolicy.load(config).summary()["source_count"] == 2
    assert config in hardened
    assert config.with_name("agent-sources.json.bak") in hardened
    assert not list(config.parent.glob("*.tmp"))


def test_agent_source_policy_acl_failure_rolls_back_atomically(tmp_path: Path) -> None:
    codex, claude = _roots(tmp_path)
    config = tmp_path / "config" / "agent-sources.json"

    def fail_acl(_path: Path) -> None:
        raise PermissionError("fixture ACL failure")

    first = AgentSourcePolicy.from_payload(_payload(codex))
    with pytest.raises(AgentSourcePolicyError, match="previous policy restored"):
        first.save_atomic(config, acl_hardener=fail_acl)
    assert not config.exists()

    first.save_atomic(config)
    original = config.read_bytes()
    second = AgentSourcePolicy.from_payload(_payload(codex, claude))
    with pytest.raises(AgentSourcePolicyError, match="previous policy restored"):
        second.save_atomic(config, acl_hardener=fail_acl)
    assert config.read_bytes() == original
    assert AgentSourcePolicy.load(config).summary()["source_count"] == 1
