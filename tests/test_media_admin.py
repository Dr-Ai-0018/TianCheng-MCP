import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from tiancheng_mcp.media_admin import edit, explain


def configured(tmp_path):
    config = tmp_path / 'config'; config.mkdir()
    (tmp_path / 'workspace').mkdir()
    (config / 'launcher.defaults.json').write_text(json.dumps({'workspace': 'workspace'}))
    return config / 'local.json'


def test_separate_switches_preserve_other_fields_and_backup(tmp_path):
    local = configured(tmp_path)
    original = '{"customSetting":true,"executionEnvironment":{"mode":"minimal"}}'
    local.write_text(original)
    assert explain(tmp_path, local)['configuration_role'] == 'next_start'
    state = edit(tmp_path, local, 'exports', 'on')
    assert state['exports'] and state['images'] and state['attachments']['mode'] == 'off'
    assert Path(str(local) + '.media.bak').read_text() == original
    assert json.loads(local.read_text())['executionEnvironment']['mode'] == 'minimal'
    assert json.loads(local.read_text())['customSetting'] is True
    state = edit(tmp_path, local, 'attachments', 'direct', ['FILES.EXAMPLE.COM'])
    assert state['attachments'] == {'mode': 'direct', 'hosts': ['files.example.com']}
    assert not edit(tmp_path, local, 'images', 'off')['images']
    previous = local.read_bytes()
    with pytest.raises(ValueError): edit(tmp_path, local, 'attachments', 'direct', ['*.example.com'])
    assert local.read_bytes() == previous


def test_bad_local_payload_not_echoed(tmp_path):
    local = configured(tmp_path)
    local.write_text('synthetic-secret invalid JSON')
    result = subprocess.run([sys.executable, '-m', 'tiancheng_mcp.media_admin',
        '--project-root', str(tmp_path), '--local', str(local), 'status'],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert 'synthetic-secret' not in result.stdout + result.stderr


def test_proxy_edit_is_local_atomic_preserves_other_settings_and_modes(tmp_path):
    local = configured(tmp_path)
    local.write_text(json.dumps({'customSetting': True}))
    state = edit(tmp_path, local, 'attachments', 'clash_proxy', ['files.example.com'], 'http://127.0.0.1:7897')
    assert state['attachments']['mode'] == 'clash_proxy'
    assert state['attachments']['proxy_url'] == 'http://127.0.0.1:7897'
    assert json.loads(local.read_text())['customSetting']
    previous = local.read_bytes()
    with pytest.raises(ValueError):
        edit(tmp_path, local, 'attachments', 'clash_proxy', ['files.example.com'], 'http://user:secret@127.0.0.1:7897')
    assert local.read_bytes() == previous
    assert edit(tmp_path, local, 'attachments', 'off')['attachments']['mode'] == 'off'
    assert edit(tmp_path, local, 'attachments', 'direct')['attachments']['mode'] == 'direct'


def test_workspace_cannot_be_admin_config_destination(tmp_path):
    configured(tmp_path)
    local = tmp_path / 'workspace' / 'config.json'
    with pytest.raises((ValueError, PermissionError)):
        edit(tmp_path, local, 'exports', 'on')
    assert not local.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows launcher')
def test_real_tq_media_json_reads_isolated_configuration(tmp_path):
    from scripts.local_runtime import powershell_path
    root = Path(__file__).resolve().parents[1]
    local = configured(tmp_path)
    local.write_text(json.dumps({'workspace': str(tmp_path / 'workspace'), 'python': sys.executable,
        'media': {'images': False}}))
    result = subprocess.run([powershell_path(root), '-NoLogo', '-NoProfile', '-File', str(root / 'tc.ps1'),
        '-Action', 'media', '-Json', '-ConfigPath', str(local)],
        capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads(result.stdout)
    assert state['images'] is False and state['exports'] is False
    assert state['configuration_role'] == 'next_start'
