"""User-scoped Windows DPAPI secrets outside the project and its snapshots."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re

class Blob(ctypes.Structure):
    _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_ubyte))]

def credential_path(name):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',name):
        raise ValueError('Invalid credential name')
    return Path(os.environ['LOCALAPPDATA'])/'ScientificConference'/'credentials'/(name+'.dpapi')

def crypt(data, *, decrypt=False):
    if os.name!='nt':
        raise RuntimeError('Credential storage requires Windows')
    buf=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)))
    target=Blob()
    dll=ctypes.WinDLL('crypt32',use_last_error=True)
    if decrypt:
        ok=dll.CryptUnprotectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target))
    else:
        ok=dll.CryptProtectData(ctypes.byref(source),'ScientificConference',None,None,None,1,ctypes.byref(target))
    if not ok:
        raise RuntimeError('Windows could not access the credential for this user')
    try:
        return ctypes.string_at(target.pbData,target.cbData)
    finally:
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.LocalFree.argtypes=[ctypes.c_void_p]
        kernel.LocalFree(target.pbData)

def save(name, secret):
    if not secret.strip():
        raise ValueError('Empty API key')
    p=credential_path(name)
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix('.tmp')
    tmp.write_bytes(crypt(secret.strip().encode('utf-8')))
    tmp.replace(p)

def load_into_environment(name, env_name):
    if os.environ.get(env_name):
        return
    p=credential_path(name)
    if not p.exists():
        raise ValueError('API credential is not configured. Run python -m scientific_conference.credentials first.')
    os.environ[env_name]=crypt(p.read_bytes(),decrypt=True).decode('utf-8')

def main():
    from getpass import getpass
    from .config import load_config
    from .agents import build_agent, NoteRequest
    from .skill import load_skill
    cfg=load_config()
    name=cfg.runner.openai.credential_name
    if not name:
        raise ValueError('No credential_name configured')
    secret=getpass('DeepSeek API key (hidden; stored for this Windows user): ')
    save(name,secret)
    os.environ[cfg.runner.openai.api_key_env]=secret
    print('Encrypted credential saved. Checking model with a synthetic sentence only...')
    try:
        agent=build_agent(cfg)
        skill=load_skill(cfg.skill_path)
        request=NoteRequest(mode='turn',session_id='credential-check',target_id='check',source_revision=1,transcript=[{'segment_id':'test','text':'The speaker introduced a sample calibration procedure.','start_ms':0,'end_ms':1000}],context={},skill_text=skill.body,skill_version=skill.version,language='en')
        result,_=agent.generate(request)
        from .notes import validate_note
        if validate_note(result):
            raise ValueError('Model response failed schema validation')
        print('Model connection and structured response verified. You may now open the conference panel.')
    except Exception:
        print('Credential saved, but connection check failed. Check the model, endpoint, account balance and network. No meeting data was sent.')
        raise SystemExit(1)

if __name__=='__main__':
    main()
