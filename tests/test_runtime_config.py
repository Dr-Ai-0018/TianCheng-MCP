from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from tiancheng_mcp import cli
from tiancheng_mcp.policy import AccessPolicy, AccessRule
from tiancheng_mcp.runtime_config import launcher_config

ROOT = Path(__file__).resolve().parents[1]


def test_configuration_resolves_relative_paths_and_environment(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/launcher.defaults.json").write_text(json.dumps({
        "workspace": "default", "agentSourcesPath": "config/sources.json",
        "python": "venv/python", "interactiveTimeoutSeconds": 75,
    }), encoding="utf-8")
    local = tmp_path / "selected.json"
    local.write_text(json.dumps({"workspace": "local", "auditDir": "audit", "interactiveTimeoutSeconds": 42}), encoding="utf-8-sig")
    config = launcher_config(tmp_path, local, {"TIANCHENG_WORKSPACE": str(tmp_path / "environment")})
    assert config["workspace"] == str(tmp_path / "environment")
    assert config["agentSourcesPath"] == str(tmp_path / "config/sources.json")
    assert config["python"] == str(tmp_path / "venv/python")
    assert config["auditDir"] == str(tmp_path / "audit")
    assert config["interactiveTimeoutSeconds"] == 42


def test_configuration_does_not_dereference_workspace_link(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    local = tmp_path / "config.json"
    local.write_text(json.dumps({"workspace": "link"}), encoding="utf-8")
    assert launcher_config(tmp_path, local, {})["workspace"] == str(link)


@pytest.mark.parametrize("payload", [[], {"workspace": 42}, {"interactiveTimeoutSeconds": True}, {"interactiveTimeoutSeconds": 91}])
def test_bad_configuration_fails_instead_of_silently_falling_back(tmp_path, payload):
    local = tmp_path / "invalid.json"
    local.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        launcher_config(tmp_path, local, {})


def test_cli_explicit_options_win_without_implicit_capability_enable(tmp_path, monkeypatch):
    local = tmp_path / "selected.json"
    local.write_text(json.dumps({"workspace": str(tmp_path / "configured"),
        "allow_exec": True, "allowExternalGrants": True, "interactiveTimeoutSeconds": 42}), encoding="utf-8")
    captured = {}
    class FakeService:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        def shutdown(self):
            pass
    class FakeServer:
        def run(self, **kwargs):
            pass
    monkeypatch.setattr(cli, "TianChengService", FakeService)
    monkeypatch.setattr(cli, "create_server", lambda service: FakeServer())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("TIANCHENG_WORKSPACE", str(tmp_path / "environment"))
    cli.main(["--runtime-config", str(local), "--runtime-project-root", str(tmp_path),
        "--workspace", str(tmp_path / "explicit"), "--interactive-timeout-seconds", "33"])
    assert captured["workspace"] == str(tmp_path / "explicit")
    assert captured["interactive_timeout_seconds"] == 33
    assert not captured["allow_exec"] and not captured["allow_external_grants"]


@pytest.mark.skipif(os.name != "nt", reason="Native Windows launcher chain")
@pytest.mark.parametrize("mode", ["tui-safe", "dev", "grants", "grants-dev"])
@pytest.mark.asyncio
async def test_native_launchers_use_selected_config_end_to_end(tmp_path, mode):
    pwsh = shutil.which("pwsh")
    assert pwsh, "Native launch verification requires PowerShell 7"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "blocked").mkdir()
    policy_path = tmp_path / "selected-policy.json"
    policy_path.write_text(json.dumps(AccessPolicy(workspace, [
        AccessRule(workspace, "full"), AccessRule(workspace / "blocked", "deny"),
    ]).to_payload()), encoding="utf-8")
    sources_root = tmp_path / ".codex/sessions"
    sources_root.mkdir(parents=True)
    sources_path = tmp_path / "selected-sources.json"
    sources_path.write_text(json.dumps({"schema_version": 1, "sources": [{
        "source_id": "src_selected", "provider": "codex", "root": str(sources_root),
        "mode": "catalog-read", "enabled": True,
    }]}), encoding="utf-8")
    catalog = tmp_path / "selected-catalog.sqlite3"
    env_path = tmp_path / "selected.env"
    env_path.write_text('EXAMPLE_AGENT_KEY=synthetic-selected\nUNSELECTED_TEST_KEY=synthetic-unselected\n', encoding="utf-8")
    config_path = tmp_path / "chosen launcher.json"
    config_path.write_text(json.dumps({
        "workspace": str(workspace), "python": sys.executable, "powerShell": pwsh,
        "accessPolicyPath": str(policy_path), "agentSourcesPath": str(sources_path),
        "agentCatalogPath": str(catalog), "agentProfilesPath": str(tmp_path / "profiles.json"),
        "envFile": str(env_path), "auditDir": str(tmp_path / "selected-audit"),
        "interactiveTimeoutSeconds": 42,
    }), encoding="utf-8")
    environment = {"PATH": os.environ.get("PATH", ""), "CONTROL_PLANE_API_KEY": "synthetic-parent-secret"}
    if mode == "tui-safe":
        info = subprocess.run([pwsh, "-NoProfile", "-File", str(ROOT / "tc.ps1"),
            "-Action", "info", "-Json", "-ConfigPath", str(config_path)],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        assert info.returncode == 0, info.stderr
        payload = json.loads(info.stdout)
        assert payload["workspace"] == str(workspace)
        assert payload["accessPolicyPath"] == str(policy_path)
        command = shlex.split(payload["mcpCommand"])
    else:
        script = "run-mcp-exec.ps1" if mode == "dev" else "run-mcp-grants.ps1"
        command = [pwsh, "-NoProfile", "-File", str(ROOT / script), "-ConfigPath", str(config_path)]
        if mode == "grants-dev":
            command.append("-AllowExec")
        if mode in {"dev", "grants-dev"}:
            command += ["-PassEnv", "EXAMPLE_AGENT_KEY"]
    params = StdioServerParameters(command=command[0], args=command[1:], env=environment,
        cwd=str(ROOT), encoding="utf-8")
    async with Client(params, mode="legacy", raise_exceptions=True) as client:
        result = await client.call_tool("workspace_info", {})
        assert not result.is_error
        info = result.structured_content
        assert info["workspace_root"] == str(workspace)
        assert info["interactive_timeout_seconds"] == 42
        assert any(rule["path"] == str(workspace / "blocked") and rule["mode"] == "deny"
            for rule in info["access_policy"]["rules"])
        assert info["agent_sources"]["source_count"] == 1
        listed = await client.call_tool("agent_catalog", {"action": "refresh", "source_id": "src_selected"})
        assert not listed.is_error
        assert catalog.exists()
        assert info["capabilities"]["command_execution"] == (mode in {"dev", "grants-dev"})
        assert info["capabilities"]["external_grants"] == mode.startswith("grants")
        if mode in {"dev", "grants-dev"}:
            result = await client.call_tool("run_command", {"command": "python", "args": ["-c",
                "import os,json; print(json.dumps([os.getenv('EXAMPLE_AGENT_KEY'),os.getenv('UNSELECTED_TEST_KEY'),os.getenv('CONTROL_PLANE_API_KEY')]))"]})
            assert not result.is_error
            assert json.loads(result.structured_content["stdout"]) == ["synthetic-selected", None, None]
    assert list((tmp_path / "selected-audit").glob("*"))
