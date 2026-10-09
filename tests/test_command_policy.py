from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import time

import pytest

from tiancheng_mcp.access_context import AccessContext
from tiancheng_mcp.command_policy import CommandPolicy, CommandPolicyError
from tiancheng_mcp.command_policy_admin import edit_policy
from tiancheng_mcp.policy import AccessPolicy, AccessRule
from tiancheng_mcp.security import WorkspaceSecurityError
from tiancheng_mcp.service import TianChengService
from tiancheng_mcp import service as service_module


def local_policy(tmp_path: Path, **fields) -> Path:
    path = tmp_path / "commands.json"
    path.write_text(json.dumps({"schema_version": 1, **fields}), encoding="utf-8")
    return path


def test_default_preset_preserves_current_developer_commands(workspace, tmp_path):
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=tmp_path / "absent.json")
    assert service.command_policy.preset == "balanced"
    assert service._ordinary_exec_commands == service._exec_commands
    assert service.run_command("PYTHON.EXE", ["-c", "print('balanced-ok')"])["stdout"].strip() == "balanced-ok"


def test_fixture_defaults_ignore_machine_policy_but_explicit_selection_works(workspace, tmp_path, monkeypatch):
    machine_root = tmp_path / "machine-project"
    (machine_root / "config").mkdir(parents=True)
    machine_policy = machine_root / "config/command-policy.local.json"
    machine_policy.write_text('{"schema_version":1,"preset":"minimal"}', encoding="utf-8")
    monkeypatch.setattr(service_module, "__file__", str(machine_root / "src/tiancheng_mcp/service.py"))
    isolated = TianChengService(workspace, None, allow_exec=True)
    assert isolated.command_policy.preset == "balanced"
    assert isolated.run_command("python", ["-c", "print('isolated-ok')"])["stdout"].strip() == "isolated-ok"
    selected = TianChengService(workspace, None, allow_exec=True, command_policy_path=machine_policy)
    assert selected.command_policy.preset == "minimal"
    with pytest.raises(PermissionError, match="allowlisted"):
        selected.run_command("python", ["-c", "print('must-not-run')"])


def test_minimal_rejects_code_and_only_allows_exact_version_templates(workspace, tmp_path):
    path = local_policy(tmp_path, preset="minimal")
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=path)
    for method in (service.run_command, service.start_process):
        with pytest.raises(PermissionError, match="allowlisted"):
            method("python", ["-c", "print('must-not-run')"])
    if "git" in service._ordinary_exec_commands:
        assert service.run_command("git", ["--version"])["exit_code"] == 0
        for args in (["status"], ["--version", "extra"], ["-c", "alias.x=!echo bad", "x"]):
            with pytest.raises(PermissionError, match="arguments are denied"):
                service.start_process("git", args)
        # The internal prepared-process path cannot bypass the argument rule.
        with pytest.raises(PermissionError, match="arguments are denied"):
            service._start_managed_process_prepared(
                "git", [*service._ordinary_exec_commands["git"], "status"], workspace,
                max_runtime_seconds=10, output_limit_bytes=4096,
            )
        assert service._process_slots.active_count == 0
        assert not service._processes


def test_local_add_disable_priority_and_case_normalization(workspace, tmp_path):
    path = local_policy(tmp_path, preset="minimal", add={
        "PYTHON.EXE": {"builtin": "python", "arguments": "any"},
        "version": {"builtin": "python", "arguments": {"exact": [["--version"]]}},
    }, disable=["Python"])
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=path)
    with pytest.raises(PermissionError, match="allowlisted"):
        service.run_command("python", ["-c", "print('blocked')"])
    assert service.run_command("version", ["--version"])["exit_code"] == 0
    with pytest.raises(PermissionError, match="arguments are denied"):
        service.run_command("version", ["-c", "print('blocked')"])
    summary = service.workspace_info()["command_policy"]
    assert summary["scope"] == "ordinary_commands_only"
    assert next(row for row in summary["commands"] if row["name"] == "python")["status"] == "disabled"


def test_safe_remains_disabled_even_with_custom_code_rule(workspace, tmp_path):
    path = local_policy(tmp_path, add={"code": {"builtin": "python"}})
    service = TianChengService(workspace, None, command_policy_path=path)
    with pytest.raises(PermissionError, match="execution is disabled"):
        service.run_command("code", ["-c", "print('blocked')"])
    assert not service.workspace_info()["command_policy"]["execution_enabled"]


def test_scoped_and_static_external_exec_inherit_parent_snapshot(workspace, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    policy = AccessPolicy(workspace, [AccessRule(workspace, "full"), AccessRule(external, "full", allow_exec=True)])
    path = local_policy(tmp_path, disable=["python"])
    parent = TianChengService(workspace, None, allow_exec=True, allow_external_grants=True, access_policy=policy, command_policy_path=path)
    # A file edit cannot silently change a running service or its new scopes.
    path.write_text('{"schema_version":1,"preset":"balanced"}', encoding="utf-8")
    with pytest.raises(PermissionError, match="allowlisted"):
        parent.policy_external_run_command("python", ["-c", "print('blocked')"], cwd=str(external))
    grant = parent.request_external_access(str(external), "exec", 600, "test command policy inheritance")
    assert grant["status"] == "approved"
    with pytest.raises(PermissionError, match="allowlisted"):
        parent.external_run_command(str(grant["grant_id"]), "python", ["-c", "print('blocked')"])
    scoped = parent._scoped_service(AccessContext(external, "exec", lambda: policy), allow_exec=True)
    assert scoped.command_policy is parent.command_policy
    with pytest.raises(PermissionError, match="allowlisted"):
        scoped.run_command("python", ["-c", "print('blocked')"])
    parent.shutdown()


def test_custom_prefix_runs_but_cannot_be_reused_by_fixed_agent_templates(workspace, tmp_path):
    script = tmp_path / "tool.py"
    script.write_text("import sys; print('fixed', sys.argv[1])", encoding="utf-8")
    path = local_policy(tmp_path, preset="minimal", add={
        "helper": {"argv": [str(Path(sys.executable).resolve()), str(script)], "arguments": {"exact": [["ok"]]}}
    })
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=path)
    assert service.run_command("helper", ["ok"])["stdout"].strip() == "fixed ok"
    started = service.start_process("helper", ["ok"], max_runtime_seconds=10)
    deadline = time.monotonic() + 15
    while service.process_status(started["process_id"])["running"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert service.process_output(started["process_id"])["stdout"].strip() == "fixed ok"
    assert "helper" not in service._exec_commands
    assert "python" in service._exec_commands  # Agent registry remains independent.
    assert "python" not in service._ordinary_exec_commands
    with pytest.raises(PermissionError):
        service.run_command("helper", ["wrong"])
    service.shutdown()


def test_builtin_and_custom_aliases_preserve_credential_output_guards(workspace, tmp_path):
    git = shutil.which("git")
    if not git:
        pytest.skip("Git unavailable")
    path = local_policy(tmp_path, add={
        "git-alias": {"builtin": "git"},
        "git-fixed": {"argv": [str(Path(git).resolve()), "credential"]},
    })
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=path)
    with pytest.raises(PermissionError, match="keyring secrets"):
        service.run_command("git-alias", ["credential", "fill"])
    with pytest.raises(PermissionError, match="keyring secrets"):
        service.run_command("git-fixed", ["fill"])


@pytest.mark.parametrize("fields", [
    {"schema_version": True}, {"schema_version": 2}, {"preset": "unknown-preset"},
    {"unknown": True}, {"disable": "python"}, {"disable": ["../python"]},
    {"add": {"x": {"builtin": "not-a-builtin"}}},
    {"add": {"x": {"builtin": "python", "argv": []}}},
    {"add": {"X": {"builtin": "python"}, "x.exe": {"builtin": "python"}}},
    {"add": {"x": {"builtin": "python", "arguments": {"prefix": ["-c"]}}}},
    {"add": {"x": {"argv": ["relative.exe"]}}},
])
def test_malformed_policy_fails_closed(tmp_path, fields):
    path = local_policy(tmp_path, **fields)
    with pytest.raises(CommandPolicyError):
        CommandPolicy.load(path)


def test_duplicate_json_keys_fail_closed(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"preset":"minimal","preset":"balanced"}', encoding="utf-8")
    with pytest.raises(CommandPolicyError):
        CommandPolicy.load(path)


def test_policy_and_custom_executable_cannot_live_in_workspace(workspace, tmp_path):
    path = workspace / "policy.json"
    path.write_text('{"schema_version":1}', encoding="utf-8")
    with pytest.raises(CommandPolicyError, match="outside the workspace"):
        CommandPolicy.load(path, workspace)
    executable = workspace / ("unsafe.exe" if os.name == "nt" else "unsafe")
    executable.write_bytes(b"fake")
    executable.chmod(0o700)
    path = local_policy(tmp_path, add={"unsafe": {"argv": [str(executable)]}})
    with pytest.raises(CommandPolicyError, match="outside the workspace"):
        CommandPolicy.load(path, workspace)


def test_admin_invalid_edit_preserves_file_and_valid_edits_round_trip(workspace, tmp_path):
    path = local_policy(tmp_path, preset="minimal")
    before = path.read_bytes()
    with pytest.raises(CommandPolicyError):
        edit_policy(path, workspace, "add", "bad", {"argv": ["not-absolute.exe"]})
    assert path.read_bytes() == before
    edit_policy(path, workspace, "add", "PYTHON.EXE", {"builtin": "python"})
    edit_policy(path, workspace, "disable", "python")
    assert "python" in CommandPolicy.load(path, workspace).disabled
    edit_policy(path, workspace, "enable", "python")
    edit_policy(path, workspace, "remove", "python")
    assert "python" not in CommandPolicy.load(path, workspace).rules
    edit_policy(path, workspace, "reset")
    assert CommandPolicy.load(path, workspace).preset == "balanced"

@pytest.mark.parametrize('preset', ['elevated', 'unrestricted'])
def test_high_presets_keep_safe_disabled_and_agent_registry_independent(workspace, tmp_path, preset):
    path = local_policy(tmp_path, preset=preset)
    safe = TianChengService(workspace, None, command_policy_path=path)
    for method in (safe.run_command, safe.start_process):
        with pytest.raises(PermissionError, match='execution is disabled'):
            method('python', ['--version'])
    dev = TianChengService(workspace, None, allow_exec=True, command_policy_path=path)
    assert dev.workspace_info()['command_policy']['host_identity'] == 'server_account'
    assert 'pwsh' not in dev._exec_commands and 'bash' not in dev._exec_commands
    assert dev.run_command('python', ['--version'])['exit_code'] == 0
    dev.shutdown()


def test_elevated_runs_explicit_platform_shell_sync_and_managed(workspace, tmp_path):
    path = local_policy(tmp_path, preset='elevated')
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=path)
    shell = next((n for n in ('pwsh', 'powershell', 'bash', 'sh') if n in service._ordinary_exec_commands), None)
    if not shell:
        pytest.skip('No platform Shell installed')
    args = ['-NoProfile', '-Command', "Write-Output 'shell-ok'"] if os.name == 'nt' else ['-c', "printf 'shell-ok\\n'"]
    assert service.run_command(shell, args)['stdout'].strip() == 'shell-ok'
    started = service.start_process(shell, args, max_runtime_seconds=15)
    deadline = time.monotonic() + 20
    while service.process_status(started['process_id'])['running'] and time.monotonic() < deadline:
        time.sleep(.05)
    assert service.process_output(started['process_id'])['stdout'].strip() == 'shell-ok'
    balanced = TianChengService(workspace, None, allow_exec=True)
    with pytest.raises(PermissionError, match='allowlisted'):
        balanced.run_command(shell, args)
    service.shutdown()


def test_missing_shell_is_unavailable_not_implicit_fallback(workspace, tmp_path, monkeypatch):
    from tiancheng_mcp import command_discovery
    monkeypatch.setattr(command_discovery, 'discover_shell_commands', lambda root: {})
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=local_policy(tmp_path, preset='elevated'))
    assert all(row['status'] == 'unavailable' for row in service.workspace_info()['command_policy']['commands'] if row['name'] in {'pwsh', 'powershell', 'cmd', 'bash', 'sh'})
    with pytest.raises(PermissionError):
        service.run_command('pwsh', ['-Command', 'echo bad'])


def test_unrestricted_path_name_disable_and_snapshot(workspace, tmp_path, monkeypatch):
    # Use a unique copy of a real native interpreter, not a mock executable.
    tool_dir = tmp_path / 'bin'
    tool_dir.mkdir()
    tool = tool_dir / ('phase-tool.exe' if os.name == 'nt' else 'phase-tool')
    shutil.copy2(Path(shutil.which('cmd') if os.name == 'nt' else sys.executable).resolve(), tool)
    probe_args = ['/d', '/c', 'echo native-ok'] if os.name == 'nt' else ['--version']
    monkeypatch.setenv('PATH', str(tool_dir) + os.pathsep + os.environ.get('PATH', ''))
    path = local_policy(tmp_path, preset='unrestricted')
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=path)
    monkeypatch.setenv('PATH', '')  # A running service retains its search path.
    assert service.run_command('phase-tool', probe_args)['exit_code'] == 0
    assert service.run_command(str(tool), probe_args)['exit_code'] == 0
    started = service.start_process(str(tool), probe_args, max_runtime_seconds=10)
    deadline = time.monotonic() + 15
    while service.process_status(started['process_id'])['running'] and time.monotonic() < deadline:
        time.sleep(.05)
    assert service.process_status(started['process_id'])['exit_code'] == 0
    summary = service.workspace_info()['command_policy']
    assert summary['mode'] == 'unrestricted' and not summary['command_list_complete']
    assert summary['absolute_executable_paths']
    blocked = TianChengService(workspace, None, allow_exec=True, command_policy_path=local_policy(tmp_path, preset='unrestricted', disable=['phase-tool']))
    for name in ('phase-tool', str(tool)):
        with pytest.raises(PermissionError, match='allowlisted'):
            blocked.run_command(name, probe_args)
    for name in ('./phase-tool', '../phase-tool', 'missing-phase-tool'):
        with pytest.raises(PermissionError):
            service.run_command(name)
    service.shutdown()


def test_unrestricted_allows_workspace_native_program_and_checks_prepared_prefix(workspace, tmp_path):
    tool = workspace / ('project-tool.exe' if os.name == 'nt' else 'project-tool')
    shutil.copy2(Path(shutil.which('cmd') if os.name == 'nt' else sys.executable).resolve(), tool)
    probe_args = ['/d', '/c', 'echo native-ok'] if os.name == 'nt' else ['--version']
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=local_policy(tmp_path, preset='unrestricted'))
    assert service.run_command(str(tool), probe_args)['exit_code'] == 0
    with pytest.raises(PermissionError, match='registered executable'):
        service._start_managed_process_prepared(str(tool), [str(Path(sys.executable).resolve()), '--version'], workspace, max_runtime_seconds=10, output_limit_bytes=4096)
    assert service._process_slots.active_count == 0
    with pytest.raises(ValueError, match='bounded text'):
        service.run_command(str(tool), ['\x00'])
    with pytest.raises(WorkspaceSecurityError):
        service.run_command(str(tool), probe_args, cwd='..')
    service.shutdown()


def test_unrestricted_exact_override_and_external_snapshot(workspace, tmp_path):
    path = local_policy(tmp_path, preset='unrestricted', add={'python': {'builtin': 'python', 'arguments': {'exact': [['--version']]}}}, disable=['gh'])
    external = tmp_path / 'external-phase2'
    external.mkdir()
    policy = AccessPolicy(workspace, [AccessRule(workspace, 'full'), AccessRule(external, 'full', allow_exec=True)])
    parent = TianChengService(workspace, None, allow_exec=True, allow_external_grants=True, access_policy=policy, command_policy_path=path)
    path.write_text('{"schema_version":1,"preset":"minimal"}', encoding='utf-8')
    assert parent.policy_external_run_command(str(Path(sys.executable).resolve()), ['--version'], cwd=str(external))['exit_code'] == 0
    grant = parent.request_external_access(str(external), 'exec', 600, 'phase2 snapshot')
    assert parent.external_run_command(str(grant['grant_id']), 'python', ['--version'])['exit_code'] == 0
    with pytest.raises(PermissionError, match='arguments are denied'):
        parent.external_run_command(str(grant['grant_id']), 'python', ['-c', 'print(1)'])
    for name in ('gh', str(tmp_path / 'gh.exe')):
        with pytest.raises(PermissionError, match='allowlisted'):
            parent.run_command(name, ['auth', 'token'])
    scoped = parent._scoped_service(AccessContext(external, 'exec', lambda: policy), allow_exec=True)
    assert scoped.command_policy is parent.command_policy
    parent.shutdown()


def test_unrestricted_direct_git_credentials_still_blocked(workspace, tmp_path):
    git = shutil.which('git')
    if not git:
        pytest.skip('Git unavailable')
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=local_policy(tmp_path, preset='unrestricted'))
    with pytest.raises(PermissionError, match='keyring secrets'):
        service.run_command(str(Path(git).resolve()), ['credential', 'fill'])


@pytest.mark.skipif(os.name != 'nt', reason='Windows script launch contract')
def test_unrestricted_windows_scripts_need_explicit_interpreter(workspace, tmp_path):
    script = workspace / 'script.cmd'
    script.write_text('@echo ok', encoding='utf-8')
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=local_policy(tmp_path, preset='unrestricted'))
    with pytest.raises(PermissionError, match='explicit interpreter'):
        service.run_command(str(script))



def test_unrestricted_background_worker_applies_same_policy(workspace, tmp_path):
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=local_policy(tmp_path, preset='unrestricted', disable=['gh']))
    try:
        success = service.jobs.submit('run_command', lambda cancel: service.run_command(str(Path(sys.executable).resolve()), ['--version']))
        assert success.done.wait(15)
        assert service.job_status(success.job_id)['state'] == 'succeeded'
        assert service.job_result(success.job_id)['result']['exit_code'] == 0
        denied = service.jobs.submit('run_command', lambda cancel: service.run_command('gh', ['auth', 'token']))
        assert denied.done.wait(15)
        assert service.job_status(denied.job_id)['state'] == 'failed'
    finally:
        service.shutdown()


def test_unrestricted_path_search_does_not_use_current_directory(workspace, tmp_path, monkeypatch):
    tool = workspace / ('cwd-only.exe' if os.name == 'nt' else 'cwd-only')
    shutil.copy2(Path(shutil.which('cmd') if os.name == 'nt' else sys.executable).resolve(), tool)
    monkeypatch.setenv('PATH', '')
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=local_policy(tmp_path, preset='unrestricted'))
    monkeypatch.chdir(workspace)
    with pytest.raises(PermissionError, match='startup PATH'):
        service.run_command('cwd-only')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX case-sensitive executable names')
def test_unrestricted_preserves_case_and_punctuation_in_program_name(workspace, tmp_path, monkeypatch):
    tool = tmp_path / 'My.Tool-2'
    tool.write_text('#!/bin/sh\nprintf case-ok', encoding='utf-8')
    tool.chmod(0o700)
    monkeypatch.setenv('PATH', str(tmp_path))
    service = TianChengService(workspace, None, allow_exec=True, command_policy_path=local_policy(tmp_path, preset='unrestricted'))
    assert service.run_command('My.Tool-2')['stdout'] == 'case-ok'
    with pytest.raises(PermissionError, match='startup PATH'):
        service.run_command('my.tool-2')


def test_unrestricted_native_filename_disable_admin_round_trip(workspace, tmp_path):
    path = local_policy(tmp_path, preset="unrestricted")
    edit_policy(path, workspace, "disable", "My.Tool-2.EXE")
    policy = CommandPolicy.load(path, workspace)
    assert "my.tool-2" in policy.disabled
    for name in ("My.Tool-2", str(tmp_path / "My.Tool-2.exe")):
        with pytest.raises(PermissionError, match="allowlisted"):
            policy.check(policy.request_key(name), [])
    edit_policy(path, workspace, "enable", "my.tool-2")
    assert not CommandPolicy.load(path, workspace).disabled
