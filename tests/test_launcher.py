from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(shutil.which("pwsh") or "pwsh")

pytestmark = pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="PowerShell 7 (pwsh) is unavailable"
)

# Some launcher paths only exercise the config surface, so a stub is enough.
# The ones that make the tunnel actually write a profile need the real binary,
# and skip rather than pretend when it is not installed.
REAL_TUNNEL_CLIENT = shutil.which("tunnel-client")
requires_tunnel_client = pytest.mark.skipif(
    REAL_TUNNEL_CLIENT is None,
    reason="tunnel-client is not on PATH; profile creation cannot be exercised",
)


def fake_tunnel_client(directory: Path) -> Path:
    """Create a stand-in tunnel-client for launcher fixtures.

    These tests drive the launcher, not the tunnel, so the stub only has to
    answer "profiles list" with an empty list. Requiring the real binary would
    tie the suite to one machine's install.
    """

    if REAL_TUNNEL_CLIENT:
        return Path(REAL_TUNNEL_CLIENT)
    path = directory / "tunnel-client.cmd"
    script = "\r\n".join(
        (
            "@echo off",
            "if \"%~1\"==\"profiles\" (echo [])",
            "exit /b 0",
            "",
        )
    )
    path.write_text(script, encoding="ascii")
    return path


def run_powershell(
    script: Path,
    *arguments: str,
    input_text: str | None = None,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        timeout=30,
        check=False,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )


def write_test_config(
    path: Path, *, env_file: Path, profile_dir: Path, workspace: Path | None = None
) -> None:
    workspace = workspace if workspace is not None else path.parent / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    tunnel_client = fake_tunnel_client(path.parent)
    path.write_text(
        json.dumps(
            {
                "tunnelClient": str(tunnel_client),
                "profileDir": str(profile_dir),
                "envFile": str(env_file),
                "defaultProfile": "tiancheng-local",
                "agentSourcesPath": str(path.with_name("agent-sources.json")),
                "agentCatalogPath": str(path.with_name("agent-catalog.sqlite3")),
                # The workspace has no built-in default, so every fixture names
                # its own directory instead of leaning on one machine's layout.
                "workspace": str(workspace),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_launcher_info_and_process_key_status_never_print_secret(tmp_path: Path) -> None:
    config = tmp_path / "launcher.json"
    write_test_config(config, env_file=tmp_path / ".env", profile_dir=tmp_path / "profiles")

    info = run_powershell(
        PROJECT_ROOT / "tc.ps1", "-Action", "info", "-Json", "-ConfigPath", str(config)
    )
    assert info.returncode == 0, info.stderr
    payload = json.loads(info.stdout)
    assert payload["tunnelClientExists"] is True
    assert payload["mcpScriptExists"] is True

    secret = "mock-control-plane-key-never-print"
    environment = os.environ.copy()
    environment["CONTROL_PLANE_API_KEY"] = secret
    status = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "key-status",
        "-Json",
        "-ConfigPath",
        str(config),
        environment=environment,
    )
    assert status.returncode == 0, status.stderr
    assert secret not in status.stdout + status.stderr
    assert json.loads(status.stdout) == {
        "configured": True,
        "source": "process environment",
    }


def test_launcher_loads_dotenv_without_printing_value(tmp_path: Path) -> None:
    config = tmp_path / "launcher.json"
    env_file = tmp_path / ".env"
    secret = "mock-dotenv-key-never-print"
    env_file.write_text(f"CONTROL_PLANE_API_KEY={secret}\n", encoding="utf-8")
    write_test_config(config, env_file=env_file, profile_dir=tmp_path / "profiles")
    environment = os.environ.copy()
    environment.pop("CONTROL_PLANE_API_KEY", None)

    result = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "key-status",
        "-Json",
        "-NoUserEnvironment",
        "-ConfigPath",
        str(config),
        environment=environment,
    )
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr
    assert json.loads(result.stdout) == {"configured": True, "source": ".env file"}


@requires_tunnel_client
def test_launcher_creates_safe_stdio_profile_in_isolated_directory(tmp_path: Path) -> None:
    config = tmp_path / "launcher.json"
    profile_dir = tmp_path / "profiles"
    write_test_config(config, env_file=tmp_path / ".env", profile_dir=profile_dir)

    created = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "configure-profile",
        "-ConfigPath",
        str(config),
        input_text=f"test-profile\ntunnel_{'a' * 32}\n\nn\n",
    )
    assert created.returncode == 0, created.stdout + created.stderr

    listed = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "profiles",
        "-Json",
        "-ConfigPath",
        str(config),
    )
    assert listed.returncode == 0, listed.stderr
    payload = json.loads(listed.stdout)
    assert payload["profiles"] == ["test-profile"]
    assert payload["defaultProfile"] == "test-profile"

    profile_text = "\n".join(
        path.read_text(encoding="utf-8") for path in profile_dir.rglob("*.yaml")
    )
    assert "run-mcp.ps1" in profile_text
    assert "run-mcp-exec.ps1" not in profile_text
    assert "env:CONTROL_PLANE_API_KEY" in profile_text

    modes = payload["profileModes"]
    assert modes["test-profile"] == "SAFE"

    dev = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "set-mode",
        "-Mode",
        "dev",
        "-Profile",
        "test-profile",
        "-AllowExecProfile",
        "-ConfigPath",
        str(config),
    )
    assert dev.returncode == 0, dev.stdout + dev.stderr
    profile_text = "\n".join(
        path.read_text(encoding="utf-8") for path in profile_dir.rglob("*.yaml")
    )
    assert "run-mcp-exec.ps1" in profile_text

    status = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "status",
        "-Json",
        "-Profile",
        "test-profile",
        "-ConfigPath",
        str(config),
    )
    assert status.returncode == 0, status.stdout + status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["selectedMode"] == "DEV"
    assert "CONTROL_PLANE_API_KEY" not in status.stdout

    safe = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "set-mode",
        "-Mode",
        "safe",
        "-Profile",
        "test-profile",
        "-ConfigPath",
        str(config),
    )
    assert safe.returncode == 0, safe.stdout + safe.stderr
    profile_text = "\n".join(
        path.read_text(encoding="utf-8") for path in profile_dir.rglob("*.yaml")
    )
    assert "run-mcp.ps1" in profile_text
    assert "run-mcp-exec.ps1" not in profile_text


def test_alias_installer_is_idempotent_and_preserves_profile(tmp_path: Path) -> None:
    profile = tmp_path / "Microsoft.PowerShell_profile.ps1"
    profile.write_text("# existing profile content\n", encoding="utf-8")
    for _ in range(2):
        result = run_powershell(
            PROJECT_ROOT / "install-tc.ps1",
            "-ProfilePath",
            str(profile),
            "-PassThru",
        )
        assert result.returncode == 0, result.stderr

    text = profile.read_text(encoding="utf-8")
    assert "# existing profile content" in text
    assert text.count("# >>> tiancheng-mcp tc >>>") == 1
    assert "function global:tc" in text
    assert str(PROJECT_ROOT / "tc.ps1") in text


def test_settings_menu_persists_interactive_timeout_without_cli_flags(tmp_path: Path) -> None:
    config = tmp_path / "launcher.json"
    write_test_config(config, env_file=tmp_path / ".env", profile_dir=tmp_path / "profiles")
    result = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "settings",
        "-ConfigPath",
        str(config),
        "-NoPause",
        input_text="\n\n\n\n82\n\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["interactiveTimeoutSeconds"] == 82


def test_launcher_agent_status_is_local_only_and_does_not_create_policy(
    tmp_path: Path,
) -> None:
    config = tmp_path / "launcher.json"
    write_test_config(config, env_file=tmp_path / ".env", profile_dir=tmp_path / "profiles")
    source_policy = tmp_path / "agent-sources.json"

    result = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "agents",
        "-Json",
        "-ConfigPath",
        str(config),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert {item["provider"] for item in payload["providers"]} == {
        "codex",
        "claude-code",
    }
    assert payload["sources"] == []
    assert not source_policy.exists()
    lowered = (result.stdout + result.stderr).casefold()
    assert "control_plane_api_key" not in lowered
    assert "example_service_key" not in lowered


def test_launcher_agent_menu_adds_only_confirmed_catalog_source(
    tmp_path: Path,
) -> None:
    config = tmp_path / "launcher.json"
    write_test_config(config, env_file=tmp_path / ".env", profile_dir=tmp_path / "profiles")
    source_root = tmp_path / ".codex" / "sessions"
    source_root.mkdir(parents=True)
    source_policy = tmp_path / "agent-sources.json"

    result = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "agents",
        "-NoPause",
        "-ConfigPath",
        str(config),
        input_text=f"1\n1\nsrc_codex_test\n{source_root}\nADD\n0\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(source_policy.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["sources"] == [
        {
            "source_id": "src_codex_test",
            "provider": "codex",
            "root": str(source_root.resolve()),
            "enabled": True,
            "mode": "catalog-read",
            "max_files": 10_000,
            "max_file_bytes": 2 * 1024 * 1024,
            "max_scan_bytes": 64 * 1024 * 1024,
            "max_refresh_seconds": 60,
        }
    ]
    assert "不会把目录变成普通文件工具白名单" in result.stdout


@requires_tunnel_client
def test_launcher_reports_policy_hot_reload_mode(tmp_path: Path) -> None:
    """The status line must distinguish hot reload from a plain grants profile.

    Hot reload lets an approved chat request widen the access policy without a
    restart, so an operator has to be able to see it is on before walking away
    from the machine.
    """

    config = tmp_path / "launcher.json"
    profile_dir = tmp_path / "profiles"
    write_test_config(config, env_file=tmp_path / ".env", profile_dir=profile_dir)

    created = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "configure-profile",
        "-ConfigPath",
        str(config),
        input_text=f"hot-profile\ntunnel_{'b' * 32}\n\nn\n",
    )
    assert created.returncode == 0, created.stdout + created.stderr

    yaml_paths = list(profile_dir.rglob("*.yaml"))
    assert yaml_paths, "profile yaml was not created"
    for path in yaml_paths:
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(
                "run-mcp.ps1",
                "run-mcp-grants.ps1 -AllowExec -AllowPolicyHotReload",
            ),
            encoding="utf-8",
        )

    listed = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "profiles",
        "-Json",
        "-ConfigPath",
        str(config),
    )
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)["profileModes"]["hot-profile"] == "GRANTS+EXEC+HOT"

    for path in yaml_paths:
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(" -AllowPolicyHotReload", ""), encoding="utf-8")

    cold = run_powershell(
        PROJECT_ROOT / "tc.ps1",
        "-Action",
        "profiles",
        "-Json",
        "-ConfigPath",
        str(config),
    )
    assert cold.returncode == 0, cold.stderr
    assert json.loads(cold.stdout)["profileModes"]["hot-profile"] == "GRANTS+EXEC"
