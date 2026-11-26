"""Session-local personal notebook and authenticated image attachments."""
import html
import base64
import re
import secrets
from .db import utc_now

MAX_IMAGE=12*1024*1024

class NotebookConflict(ValueError):
    pass


def init(store):
    with store.db.tx() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS personal_notebook (id INTEGER PRIMARY KEY CHECK(id=1), text TEXT NOT NULL, revision INTEGER NOT NULL)')
        conn.execute("INSERT OR IGNORE INTO personal_notebook VALUES (1,'',0)")
        conn.execute('CREATE TABLE IF NOT EXISTS notebook_images (id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL, time_ms INTEGER NOT NULL, created TEXT NOT NULL)')


def read(store):
    init(store)
    row=dict(store.db.query_one('SELECT * FROM personal_notebook WHERE id=1'))
    return {'text':row['text'],'revision':row['revision'],'images':[dict(r) for r in store.db.query('SELECT * FROM notebook_images ORDER BY created,id')]}


def export(store):
    book=read(store)
    lines=['# Personal meeting notes','',book['text'],'']
    unplaced=[im for im in book['images'] if f"(attachments/{im['id']})" not in book['text']]
    if unplaced:lines+=['## Slide images','']
    for image in unplaced:
        t=image['time_ms']//1000
        name=html.escape(image['name']).replace('\n',' ').replace('\r',' ').replace('[','').replace(']','')
        lines += [f"### {t//60:02}:{t%60:02} — {name}",'',f"![Slide](attachments/{image['id']})",'']
    path=store.paths.root/'personal_notes.md'
    temp=path.with_suffix('.tmp')
    temp.write_text('\n'.join(lines),encoding='utf-8')
    temp.replace(path)


def save(store,text,revision):
    if not isinstance(text,str) or len(text)>200000: raise ValueError('Notes exceed 200,000 characters')
    init(store)
    with store.db.tx() as conn:
        changed=conn.execute('UPDATE personal_notebook SET text=?,revision=revision+1 WHERE id=1 AND revision=?',(text,revision)).rowcount
        if not changed: raise NotebookConflict('Notes changed in another tab. Your draft is preserved; copy it before reloading.')
    export(store)
    return read(store)


def add_image(store,data):
    encoded=data.get('content','')
    if not isinstance(encoded,str) or len(encoded)>MAX_IMAGE*4//3+8: raise ValueError('Image exceeds 12 MB')
    try: raw=base64.b64decode(encoded,validate=True)
    except Exception: raise ValueError('Invalid image encoding')
    if not raw or len(raw)>MAX_IMAGE: raise ValueError('Image exceeds 12 MB')
    if raw.startswith(b'\x89PNG\r\n\x1a\n'): ext,mime='png','image/png'
    elif raw.startswith(b'\xff\xd8\xff'): ext,mime='jpg','image/jpeg'
    elif raw[:4]==b'RIFF' and raw[8:12]==b'WEBP': ext,mime='webp','image/webp'
    else: raise ValueError('Use a PNG, JPEG or WebP image')
    at=data.get('time_ms',0)
    if not isinstance(at,int) or isinstance(at,bool) or not 0<=at<=604800000: raise ValueError('Invalid timestamp')
    init(store)
    identifier=secrets.token_hex(16)+'.'+ext
    folder=store.paths.root/'attachments';folder.mkdir(exist_ok=True)
    (folder/identifier).write_bytes(raw)
    name=str(data.get('name','Slide'))[:180]
    with store.db.tx() as conn:
        conn.execute('INSERT INTO notebook_images VALUES (?,?,?,?,?)',(identifier,name,mime,at,utc_now()))
    export(store)
    return read(store)


def image(store,identifier):
    if not re.fullmatch(r'[a-f0-9]{32}\.(png|jpg|webp)',identifier or ''): raise ValueError('Invalid image ID')
    init(store)
    row=store.db.query_one('SELECT mime FROM notebook_images WHERE id=?',(identifier,))
    if not row: raise FileNotFoundError('Image not found')
    return (store.paths.root/'attachments'/identifier).read_bytes(),row['mime']


def combined_markdown(store):
    """Separate export; generated notes, notebook text and images stay intact."""
    export(store)
    notes=store.notes()
    # All currently active summaries are retained: an early overview has no
    # authority to suppress later report/turn content.
    from .review_layers import epoch
    import json
    current=epoch(store)
    fresh=[n for n in notes if json.loads(n.get('content_json') or '{}').get('_review_epoch',0)==current]
    selected=sorted(fresh,key=lambda n:{'session':0,'talk':1,'turn':2}.get(n['mode'],3))
    lines=['# '+(store.session().get('title') or store.session_id),'']
    if len(fresh)<len(notes):lines+=['部分旧纪要未包含当前人工修订，已从本次导出中暂缓；请重新生成总结。','']
    lines.extend(n.get('content_md','') for n in selected)
    lines+=['',(store.paths.root/'personal_notes.md').read_text(encoding='utf8')]
    text='\n\n'.join(lines)
    path=store.paths.root/'meeting_notes.md';temp=path.with_suffix('.tmp');temp.write_text(text,encoding='utf8');temp.replace(path)
    return text
