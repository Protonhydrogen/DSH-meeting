"""Read bounded PCM clips on the existing absolute audio timeline."""
import io
import wave
from pathlib import Path


def audio_clip(store,start_ms,end_ms):
    if type(start_ms) is not int or type(end_ms) is not int or not 0<=start_ms<end_ms or end_ms-start_ms>60000:
        raise ValueError('Audio clip must cover 1–60,000 ms')
    rows=store.db.query('SELECT * FROM audio_chunks WHERE end_ms>? AND start_ms<? ORDER BY chunk_index',(start_ms,end_ms))
    if not rows:raise FileNotFoundError('此时间点没有保存的音频')
    rate=16000;pcm=bytearray((end_ms-start_ms)*rate//1000*2);found=False
    root=store.paths.root.resolve()
    for row in rows:
        name=row['converted_path']
        if not name:continue
        path=(root/Path(name)).resolve()
        if not path.is_relative_to(root):raise ValueError('Audio path is outside the meeting')
        if not path.is_file():continue
        with wave.open(str(path),'rb') as wav:
            if (wav.getframerate(),wav.getnchannels(),wav.getsampwidth())!=(rate,1,2):raise ValueError('Playback requires the saved 16 kHz mono PCM audio')
            lo=max(start_ms,row['start_ms']);hi=min(end_ms,row['end_ms'])
            offset=(lo-row['start_ms'])*rate//1000
            if offset>=wav.getnframes():continue
            wav.setpos(offset);chunk=wav.readframes((hi-lo)*rate//1000)
            dest=(lo-start_ms)*rate//1000*2;pcm[dest:dest+len(chunk)]=chunk;found=True
    if not found:raise FileNotFoundError('音频文件不可用；转录与笔记仍保留')
    buf=io.BytesIO()
    with wave.open(buf,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(rate);wav.writeframes(pcm)
    return buf.getvalue()
