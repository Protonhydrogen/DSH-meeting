"""Install only the Meetings plugin and its supported DSH home-overlay row.

No Desktop Profile, DSH core, package manager, backend data or agent state is
modified. Uninstall unregisters the plugin; deployed files are retained.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
import sys
from pathlib import Path
import tarfile
import uuid
import yaml

from scientific_conference.config import default_install_root

ROOT=Path(__file__).resolve().parents[1]
MANIFEST='.dsh-meetings-manifest.json'
BEGIN=b'# BEGIN dsh-meetings managed\n'
END=b'# END dsh-meetings managed\n'
OWNER='dsh-meetings-installer-v1'

def digest(data):
    return hashlib.sha256(data).hexdigest()

def encode(value):
    return (json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+'\n').encode('utf8')

def atomic_write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name('.tmp-'+uuid.uuid4().hex[:12])
    try:
        with temp.open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()

def backup(path,state_dir,label):
    if path.is_file():
        target=state_dir/'backups'/(label+'-'+uuid.uuid4().hex+'.bin')
        atomic_write(target,path.read_bytes())

def inventory(source):
    if source.is_symlink() or source.is_junction() or not source.is_dir():raise ValueError('Plugin source is unavailable')
    files={}
    for path in source.rglob('*'):
        rel=path.relative_to(source)
        if path.is_symlink() or path.is_junction():raise ValueError('Plugin source must not contain links')
        if path.is_file() and (rel.as_posix()=='package.json' or rel.parts[0]=='lib'):
            files[rel.as_posix()]=path.read_bytes()
    if 'package.json' not in files or 'lib/index.js' not in files:raise ValueError('Plugin source needs package.json and lib/index.js')
    try:package=json.loads(files['package.json'])
    except (ValueError,UnicodeError):raise ValueError('Invalid plugin package metadata') from None
    return files,str(package.get('version','unknown'))

def manifest_at(destination):
    path=destination/MANIFEST
    if not path.is_file():
        if destination.exists() and any(destination.iterdir()):raise ValueError('Destination contains unowned files')
        return None
    try:manifest=json.loads(path.read_bytes())
    except (ValueError,UnicodeError):raise ValueError('Invalid installation manifest') from None
    if manifest.get('owner')!=OWNER or not isinstance(manifest.get('files'),dict):raise ValueError('Destination is not owned by this installer')
    expected=set(manifest['files'])|{MANIFEST}
    actual=set()
    for item in destination.rglob('*'):
        if item.is_symlink() or item.is_junction():raise ValueError('Installed package contains links')
        if item.is_file():actual.add(item.relative_to(destination).as_posix())
    if actual!=expected:raise ValueError('Installed package contains unknown or missing files')
    for name,sha in manifest['files'].items():
        path=destination/name
        if not path.resolve().is_relative_to(destination.resolve()) or digest(path.read_bytes())!=sha:
            raise ValueError('Installed package has local changes; preserve or restore them before upgrade')
    return manifest

def parse_patch(raw):
    try:data=yaml.safe_load(raw.decode('utf-8-sig'))
    except (ValueError,UnicodeError,yaml.YAMLError):raise ValueError('Malformed DSH home overlay') from None
    if data is not None and (not isinstance(data,list) or any(not isinstance(row,dict) for row in data)):
        raise ValueError('DSH home overlay must be a patch list')
    return data or []

def contains_id(value):
    if isinstance(value,dict):return value.get('id')=='dsh-meetings' or any(contains_id(x) for x in value.values())
    if isinstance(value,list):return any(contains_id(x) for x in value)
    return False

def overlay_parts(raw,manifest):
    parse_patch(raw)
    if b'dsh-meetings managed' not in raw:
        if contains_id(parse_patch(raw)):raise ValueError('Foreign dsh-meetings registration exists')
        return raw,None,b''
    if raw.count(BEGIN)!=1 or raw.count(END)!=1 or raw.count(b'dsh-meetings managed')!=2:
        raise ValueError('Malformed or foreign Meetings marker')
    start=raw.index(BEGIN);end=raw.index(END)+len(END)
    # Every block written by this installer owns one leading LF. Removing
    # exactly that LF restores even an original file without a final newline.
    if start==0 or raw[start-1:start]!=b'\n' or end<start:raise ValueError('Foreign Meetings marker boundary')
    start-=1;block=raw[start:end]
    if not manifest or manifest.get('overlay_sha256')!=digest(block):raise ValueError('Managed Meetings block changed or has no ownership manifest')
    before,after=raw[:start],raw[end:]
    if contains_id(parse_patch(before+after)):raise ValueError('Duplicate Meetings registration exists')
    return before,block,after

def options(home,destination,source,project_root,python,config,state_dir,app_root):
    raw_paths={k:Path(v).expanduser() for k,v in locals().copy().items()}
    if any(p.is_symlink() or p.is_junction() for p in (raw_paths['destination'],raw_paths['home']/'cordis.patch.yml')):raise ValueError('Installation targets must not be links')
    paths={k:v.resolve() for k,v in raw_paths.items()}
    dest=paths['destination']
    for name in ('home','source','project_root','state_dir','app_root'):
        other=paths[name]
        if other==dest or other.is_relative_to(dest):raise ValueError('Destination overlaps protected source, state, or application paths')
    if dest.is_relative_to(paths['home']) or dest.is_relative_to(paths['app_root']):raise ValueError('Deploy outside DSH home and application files')
    if paths['state_dir'].is_relative_to(paths['home']) or paths['state_dir'].is_relative_to(paths['source']):raise ValueError('Keep backup state outside DSH home and plugin source')
    return paths

def managed_block(paths):
    cfg={key:str(paths[value]) for key,value in [('projectRoot','project_root'),('python','python'),('configPath','config'),('stateDir','state_dir'),('appRoot','app_root')]}
    rows=[{'insert':[{'id':'dsh-meetings','name':(paths['destination']/'lib/index.js').as_uri(),'config':cfg}]}]
    return b'\n'+BEGIN+yaml.safe_dump(rows,allow_unicode=True,sort_keys=False).encode('utf8')+END

def install(*,home,destination,source,project_root,python,config,state_dir,app_root,**_):
    paths=options(home,destination,source,project_root,python,config,state_dir,app_root)
    dest=paths['destination'];overlay=paths['home']/'cordis.patch.yml'
    old=manifest_at(dest);raw=overlay.read_bytes() if overlay.exists() else b''
    before,previous,after=overlay_parts(raw,old)
    files,version=inventory(paths['source']);block=managed_block(paths)
    replacement=before+block+after
    parse_patch(replacement) # Validate the complete overlay before any package or home write.
    manifest={'owner':OWNER,'version':version,'files':{k:digest(v) for k,v in sorted(files.items())},'overlay_sha256':digest(block),'overlay_created':old.get('overlay_created',not overlay.exists()) if old else not overlay.exists()}
    if old==manifest and raw==replacement:return status(**paths)
    # Back up every overwritten artifact outside both deployment and DSH home.
    backup(overlay,paths['state_dir'],'overlay')
    if old:
        bundle=paths['state_dir']/'backups'/('package-'+uuid.uuid4().hex[:12])
        for name in [*old['files'],MANIFEST]:atomic_write(bundle/name,(dest/name).read_bytes())
    for name,data in files.items():atomic_write(dest/name,data)
    if old:
        for name in old['files'].keys()-files.keys():
            (dest/name).unlink() # prevalidated hash-owned file only
    atomic_write(dest/MANIFEST,encode(manifest))
    atomic_write(overlay,replacement)
    return status(**paths)

def uninstall(*,home,destination,source,project_root,python,config,state_dir,app_root,**_):
    paths=options(home,destination,source,project_root,python,config,state_dir,app_root)
    dest=paths['destination'];overlay=paths['home']/'cordis.patch.yml'
    old=manifest_at(dest);raw=overlay.read_bytes() if overlay.exists() else b''
    before,block,after=overlay_parts(raw,old)
    if block:
        backup(overlay,paths['state_dir'],'overlay')
        remaining=before+after
        if not remaining and old.get('overlay_created',False):overlay.unlink() # only our newly created, otherwise empty registry file
        else:atomic_write(overlay,remaining)
    return status(**paths)

def status(*,home,destination,**_):
    home=Path(home).resolve();destination=Path(destination).resolve()
    result={'installed':False,'package_verified':False,'home':str(home),'destination':str(destination)}
    try:
        manifest=manifest_at(destination);result['package_verified']=manifest is not None
        raw=(home/'cordis.patch.yml').read_bytes() if (home/'cordis.patch.yml').exists() else b''
        _,block,_=overlay_parts(raw,manifest)
        result['installed']=bool(block and manifest)
        if manifest:result.update(version=manifest['version'],file_count=len(manifest['files']))
    except (ValueError,OSError):result['error']='Installation ownership or overlay validation failed'
    return result

def pack(*,source,archive,**_):
    source=Path(source).resolve();archive=Path(archive).resolve()
    files,version=inventory(source)
    if archive.is_relative_to(source):raise ValueError('Archive must be outside plugin source')
    manifest={'owner':OWNER,'version':version,'files':{k:digest(v) for k,v in sorted(files.items())}}
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w:gz') as tar:
        for name,data in sorted({**files,MANIFEST:encode(manifest)}.items()):
            info=tarfile.TarInfo('dsh-meetings/'+name);info.size=len(data);info.mode=0o644;info.mtime=0
            tar.addfile(info,io.BytesIO(data))
    if archive.exists():raise ValueError('Archive already exists; choose a new output path')
    atomic_write(archive,buf.getvalue())
    return {'archive':str(archive),'sha256':digest(buf.getvalue()),'file_count':len(files),'version':version}

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['install','uninstall','status','pack'])
    state = default_install_root()
    local_config = ROOT / 'config' / 'local.yaml'
    programs = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'Programs'
    defaults = {
        'home': Path(os.environ.get('DSH_HOME', Path.home() / '.dsh')),
        'destination': state / 'plugins' / 'dsh-meetings',
        'source': ROOT / 'dsh-meetings-plugin',
        'project-root': ROOT,
        'python': Path(sys.executable),
        'config': local_config if local_config.is_file() else ROOT / 'config' / 'example.yaml',
        'state-dir': state / 'state' / 'dsh-meetings',
        'app-root': programs / 'DeepSeek Harness' / 'resources' / 'app.asar',
        'archive': state / 'exports' / 'dsh-meeting.tar.gz',
    }
    for key,value in defaults.items():p.add_argument('--'+key,type=Path,default=value)
    return p

def main(argv=None):
    args=vars(parser().parse_args(argv));command=args.pop('command')
    try:
        result={'install':install,'uninstall':uninstall,'status':status,'pack':pack}[command](**args)
        print(json.dumps(result,ensure_ascii=False));return 0
    except (OSError,ValueError) as exc:
        # Validation messages contain no overlay/config content or secrets.
        print(json.dumps({'ok':False,'error':str(exc) if isinstance(exc,ValueError) else 'Filesystem operation failed; inspect preserved backups'},ensure_ascii=False));return 1

if __name__=='__main__':raise SystemExit(main())
