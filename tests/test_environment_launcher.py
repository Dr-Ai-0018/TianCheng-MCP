from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows environment TUI")


def local_config(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "local.json"
    config.write_text(json.dumps({"workspace": str(workspace), "python": sys.executable,
        "envFile": str(tmp_path / "absent.env"),
        "executionEnvironment": {"mode": "basic", "allow": [], "deny": []}}))
    return config


def run(config, *args, text=None):
    return subprocess.run([shutil.which("pwsh"), "-NoLogo", "-NoProfile", "-File", str(ROOT / "tc.ps1"),
        "-Action", "environment", "-ConfigPath", str(config), "-NoPause", *args],
        input=text, capture_output=True, text=True, encoding="utf-8", timeout=45,
        env=dict(os.environ, SYNTHETIC_ENV_KEY="must-not-display"))


def test_json_status_has_no_values_or_side_effects(tmp_path):
    config = local_config(tmp_path)
    before = config.read_bytes()
    result = run(config, "-Json")
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["configuration_role"] == "next_start" and state["runtime_mode"] == "unknown"
    assert "must-not-display" not in result.stdout + result.stderr
    assert config.read_bytes() == before


def test_menu_save_and_inherit_confirmation(tmp_path):
    config = local_config(tmp_path)
    result = run(config, text="1\ninherit\nCANCEL\n1\nminimal\n2\nSYNTHETIC_ENV_KEY\n4\nSYNTHETIC_ENV_KEY\n0\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "已校验并保存" in result.stdout and "运行值未知" in result.stdout
    assert "must-not-display" not in result.stdout + result.stderr
    policy = json.loads(config.read_text())["executionEnvironment"]
    assert policy == {"mode": "minimal", "allow": ["SYNTHETIC_ENV_KEY"], "deny": ["SYNTHETIC_ENV_KEY"]}
    result = run(config, text="1\ninherit\nINHERIT\n0\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(config.read_text())["executionEnvironment"]["mode"] == "inherit"
