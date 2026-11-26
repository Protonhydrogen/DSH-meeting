"""Installer contracts; fixtures never use the production DSH home."""
import importlib.util
import json
from pathlib import Path
import tarfile
import pytest
import yaml

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/install_dsh_meetings.py'

def installer():
    assert SCRIPT.is_file(), 'Task 5 installer is missing'
    spec=importlib.util.spec_from_file_location('meetings_installer',SCRIPT)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

@pytest.fixture
def deployment(tmp_path):
    source=tmp_path/'source';(source/'lib').mkdir(parents=True)
    (source/'package.json').write_text(json.dumps({'name':'dsh-meetings','version':'1.0.0','type':'module'}),encoding='utf8')
    (source/'lib/index.js').write_text('export default 1;\n',encoding='utf8')
    home=tmp_path/'home';home.mkdir()
    return dict(home=home,destination=tmp_path/'installed/dsh-meetings',source=source,project_root=tmp_path/'科研项目 with spaces',python=Path('D:/external Python/python.exe'),config=tmp_path/'配置 with spaces/it\'s.yaml',state_dir=tmp_path/'external state',app_root=tmp_path/'DSH Desktop/app')

def test_install_preserves_existing_insert_and_uninstall_restores_exact_bytes(deployment):
    mod=installer();p=deployment['home']/'cordis.patch.yml'
    original=b'\xef\xbb\xbf# existing\r\n- insert:\r\n    - id: other\r\n      name: other-plugin' # no final newline
    p.write_bytes(original)
    result=mod.install(**deployment)
    assert result['installed'] is True
    data=yaml.safe_load(p.read_text(encoding='utf-8-sig'))
    assert data[0]['insert'][0]['id']=='other'
    row=data[1]['insert'][0]
    assert row['id']=='dsh-meetings' and row['name'].startswith('file:///')
    assert row['config']['configPath']==str(deployment['config'].resolve())
    assert p.read_bytes().startswith(original)
    mod.uninstall(**deployment)
    assert p.read_bytes()==original
    assert (deployment['destination']/'lib/index.js').is_file()
    assert list((deployment['state_dir']/'backups').glob('*.bin'))

def test_repeat_install_is_idempotent_and_upgrade_only_changes_owned_package(deployment):
    mod=installer();mod.install(**deployment)
    p=deployment['home']/'cordis.patch.yml';before=p.read_bytes()
    manifest=(deployment['destination']/mod.MANIFEST).read_bytes()
    mod.install(**deployment)
    assert p.read_bytes()==before
    assert (deployment['destination']/mod.MANIFEST).read_bytes()==manifest
    (deployment['source']/'lib/index.js').write_text('export default 2;',encoding='utf8')
    mod.install(**deployment)
    assert (deployment['destination']/'lib/index.js').read_text()=='export default 2;'
    mod.uninstall(**deployment)
    assert not p.exists() # an absent original overlay stays absent, not invalid empty YAML

@pytest.mark.parametrize('overlay',[
    '- insert:\n  - id: dsh-meetings\n    name: foreign\n',
    '# BEGIN dsh-meetings managed\n- insert: []\n',
    '# END dsh-meetings managed\n',
    '# BEGIN dsh-meetings managed elsewhere\n- insert: []\n# END dsh-meetings managed elsewhere\n',
    'not: a-patch-list\n',
    '- insert: [\n',
])
def test_foreign_or_malformed_overlay_refuses_without_mutation(deployment,overlay):
    mod=installer();p=deployment['home']/'cordis.patch.yml';p.write_text(overlay,encoding='utf8');before=p.read_bytes()
    with pytest.raises(ValueError):mod.install(**deployment)
    assert p.read_bytes()==before and not deployment['destination'].exists()

def test_unknown_or_modified_deployment_is_never_overwritten(deployment):
    mod=installer();deployment['destination'].mkdir(parents=True)
    unknown=deployment['destination']/'foreign.txt';unknown.write_text('keep')
    with pytest.raises(ValueError):mod.install(**deployment)
    assert unknown.read_text()=='keep'
    unknown.unlink();mod.install(**deployment)
    owned=deployment['destination']/'lib/index.js';owned.write_text('local edit')
    with pytest.raises(ValueError):mod.install(**deployment)
    assert owned.read_text()=='local edit'

def test_managed_block_tamper_refuses_and_status_never_exposes_config(deployment):
    mod=installer();mod.install(**deployment)
    p=deployment['home']/'cordis.patch.yml';p.write_bytes(p.read_bytes().replace(b'projectRoot:',b'evilConfig:'))
    before=p.read_bytes()
    with pytest.raises(ValueError):mod.uninstall(**deployment)
    state=mod.status(**deployment)
    assert state['installed'] is False and 'projectRoot' not in json.dumps(state)
    assert p.read_bytes()==before

def test_pack_contains_only_plugin_code_and_hash_manifest(deployment,tmp_path):
    mod=installer();(deployment['source']/'tests').mkdir();(deployment['source']/'tests/secret.txt').write_text('not runtime')
    archive=tmp_path/'offline.tar.gz';mod.pack(**deployment,archive=archive)
    with tarfile.open(archive) as t:
        names=t.getnames();assert 'dsh-meetings/lib/index.js' in names
        assert all('secret' not in n for n in names)
        manifest=json.load(t.extractfile('dsh-meetings/'+mod.MANIFEST))
        assert len(manifest['files']['lib/index.js'])==64
    assert not deployment['home'].joinpath('cordis.patch.yml').exists()

def test_cli_defaults_and_status_are_read_only(deployment,capsys):
    mod=installer();args=mod.parser().parse_args(['status'])
    assert args.destination == mod.default_install_root() / 'plugins' / 'dsh-meetings'
    cli=['status']
    for key,value in deployment.items():cli.extend(['--'+key.replace('_','-'),str(value)])
    assert mod.main(cli)==0
    assert json.loads(capsys.readouterr().out)['installed'] is False
    assert not deployment['destination'].exists()

def test_upgrade_backs_up_named_owned_files_and_removes_only_obsolete_code(deployment):
    mod=installer();extra=deployment['source']/'lib/old.js';extra.write_text('original old code')
    mod.install(**deployment)
    extra.unlink();(deployment['source']/'lib/index.js').write_text('export default 3;')
    mod.install(**deployment)
    assert not (deployment['destination']/'lib/old.js').exists()
    assert any(p.read_text()=='original old code' for p in (deployment['state_dir']/'backups').glob('package-*/lib/old.js'))
    state=deployment['state_dir']/'native-agent.json';state.write_text('retain native association')
    mod.uninstall(**deployment)
    assert state.read_text()=='retain native association'

def test_overlay_duplicate_marker_and_duplicate_id_outside_block_refuse(deployment):
    mod=installer();mod.install(**deployment)
    p=deployment['home']/'cordis.patch.yml';original=p.read_bytes()
    for extra in [original,b'\n- insert:\n  - id: dsh-meetings\n    name: other\n']:
        p.write_bytes(original+extra)
        with pytest.raises(ValueError):mod.install(**deployment)
        assert p.read_bytes()==original+extra

def test_state_backup_must_not_be_inside_dsh_home(deployment):
    mod=installer();deployment['state_dir']=deployment['home']/'backups'
    with pytest.raises(ValueError):mod.install(**deployment)
    assert not (deployment['home']/'cordis.patch.yml').exists()

@pytest.mark.parametrize("original",[b"[]\n",b"[{insert: [{id: other, name: other-plugin}]}]\n"])
def test_flow_overlay_refused_before_any_mutation(deployment,original):
    mod=installer();p=deployment["home"]/"cordis.patch.yml";p.write_bytes(original)
    with pytest.raises(ValueError):mod.install(**deployment)
    assert p.read_bytes()==original
    assert not deployment["destination"].exists()

def test_uninstall_of_created_overlay_leaves_valid_dsh_home(deployment):
    mod=installer();mod.install(**deployment);manifest=json.loads((deployment["destination"]/mod.MANIFEST).read_bytes())
    assert manifest["overlay_created"] is True
    mod.uninstall(**deployment)
    assert not (deployment["home"]/"cordis.patch.yml").exists()
    mod.install(**deployment);assert mod.status(**deployment)["installed"]
