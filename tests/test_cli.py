from __future__ import annotations

import os
from pathlib import Path

import pytest

from tiancheng_mcp import cli


def test_stdio_keyboard_interrupt_is_a_clean_shutdown(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    class FakeService:
        def __init__(self, *args, **kwargs) -> None:
            self.kwargs = kwargs
            self.stopped = False

        def stop_all_processes(self) -> None:
            self.stopped = True

    class FakeServer:
        def run(self, *, transport: str) -> None:
            assert transport == "stdio"
            raise KeyboardInterrupt

    service_holder: list[FakeService] = []

    def make_service(*args, **kwargs):
        service = FakeService(*args, **kwargs)
        service_holder.append(service)
        return service

    monkeypatch.setattr(cli, "TianChengService", make_service)
    monkeypatch.setattr(cli, "create_server", lambda service: FakeServer())
    cli.main(
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "--audit-dir",
            str(tmp_path / "audit"),
            "--pass-env",
            "EXAMPLE_AGENT_KEY",
        ]
    )
    assert service_holder and service_holder[0].stopped is True
    assert service_holder[0].kwargs["passthrough_env"] == ["EXAMPLE_AGENT_KEY"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX XDG directory defaults")
def test_posix_defaults_use_private_xdg_directories(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    args = cli.build_parser().parse_args(["--workspace", str(tmp_path / "workspace")])
    config = tmp_path / "config-home" / "tiancheng-mcp"
    state = tmp_path / "state-home" / "tiancheng-mcp"
    assert args.access_policy == str(config / "access-policy.json")
    assert args.agent_sources == str(config / "agent-sources.json")
    assert args.agent_catalog == str(state / "agent-catalog.sqlite3")
    assert args.audit_dir == str(state / "logs")
    cli._ensure_private_directory(config)
    cli._ensure_private_directory(state)
    assert config.stat().st_mode & 0o077 == 0
    assert state.stat().st_mode & 0o077 == 0
    config.chmod(0o755)
    with pytest.raises(PermissionError, match="0700"):
        cli._ensure_private_directory(config)


@pytest.mark.skipif(os.name == "nt", reason="POSIX XDG directory defaults")
def test_posix_default_config_is_not_created_inside_workspace(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(workspace))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    with pytest.raises(ValueError, match="outside the workspace"):
        cli.main(["--workspace", str(workspace)])
    assert not (workspace / "tiancheng-mcp").exists()
