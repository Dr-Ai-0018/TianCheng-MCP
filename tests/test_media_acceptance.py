"""The acceptance driver uses the normal launcher and rejects live collisions."""
import json
import os
from pathlib import Path
import socket
import subprocess
import pytest
from scripts.local_runtime import powershell_path

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows acceptance launcher')
ROOT = Path(__file__).resolve().parents[1]


def prepared(tmp_path):
    root = tmp_path/'project'
    scripts = root/'scripts'; scripts.mkdir(parents=True)
    driver = scripts/'media-acceptance.ps1'
    driver.write_bytes((ROOT/'scripts/media-acceptance.ps1').read_bytes())
    state = root/'.tmp/media-0.18.0'; state.mkdir(parents=True)
    config = state/'launcher.local.json'
    config.write_text(json.dumps({'defaultProfile':'media-probe',
        'workspace':str(state/'functional-workspace'), 'profileDir':str(state/'profiles'),
        'healthBaseUrl':'http://127.0.0.1:18080'}),encoding='utf-8')
    (root/'tc.ps1').write_text('''param($Action,$Profile,$ConfigPath,[switch]$Force,[switch]$Json,[switch]$NoPause)
@{action=$Action;profile=$Profile;config=$ConfigPath;force=[bool]$Force;json=[bool]$Json}|ConvertTo-Json -Compress
$global:LASTEXITCODE=0
''',encoding='utf-8')
    return root, driver, config


def run(driver, *args):
    return subprocess.run([powershell_path(ROOT),'-NoLogo','-NoProfile','-File',str(driver),*args],
                          capture_output=True,text=True,encoding='utf-8',timeout=20)


@pytest.mark.parametrize('action', ['status','stop'])
def test_forwards_only_owned_test_profile_to_normal_launcher(tmp_path, action):
    root, driver, config = prepared(tmp_path)
    result = run(driver,'-Action',action,'-Json')
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['profile'] == 'media-probe' and data['action'] == action
    assert Path(data['config']) == config
    assert data['force'] == (action == 'stop') and data['json']


def test_unsafe_workspace_is_rejected_before_launcher(tmp_path):
    root, driver, config = prepared(tmp_path)
    value = json.loads(config.read_text(encoding='utf-8'))
    value['workspace'] = str(root/'regular-workspace')
    config.write_text(json.dumps(value),encoding='utf-8')
    result = run(driver,'-Action','status')
    assert result.returncode != 0 and not result.stdout.strip()
    assert 'requires its isolated workspace' in result.stderr


def test_listening_but_not_ready_regular_runtime_prevents_competing_start(tmp_path):
    root, driver, config = prepared(tmp_path)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0)); listener.listen()
        folder = root/'config'; folder.mkdir()
        (folder/'launcher.local.json').write_text(json.dumps({
            'healthBaseUrl':f'http://127.0.0.1:{listener.getsockname()[1]}'}),encoding='utf-8')
        result = run(driver,'-Action','start')
    assert result.returncode != 0
    assert 'already listens on port' in result.stderr
    assert '"profile"' not in result.stdout  # normal launcher was never invoked
