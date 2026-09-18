"""Versioned lyric-to-score declarations. Never a claim about rendered audio."""
from collections import Counter,defaultdict
from fractions import Fraction
import json
import re
from .store import require,canonical,sha,new_id,StoreError
from .score import parse_abc,PARSER_VERSION
from .lyric_timeline import normalized


def lines(lyrics):
    from .direction import lyric_sections
    result=[];sections=lyric_sections(lyrics)
    if not sections:sections=[dict(label='unmarked',start=0,end=len(lyrics))]
    elif sections[0]['start']>0:sections.insert(0,dict(label='unmarked',start=0,end=sections[0]['start']))
    for section in sections:
        position=0
        for raw in lyrics[section['start']:section['end']].splitlines():
            value=re.sub(r'\[[^\]]*\]','',raw).strip()
            if not normalized(value):continue
            position+=1
            result.append(dict(line=len(result)+1,text=value,section=section['label'],section_line=position))
    return result


def geometry(abc):
    score=parse_abc(abc.encode());notes=[];lookup={};clock=defaultdict(Fraction);counts=Counter();order=Counter()
    tempo=re.fullmatch(r'1/4=([1-9]\d*)',score['headers'].get('Q',''))
    seconds=Fraction(240,int(tempo[1])) if tempo else None
    bars={b['bar_id']:b for b in score['bars']};sections={s['section_id']:s['suggested_label'] for s in score['sections']}
    for e in score['events']:
        voice=e['voice_id'];start=clock[voice];duration=Fraction(**e['duration']);clock[voice]+=duration
        if e['kind']!='note':continue
        counts[e['bar_id']]+=1;order[voice]+=1
        row=dict(voice=voice,bar=bars[e['bar_id']]['ordinal'],note=counts[e['bar_id']],bar_id=e['bar_id'],event_id=e['event_id'],
            pitch=e['pitch'],section=sections[e['section_id']],order=order[voice],tie_in=e.get('tie_in',False),
            start=float(start*seconds) if seconds else None,end=float((start+duration)*seconds) if seconds else None)
        notes.append(row);lookup[(voice,row['bar'],row['note'])]=row
    return score,notes,lookup


def validate(abc,lyrics,rows,*,complete=False):
    score,notes,lookup=geometry(abc);source=lines(lyrics);seen=set();clean=[]
    validate_abc_lyrics(abc,lyrics)
    require(isinstance(rows,list) and len(rows)<=len(source),'SCORE_LYRIC_MAP_INVALID')
    require(not rows or all(d['code']=='BAR_DURATION_MISMATCH' for d in score['parse_diagnostics']),'SCORE_LYRIC_MAP_UNSUPPORTED')
    last_by_voice={}
    def interval(voice,value):
        require(isinstance(value,list) and len(value)==4 and all(type(n) is int and n>0 for n in value),'SCORE_LYRIC_MAP_RANGE')
        a=lookup.get((voice,*value[:2]));b=lookup.get((voice,*value[2:]));require(a and b and a['order']<=b['order'] and not a['tie_in'],'SCORE_LYRIC_MAP_RANGE')
        return a,b
    for row in sorted(rows,key=lambda r:r.get('line',0) if isinstance(r,dict) else 0):
        require(isinstance(row,dict) and set(row) in ({'line','voice','units'},{'line','voice','units','range'}),'SCORE_LYRIC_MAP_INVALID')
        number=row['line'];voice=row['voice'];units=row['units']
        require(type(number) is int and 1<=number<=len(source) and number not in seen and isinstance(voice,str),'SCORE_LYRIC_MAP_INVALID');seen.add(number)
        require(isinstance(units,list) and len(units)<=len(source[number-1]['text']),'SCORE_LYRIC_MAP_INVALID')
        previous=0;unit_rows=[]
        for unit in units:
            require(isinstance(unit,list) and len(unit)==5 and isinstance(unit[0],str) and normalized(unit[0]),'SCORE_LYRIC_MAP_UNITS')
            a,b=interval(voice,unit[1:]);require(a['order']>previous,'SCORE_LYRIC_MAP_OVERLAP');previous=b['order']
            unit_rows.append(list(unit))
        if unit_rows:
            require(normalized(''.join(u[0] for u in units))==normalized(source[number-1]['text']),'SCORE_LYRIC_MAP_TEXT')
            bounds=unit_rows[0][1:3]+unit_rows[-1][3:5]
            require(row.get('range') in (None,bounds),'SCORE_LYRIC_MAP_RANGE')
        else:
            require(not complete,'SCORE_LYRIC_MAP_INCOMPLETE');bounds=row.get('range')
        a,b=interval(voice,bounds)
        require(a['order']>last_by_voice.get(voice,0),'SCORE_LYRIC_MAP_OVERLAP');last_by_voice[voice]=b['order']
        clean.append(dict(line=number,voice=voice,range=bounds,units=unit_rows))
    require(not complete or len(clean)==len(source),'SCORE_LYRIC_MAP_INCOMPLETE')
    return clean


def validate_abc_lyrics(abc,lyrics):
    """Prevent an ABC lyric channel from silently contradicting the render lyrics.

    Only text consistency is asserted here, not syllable timing or performance.
    ABC hyphens, melisma underscores and skip markers are notation, not words.
    """
    embedded=re.findall(r'^w:[ \t]*(.*)$',abc,re.M)
    if embedded:
        require(normalized(''.join(embedded))==normalized(''.join(l['text'] for l in lines(lyrics))),
                'ABC_LYRICS_MISMATCH')


def records(db,pid,rid):
    return [e for e in (json.loads(r[0]) for r in db.execute('SELECT document FROM events WHERE project_id=? ORDER BY sequence',(pid,)))
        if e['type']=='score_lyric_map' and e['revision_id']==rid]


def manifest(db,record):
    raw=db.execute('SELECT content FROM blobs WHERE hash=?',(record['manifest_sha256'],)).fetchone()[0]
    require(sha(raw)==record['manifest_sha256'],'CORRUPT_SCORE_LYRIC_MAP');return json.loads(raw)


def put(store,db,revision,rows,actor,origin,inherited_from=None):
    history=records(db,revision['project_id'],revision['revision_id']);snapshot=store._snapshot(db,revision)
    value=dict(schema_version='score-lyrics/1',mapping_id=new_id('mapping'),revision_id=revision['revision_id'],
        snapshot_sha256=revision['snapshot_sha256'],abc_sha256=revision['abc_sha256'],lyrics_sha256=sha(snapshot['brief']['lyrics'].encode()),
        parser_version=PARSER_VERSION,generation=len(history)+1,origin=origin,declared_by=actor,rows=rows)
    if inherited_from:value['inherited_from']=inherited_from
    digest=store._blob(db,canonical(value))
    store._event(db,revision['project_id'],'score_lyric_map',actor,dict(revision_id=revision['revision_id'],manifest_sha256=digest,generation=value['generation']))
    return value


def read(store,pid,rid):
    source=store.get_revision(pid,rid);snapshot=source['snapshot'];score,notes,lookup=geometry(snapshot['abc']);lyric=lines(snapshot['brief']['lyrics'])
    with store.connect() as db:
        history=records(db,pid,rid);current=manifest(db,history[-1]) if history else None
    rows=[]
    if current:
        require(current['snapshot_sha256']==source['revision']['snapshot_sha256'] and current['abc_sha256']==source['revision']['abc_sha256'] and
            current['lyrics_sha256']==sha(snapshot['brief']['lyrics'].encode()),'CORRUPT_SCORE_LYRIC_MAP')
        rows=validate(snapshot['abc'],snapshot['brief']['lyrics'],current['rows'])
    by={r['line']:r for r in rows};items=[]
    for line in lyric:
        row=by.get(line['line']);item=dict(line,mapping=row,bar_ids=[],start=None,end=None)
        if row:
            a=lookup[(row['voice'],*row['range'][:2])];b=lookup[(row['voice'],*row['range'][2:])]
            item.update(bar_ids=[bar['bar_id'] for bar in score['bars'] if bar['voice_id']==row['voice'] and a['bar']<=bar['ordinal']<=b['bar']],
                start=a['start'],end=b['end'],level='word_groups' if row['units'] else 'phrase',
                unit_positions=[dict(text=u[0],start=lookup[(row['voice'],u[1],u[2])]['start'],end=lookup[(row['voice'],u[3],u[4])]['end'],
                    start_bar=u[1],start_note=u[2],end_bar=u[3],end_note=u[4],
                    start_event_id=lookup[(row['voice'],u[1],u[2])]['event_id'],end_event_id=lookup[(row['voice'],u[3],u[4])]['event_id']) for u in row['units']])
        items.append(item)
    return dict(schema_version='score-lyrics/1',generation=len(history),origin=current['origin'] if current else None,
        mapping_id=current['mapping_id'] if current else None,items=items,notes=notes,rows=rows,unmapped_count=len(lyric)-len(rows),
        editable=all(d['code']=='BAR_DURATION_MISMATCH' for d in score['parse_diagnostics']),
        semantics='Declared composition alignment. Times are calculated from the input score, never measured audio times.')


def save(store,pid,rid,expected_snapshot_sha256,expected_generation,rows,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED');source=store.get_revision(pid,rid)
    require(source['revision']['snapshot_sha256']==expected_snapshot_sha256,'STALE_BASE')
    clean=validate(source['snapshot']['abc'],source['snapshot']['brief']['lyrics'],rows)
    def action(db):
        require(type(expected_generation) is int and len(records(db,pid,rid))==expected_generation,'SCORE_LYRIC_MAP_CONFLICT')
        return put(store,db,source['revision'],clean,actor,'producer_declared')
    return store._operation(actor,key,dict(op='save_score_lyrics',project_id=pid,revision_id=rid,snapshot_sha256=expected_snapshot_sha256,generation=expected_generation,rows=rows),action)


def attach(store,pid,rid,rows,*,actor,key):
    source=store.get_revision(pid,rid);clean=validate(source['snapshot']['abc'],source['snapshot']['brief']['lyrics'],rows)
    def action(db):
        history=records(db,pid,rid)
        require(not history or manifest(db,history[-1])['origin']=='inherited','SCORE_LYRIC_MAP_CONFLICT')
        return put(store,db,source['revision'],clean,actor,'composer_declared')
    return store._operation(actor,key,dict(op='attach_score_lyrics',project_id=pid,revision_id=rid,rows=clean),action)


def rebase(source,target,rows):
    """Retain only unchanged lyric sections with unchanged rhythmic topology."""
    old,_,old_notes=geometry(source['abc']);new,_,new_notes=geometry(target['abc'])
    old_lines=lines(source['brief']['lyrics']);new_lines=lines(target['brief']['lyrics'])
    line_keys={(l['section'],l['section_line'],l['text']):l['line'] for l in new_lines}
    def section_bars(score):
        labels={s['section_id']:s['suggested_label'] for s in score['sections']};result=defaultdict(list)
        for b in score['bars']:result[(labels[b['section_id']],b['voice_id'])].append(b)
        return result
    old_bars=section_bars(old);new_bars=section_bars(new)
    def shape(score,bars):
        ids={b['bar_id'] for b in bars}
        return [[(e['kind'],e['duration'],e.get('tie_in'),e.get('tie_out')) for e in score['events'] if e['bar_id']==b['bar_id']] for b in bars]
    result=[]
    for row in rows:
        line=old_lines[row['line']-1];number=line_keys.get((line['section'],line['section_line'],line['text']))
        if number is None:continue
        def address(bar,note):
            event=old_notes[(row['voice'],bar,note)];key=(event['section'],row['voice'])
            ob=old_bars[key];nb=new_bars.get(key,[])
            require(shape(old,ob)==shape(new,nb),'MAPPING_TOPOLOGY_CHANGED')
            position=next(i for i,b in enumerate(ob) if b['ordinal']==bar)
            target_bar=nb[position]['ordinal'];require((row['voice'],target_bar,note) in new_notes,'MAPPING_TOPOLOGY_CHANGED')
            return [target_bar,note]
        try:
            bounds=address(*row['range'][:2])+address(*row['range'][2:])
            units=[[u[0],*address(u[1],u[2]),*address(u[3],u[4])] for u in row['units']]
            candidate=dict(line=number,voice=row['voice'],range=bounds,units=units)
            validate(target['abc'],target['brief']['lyrics'],result+[candidate]);result.append(candidate)
        except (StoreError,KeyError,StopIteration):continue
    return result


def inherit_on_insert(store,db,revision):
    parent_id=revision['parent_revision_id']
    if not parent_id:return
    history=records(db,revision['project_id'],parent_id)
    if not history:return
    parent=store._revision(db,revision['project_id'],parent_id)
    current=manifest(db,history[-1])
    try:rows=rebase(store._snapshot(db,parent),store._snapshot(db,revision),current['rows'])
    except (ValueError,StoreError,KeyError):rows=[]
    # Freeze even an empty result: later changes to the parent must not back-propagate.
    put(store,db,revision,rows,revision['author_id'],'inherited',dict(revision_id=parent_id,manifest_sha256=history[-1]['manifest_sha256']))
