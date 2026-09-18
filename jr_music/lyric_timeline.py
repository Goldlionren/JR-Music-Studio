"""Estimated alignment and independently versioned, human corrected audio times."""
from difflib import SequenceMatcher
import math
import re
from .store import require,canonical,sha


def normalized(text):return ''.join(c.lower() for c in text if c.isalnum())


def lyric_lines(lyrics):
    rows=[];section='未分段'
    for raw in lyrics.splitlines():
        raw=raw.strip()
        if not raw:continue
        if re.fullmatch(r'\[.*\]',raw):
            if re.fullmatch(r'\[(?:intro|verse|pre[-_ ]?chorus|chorus|bridge|outro|主歌|副歌|前奏|尾奏|桥段|预副歌)(?:[ _-]*\d+)?\]',raw,re.I):section=raw[1:-1]
            continue
        content=re.sub(r'\[[^\]]*\]','',raw).strip()
        if not normalized(content):continue
        rows.append(dict(line_id=f'line_{len(rows)+1}',text=content,section=section,start=None,end=None,basis='unmatched',coverage=0))
    return rows


def estimate(lyrics,asr):
    lines=lyric_lines(lyrics);letters=[];times=[]
    for segment in asr.get('segments',[]):
        for word in segment.get('words',[]):
            value=normalized(word['text'])
            for i,char in enumerate(value):
                letters.append(char);times.append((word['start']+(word['end']-word['start'])*i/len(value),word['start']+(word['end']-word['start'])*(i+1)/len(value)))
    expected=''.join(normalized(line['text']) for line in lines);heard=''.join(letters)
    matches={}
    for block in SequenceMatcher(None,expected,heard,autojunk=False).get_matching_blocks():
        for i in range(block.size):matches[block.a+i]=block.b+i
    offset=0
    for line in lines:
        length=len(normalized(line['text']));found=[matches[i] for i in range(offset,offset+length) if i in matches]
        line['coverage']=round(len(found)/length,3);offset+=length
        # Very sparse matches do not establish a usable lyric position.
        if found and len(found)>=min(3,length) and line['coverage']>=0.5:
            line.update(start=round(times[min(found)][0],2),end=round(times[max(found)][1],2),basis='asr_estimate')
    return lines


def saved(store,pid,render_id):
    return [e for e in store.events(pid) if e['type']=='lyric_timeline' and e['render_id']==render_id]


def read(producer,pid,render_id):
    from . import audio_analysis
    job=producer.renders.get(pid,render_id);snapshot=producer.store.get_revision(pid,job['revision_id'])['snapshot']
    records=saved(producer.store,pid,render_id)
    if records:return dict(generation=len(records),lines=records[-1]['lines'],basis='human_corrected',audio_sha256=records[-1]['audio_sha256'])
    report=audio_analysis.read(producer,pid,render_id)
    return dict(generation=0,lines=estimate(snapshot['brief'].get('lyrics',''),report.get('asr',{})),basis='asr_estimate',
        audio_sha256=next(a['sha256'] for a in job['assets'] if a['media_type']=='audio/wav'))


def save(producer,pid,render_id,lines,expected_generation,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED')
    job=producer.renders.get(pid,render_id);require(job['state']=='succeeded','AUDIO_NOT_READY')
    original=lyric_lines(producer.store.get_revision(pid,job['revision_id'])['snapshot']['brief'].get('lyrics',''))
    before=read(producer,pid,render_id)['lines']
    require(isinstance(lines,list) and len(lines)==len(original),'INVALID_TIMELINE')
    duration=job['verification']['duration_seconds'];last=-1;clean=[]
    for row,source,prior in zip(lines,original,before):
        require(set(row)=={'line_id','start','end'} and row['line_id']==source['line_id'],'INVALID_TIMELINE')
        start,end=row['start'],row['end']
        if start is not None or end is not None:
            require(all(type(v) in (int,float) and math.isfinite(v) for v in (start,end)) and 0<=start<end<=duration and start>=last,'INVALID_TIMELINE')
            last=start
        basis=prior['basis'] if start==prior['start'] and end==prior['end'] else 'manual' if start is not None else 'unmatched'
        clean.append(dict(source,start=start,end=end,basis=basis,coverage=prior['coverage']))
    audio_sha=next(a['sha256'] for a in job['assets'] if a['media_type']=='audio/wav')
    def action(db):
        rows=db.execute('SELECT document FROM events WHERE project_id=?',(pid,))
        import json
        count=sum(e['type']=='lyric_timeline' and e.get('render_id')==render_id for e in (json.loads(r[0]) for r in rows))
        require(type(expected_generation) is int and count==expected_generation,'TIMELINE_CONFLICT')
        producer.store._event(db,pid,'lyric_timeline',actor,dict(render_id=render_id,revision_id=job['revision_id'],audio_sha256=audio_sha,lines=clean,generation=count+1))
        return dict(generation=count+1,lines=clean,basis='human_corrected',audio_sha256=audio_sha)
    return producer.store._operation(actor,key,dict(op='lyric_timeline',project_id=pid,render_id=render_id,lines=lines,expected_generation=expected_generation),action)
