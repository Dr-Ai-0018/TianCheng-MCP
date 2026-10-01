from __future__ import annotations

import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    HARNESS = runpy.run_path(str(ROOT / "scripts/accept_policy_hotreload.py"))
finally:
    sys.path.pop(0)
PREPARE = HARNESS["prepare_fixture"]


def test_setup_creates_unique_git_project_and_separate_policy(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    staging = tmp_path / "staging"
    first, second = PREPARE(workspace, staging), PREPARE(workspace, staging)
    assert first.sandbox != second.sandbox
    assert first.project.is_dir() and second.project.is_dir()
    assert first.policy_file.parent not in first.project.parents
    assert first.project not in first.policy_file.parents
    assert json.loads(first.policy_file.read_text(encoding="utf-8")) == {
        "rules": [{"path": str(workspace), "mode": "full"}],
    }
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=first.project,
                            capture_output=True, text=True, check=True)
    assert Path(result.stdout.strip()).resolve() == first.project.resolve()
    subprocess.run([sys.executable, "-c", "from pathlib import Path; Path('probe.txt').write_text('child')"],
                   cwd=first.project, check=True)
    assert (first.project / "probe.txt").read_text() == "child"
    assert not (workspace / "probe.txt").exists()


def test_setup_failure_cleans_only_its_fixture(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    preserved = staging / "existing.txt"
    preserved.write_text("preserved")

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "git")

    monkeypatch.setattr(HARNESS["subprocess"], "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        PREPARE(tmp_path, staging)
    assert list(staging.iterdir()) == [preserved]
    assert preserved.read_text() == "preserved"


@pytest.mark.parametrize("keep", [False, True])
def test_prepare_only_cli_does_not_start_agents_and_honors_cleanup(tmp_path, keep):
    workspace, staging = tmp_path / "workspace", tmp_path / "staging"
    workspace.mkdir()
    arguments = [sys.executable, str(ROOT / "scripts/accept_policy_hotreload.py"),
                 "--workspace", str(workspace), "--test-root", str(staging), "--prepare-only"]
    if keep:
        arguments.append("--keep")
    result = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8", check=True)
    report = json.loads(result.stdout.splitlines()[0])
    assert report["prepared"] is True
    assert Path(report["project"]).exists() is keep
    assert bool(list(staging.iterdir())) is keep
