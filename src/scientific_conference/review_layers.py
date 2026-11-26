"""Manual revision heads over immutable ASR and existing AI correction tables."""
import json
import re
from .db import utc_now

class ReviewConflict(ValueError):pass

def epoch(store):
    row=store.db.query_one("SELECT value FROM meta WHERE key='review:epoch'")
    return int(row['value']) if row else 0

def bump(conn):
    conn.execute("INSERT INTO meta VALUES ('review:epoch','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")

def record_ai(conn,session_id,segment_id,field,value):
    conn.execute('INSERT INTO review_history(session_id,segment_id,field,channel,value,action,created_utc) VALUES (?,?,?,?,?,?,?)',(session_id,segment_id,field,'ai',value,'generated',utc_now()))

def head(store,segment_id,field):
    row=store.db.query_one('SELECT * FROM review_heads WHERE session_id=? AND segment_id=? AND field=?',(store.session_id,segment_id,field))
    return dict(row) if row else dict(value=None,revision=0,undo_stack='[]')

def field_name(field):
    if field != 'transcript' and not re.fullmatch(r'translation:[a-z]{2,8}(?:-[a-z0-9]{2,8})?',field or ''):raise ValueError('Invalid review field')
    return field

def save_batch(store,items,*,limit=100):
    if not isinstance(items,list) or not 1<=len(items)<=limit:raise ValueError('Select 1–100 segments')
    seen=set()
    with store.db.tx() as conn:
        for item in items:
            sid=item.get('segment_id');field=field_name(item.get('field'));key=(sid,field)
            if key in seen:raise ValueError('Duplicate edit in batch')
            seen.add(key)
            if not conn.execute('SELECT 1 FROM segments WHERE session_id=? AND segment_id=? AND superseded_by IS NULL',(store.session_id,sid)).fetchone():raise ValueError('Segment not found or superseded')
            value=item.get('value')
            if not isinstance(value,str) or len(value)>30000:raise ValueError('Text must be at most 30,000 characters')
            old=head(store,sid,field)
            if field=='transcript':
                ai=conn.execute('SELECT corrected FROM transcript_edits WHERE session_id=? AND segment_id=?',(store.session_id,sid)).fetchone()
                if ai:record_ai(conn,store.session_id,sid,field,ai['corrected'])
            if type(item.get('revision')) is not int or old['revision']!=item['revision']:raise ReviewConflict('另一处已修改此段；草稿保留，请重新载入对照后保存。')
            stack=json.loads(old['undo_stack']);stack.append(old['value'])
            conn.execute('INSERT INTO review_heads VALUES (?,?,?,?,?,?) ON CONFLICT(session_id,segment_id,field) DO UPDATE SET value=excluded.value,revision=excluded.revision,undo_stack=excluded.undo_stack',(store.session_id,sid,field,value,old['revision']+1,json.dumps(stack)))
            conn.execute('INSERT INTO review_history(session_id,segment_id,field,channel,value,action,created_utc) VALUES (?,?,?,?,?,?,?)',(store.session_id,sid,field,'manual',value,'save',utc_now()))
        bump(conn)
    return dict(details(store,list(dict.fromkeys(i['segment_id'] for i in items))[:100]),affected_count=len(items))

def undo(store,segment_id,field,revision):
    field_name(field)
    with store.db.tx() as conn:
        if not conn.execute('SELECT 1 FROM segments WHERE session_id=? AND segment_id=? AND superseded_by IS NULL',(store.session_id,segment_id)).fetchone():raise ValueError('Segment not found or superseded')
        old=head(store,segment_id,field)
        if type(revision) is not int or old['revision']!=revision:raise ReviewConflict('修改版本已变化；草稿保留，请重新载入。')
        stack=json.loads(old['undo_stack'])
        if not stack:raise ValueError('没有可撤销的人工修改')
        value=stack.pop()
        conn.execute('UPDATE review_heads SET value=?,revision=revision+1,undo_stack=? WHERE session_id=? AND segment_id=? AND field=?',(value,json.dumps(stack),store.session_id,segment_id,field))
        conn.execute('INSERT INTO review_history(session_id,segment_id,field,channel,value,action,created_utc) VALUES (?,?,?,?,?,?,?)',(store.session_id,segment_id,field,'manual',value,'undo',utc_now()))
        bump(conn)
    return details(store,[segment_id])

def overlay(store,rows):
    overrides={}
    for h in store.db.query('SELECT * FROM review_heads WHERE session_id=?',(store.session_id,)):
        overrides.setdefault(h['segment_id'],{})[h['field']]=dict(h)
    for row in rows:
        fields=overrides.get(row['segment_id'],{})
        if fields:row['review_revision']={f:h['revision'] for f,h in fields.items()}
        transcript=fields.get('transcript',{})
        if transcript.get('value') is not None:
            row['raw_text']=row.get('raw_text',row['text']);row['ai_text']=row['text'];row['text']=transcript['value'];row['human_confirmed']=True
        confirmed={f.split(':',1)[1]:h['value'] for f,h in fields.items() if f.startswith('translation:') and h['value'] is not None}
        if confirmed:row['confirmed_translations']=confirmed
    return rows

def translation_overlay(store,row):
    if row is None:return None
    row=dict(row);h=head(store,row['segment_id'],'translation:'+row['target_lang'])
    source=head(store,row['segment_id'],'transcript')
    saved=store.db.query_one('SELECT value FROM meta WHERE key=?',('translation_review:'+row['target_lang']+':'+row['segment_id'],))
    row['stale_source']=(int(saved['value']) if saved else 0)<source['revision']
    if h['value'] is not None:row.update(auto_text=row['text'],text=h['value'],human_confirmed=True,state='ok',review_revision=h['revision'],stale_source=False)
    return row

def details(store,ids):
    if not isinstance(ids,list) or not 1<=len(ids)<=100 or not all(isinstance(x,str) for x in ids):raise ValueError('Invalid segment selection')
    effective={s['segment_id']:s for s in store.segments()}
    result=[]
    for sid in ids:
        raw=store.segment(sid)
        if raw is None or sid not in effective:raise ValueError('Segment not found or superseded')
        ai=store.db.query_one('SELECT * FROM transcript_edits WHERE session_id=? AND segment_id=?',(store.session_id,sid))
        fields={h['field']:dict(h) for h in store.db.query('SELECT * FROM review_heads WHERE session_id=? AND segment_id=?',(store.session_id,sid))}
        history=[dict(h) for h in store.db.query('SELECT * FROM review_history WHERE session_id=? AND segment_id=? ORDER BY id',(store.session_id,sid))]
        result.append(dict(effective[sid],asr_text=raw['text'],ai_text=ai['corrected'] if ai and ai['original']==raw['text'] else raw['text'],heads=fields,history=history,translations=[dict(t) for t in store.translations() if t['segment_id']==sid]))
    return dict(segments=result,review_epoch=epoch(store))

def signature(segment):
    return json.dumps([segment.get('revision',1),segment['text'],segment.get('speaker_id'),segment.get('review_revision',{}),segment.get('confirmed_translations',{})],ensure_ascii=False,sort_keys=True)
