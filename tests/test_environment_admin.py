from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from tiancheng_mcp.environment_admin import edit, explain
from tiancheng_mcp.execution_environment import EnvironmentPolicy
from tiancheng_mcp.service import TianChengService


def config(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/launcher.defaults.json").write_text(json.dumps({
        "executionEnvironment": {"mode": "basic", "allow": [], "deny": []},
        "envFile": "selected.env", "workspace": "workspace"}))
    (tmp_path / "workspace").mkdir()
    return tmp_path / "config/local.json"


def test_explanation_preserves_legacy_and_never_exposes_values(tmp_path, monkeypatch):
    local = config(tmp_path)
    allowlist = tmp_path / "exec-env.allowlist"
    allowlist.write_text("# unchanged\nLEGACY_KEY\n")
    monkeypatch.delenv("LEGACY_KEY", raising=False)
    (tmp_path / "selected.env").write_text("LEGACY_KEY=synthetic-private-secret\nOTHER=other-secret\n")
    state = explain(tmp_path, local)
    assert state["missing"] == ["LEGACY_KEY"]
    assert state["dotenv_names"] == ["LEGACY_KEY"]
    assert state["runtime_mode"] == "unknown" and state["pending_restart"] is None
    assert "synthetic-private-secret" not in json.dumps(state) and "other-secret" not in json.dumps(state)
    assert not local.exists() and allowlist.read_text() == "# unchanged\nLEGACY_KEY\n"


def test_transactional_edits_keep_other_fields_and_backup(tmp_path):
    local = config(tmp_path)
    original = '{"customSetting": true, "executionEnvironment":{"allow":["FIRST"]}}'
    local.write_text(original)
    state = edit(tmp_path, local, "mode", "inherit")
    assert state["mode"] == "inherit" and state["configured"]["allow"] == ["FIRST"]
    assert json.loads(local.read_text())["customSetting"] is True
    assert Path(str(local) + ".environment.bak").read_text() == original
    previous = local.read_bytes()
    with pytest.raises((ValueError, PermissionError)):
        edit(tmp_path, local, "allow", "CONTROL_PLANE_API_KEY")
    assert local.read_bytes() == previous
    assert edit(tmp_path, local, "deny", "FIRST")["denied_allow"] == ["FIRST"]
    assert edit(tmp_path, local, "reset")["configured"] == {"mode": "basic", "allow": [], "deny": []}


def test_admin_failure_does_not_echo_bad_payload(tmp_path):
    local = config(tmp_path)
    local.write_text('synthetic-secret-token invalid json')
    result = subprocess.run([sys.executable, "-m", "tiancheng_mcp.environment_admin",
        "--project-root", str(tmp_path), "--local", str(local), "status"],
        capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 1
    assert "synthetic-secret-token" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["error"] == "JSONDecodeError"


def test_runtime_snapshot_is_active_and_value_free(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("CUSTOM", "synthetic-private-secret")
    service = TianChengService(workspace, tmp_path / "audit", execution_environment={"allow": ["CUSTOM"]})
    try:
        state = service.workspace_info()["execution_environment"]
        assert state["configuration_role"] == "active" and state["loaded_at"]
        assert state["policy_revision"] == EnvironmentPolicy.parse({"allow": ["CUSTOM"]}).revision
        assert "synthetic-private-secret" not in json.dumps(state)
        assert service.workspace_info()["explicit_env_passthrough_enabled"] is True
    finally:
        service.shutdown()


def test_unavailable_profile_credential_still_filtered(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(TianChengService, "_discover_exec_commands", lambda self: {})
    monkeypatch.setattr(TianChengService, "_discover_agent_only_commands", lambda self: {})
    monkeypatch.setenv("UNAVAILABLE_PROFILE_KEY", "synthetic-hidden-key")
    profile = tmp_path / "profiles.json"
    profile.write_text(json.dumps({"version": 2, "inherit_defaults": False, "profiles": [{
        "name": "not-installed", "provider": "codex", "provider_profile": "example",
        "auth": {"mode": "env", "credential_env": "UNAVAILABLE_PROFILE_KEY"}}]}))
    service = TianChengService(workspace, None, agent_profile_config_path=profile,
        execution_environment={"mode": "inherit"})
    try:
        assert not service.agent_profiles.names()
        assert "UNAVAILABLE_PROFILE_KEY" not in service._execution_environment()
    finally:
        service.shutdown()


def test_optional_dotenv_access_failure_is_unknown_not_configuration_failure(tmp_path, monkeypatch):
    local = config(tmp_path)
    env_file = tmp_path / "selected.env"
    env_file.write_text("PRIVATE_KEY=synthetic-secret")
    original = Path.read_text
    def guarded(path, *args, **kwargs):
        if path == env_file:
            raise PermissionError("private file")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", guarded)
    state = explain(tmp_path, local)
    assert state["dotenv_status"] == "unavailable" and state["dotenv_names"] == []
