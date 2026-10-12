import json
import pytest
from tiancheng_mcp.media_config import MediaSettings
from tiancheng_mcp.runtime_config import launcher_config, runtime_arguments


@pytest.mark.parametrize('document', [
    [], {'exports': 'true'}, {'images': 1}, {'unexpected': True},
    {'attachments': {'mode': 'proxy'}}, {'attachments': {'mode': 'direct'}},
    *({'attachments': {'hosts': [host]}} for host in
      ('*.example.com', 'https://example.com', '127.0.0.1', 'example.com:443', 'a..com')),
])
def test_invalid_media_settings_rejected(document):
    with pytest.raises(ValueError): MediaSettings.parse(document)


def test_settings_are_local_defaults_and_partial_merge(tmp_path):
    config_dir = tmp_path / 'config'; config_dir.mkdir()
    (config_dir / 'launcher.defaults.json').write_text(json.dumps({'media': {
        'images': False, 'attachments': {'mode': 'off', 'hosts': ['FILES.EXAMPLE.COM']}}}))
    local = config_dir / 'launcher.local.json'
    local.write_text(json.dumps({'media': {'exports': True}}))
    config = launcher_config(tmp_path, local, {})
    assert config['media'] == {'images': False, 'exports': True,
        'attachments': {'mode': 'off', 'hosts': ['files.example.com']}}
    args = runtime_arguments(config, local)
    assert json.loads(args[args.index('--media-config')+1]) == config['media']
