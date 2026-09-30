from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != "posix", reason="Linux tunnel runtime launcher")
def test_linux_tunnel_runtime_keeps_key_out_of_arguments_and_output(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/linux_tunnel_runtime.py"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capture = tmp_path / "captured.json"
    client = tmp_path / "fake-client"
    client.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open({str(capture)!r}, 'w', encoding='utf-8') as f:\n"
        "    json.dump({'args': sys.argv[1:], 'key': os.environ.get('CONTROL_PLANE_API_KEY'), "
        "'xdg': os.environ.get('XDG_STATE_HOME')}, f)\n"
        "print(json.dumps({'ready': True, 'runtime_state': 'ready', 'log': 'private'}))\n",
        encoding="utf-8",
    )
    client.chmod(0o700)
    key_file = tmp_path / "tunnel.env"
    key_file.write_text("CONTROL_PLANE_API_KEY=synthetic-test-secret\n", encoding="utf-8")
    key_file.chmod(0o600)
    runtime_root = tmp_path / "runtime"
    config = {
        "client": str(client), "python": sys.executable,
        "runtime_root": str(runtime_root), "key_file": str(key_file),
        "tunnel_id": "tunnel_test123", "alias": "test", "profile": "test",
        "workspace": str(workspace), "audit_dir": str(tmp_path / "audit"),
        "access_policy": str(tmp_path / "access-policy.json"),
        "agent_sources": str(tmp_path / "agent-sources.json"),
        "agent_catalog": str(tmp_path / "catalog.sqlite3"),
        "agent_profiles": str(tmp_path / "agent-profiles.json"),
        "agent_env_file": str(tmp_path / "agent.env"),
        "launcher_local_config": str(tmp_path / "launcher.json"),
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), "connect", "--config", str(config_file)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ready": True, "runtime_state": "ready"}
    assert "synthetic-test-secret" not in result.stdout + result.stderr
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert captured["key"] == "synthetic-test-secret"
    assert "synthetic-test-secret" not in " ".join(captured["args"])
    assert captured["xdg"] == str(runtime_root / "state")

    capture.unlink()
    key_file.chmod(0o644)
    refused = subprocess.run(
        [sys.executable, str(script), "connect", "--config", str(config_file)],
        capture_output=True, text=True, check=False,
    )
    assert refused.returncode == 2
    assert not capture.exists()
    assert "synthetic-test-secret" not in refused.stdout + refused.stderr
