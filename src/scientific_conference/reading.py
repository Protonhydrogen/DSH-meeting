"""Deterministic, lossless display sentences; ASR rows are never edited.

Times remain source-window bounds: no interpolated word timings are invented.
Character offsets refer to the selected segment text (not raw_text after edits).
"""
import re

END = re.compile(r'[.!?。！？]["\'’”）)\]]*$')
ABBREVIATION = re.compile(r'\b(?:Dr|Mr|Mrs|Ms|Prof|Fig|Figs|Eq|Eqs|vs|cf|al|et al|e\.g|i\.e|etc|approx|vol|no)\.$',re.I)
INITIAL = re.compile(r'(?:\b[A-Z]\.)+$')
PUNCTUATION = re.compile(r'[.!?。！？]+["\'’”）)\]]*')


def sentence_complete(text):
    text=text.strip()
    return bool(END.search(text)) and not (ABBREVIATION.search(text) or INITIAL.search(text))


def sentence_spans(text):
    start=0
    for match in PUNCTUATION.finditer(text):
        end=match.end(); token=match.group()
        if token.startswith('.'):
            # Decimal numbers, scientific identifiers and dotted abbreviations.
            if end<len(text) and not text[end].isspace():
                continue
            if not sentence_complete(text[start:end]):
                continue
        lo=start
        while lo<end and text[lo].isspace():lo+=1
        if lo<end:yield lo,end,True
        start=end
    lo=start
    while lo<len(text) and text[lo].isspace():lo+=1
    end=len(text.rstrip())
    if lo<end:yield lo,end,False


def reading_blocks(segments, *, closed=False):
    from .quality import suspect_segment
    blocks=[]
    for segment in segments:
        text=segment['text']
        anomalous=suspect_segment(segment)
        # An anomaly stays intact and visible for review, isolated from prose.
        spans=[(len(text)-len(text.lstrip()),len(text.rstrip()),False)] if anomalous and text.strip() else sentence_spans(text)
        for lo,hi,complete in spans:
            piece=text[lo:hi]
            if not piece:continue
            last=blocks[-1] if blocks else None
            join=bool(last and not last['_complete'] and not last['anomalous'] and not anomalous
                and not segment.get('speaker_boundary') and not last.get('speaker_boundary')
                and last['speaker_id']==segment.get('speaker_id') and last['talk_id']==segment.get('talk_id')
                and segment['start_ms']-last['end_ms']<=2500 and len(last['text'])<1200)
            source={k:segment[k] for k in ('segment_id','start_ms','end_ms')}
            source.update(text=piece,char_start=lo,char_end=hi)
            if segment.get('raw_text'):source.update(raw_text=segment['raw_text'],correction_reason=segment.get('correction_reason',''))
            if join:
                # Do not insert a space into a Chinese word split across windows.
                spacer='' if re.search(r'[\u3400-\u9fff]$',last['text']) and re.match(r'[\u3400-\u9fff]',piece) else ' '
                last['text']+=spacer+piece;last['end_ms']=max(last['end_ms'],segment['end_ms']);last['sources'].append(source);last['_complete']=complete
            else:
                blocks.append(dict(block_id=f"reading:{segment['segment_id']}:{lo}",text=piece,start_ms=segment['start_ms'],end_ms=segment['end_ms'],speaker_id=segment.get('speaker_id'),talk_id=segment.get('talk_id'),sources=[source],speaker_boundary=segment.get('speaker_boundary',False),anomalous=anomalous,timing_precision='source_window',_complete=complete))
    for i,b in enumerate(blocks):
        complete=b.pop('_complete')
        b['state']='Stable' if complete else ('Draft' if i==len(blocks)-1 and not closed else 'Incomplete sentence')
    return blocks
