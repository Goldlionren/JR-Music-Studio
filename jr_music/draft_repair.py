"""Bounded repair of a frozen rejected draft, through the original Hermes route."""
import json
import re
from copy import deepcopy
from .store import require,canonical,sha,new_id,now,StoreError
from .score import parse_abc

MAX_ROUNDS=2
CODES={'SCORE_REQUIRES_NOTATION_REPAIR','SPEC_SCORE_MISMATCH','SPEC_LYRICS_MISMATCH',
       'ABC_LYRICS_MISMATCH','INVALID_PRODUCTION_SPECS','PRODUCTION_SPECS_REQUIRED',
       'SCORE_LYRIC_MAP_INVALID','SCORE_LYRIC_MAP_RANGE','SCORE_LYRIC_MAP_UNITS',
       'SCORE_LYRIC_MAP_TEXT','SCORE_LYRIC_MAP_OVERLAP','SCORE_LYRIC_MAP_INCOMPLETE'}

def received(store,pid,cid,db=None):
    if db:events=[json.loads(r[0]) for r in db.execute('SELECT document FROM events WHERE project_id=? ORDER BY sequence',(pid,))]
    else:events=store.events(pid)
    return next((e['result_sha256'] for e in reversed(events) if e['type'] in
        ('creation_result_received','direction_result_received') and e.get('command_id')==cid),None)

def read_blob(store,digest):
    with store.connect() as db:row=db.execute('SELECT content FROM blobs WHERE hash=?',(digest,)).fetchone()
    require(row is not None and sha(row[0])==digest,'REPAIR_EVIDENCE_MISMATCH')
    return json.loads(row[0])

def eligible(command):
    return (command['kind'] in ('compose','direction') and command['state']=='needs_attention'
        and command.get('issue') in CODES and not any(command.get(k) for k in ('render_id','result_revision_id','result_sha256'))
        and command.get('draft_repair',{}).get('round',0)<MAX_ROUNDS)

def diagnose(command,result):
    abc=result.get('abc',result.get('section_abc',''));lyrics=result.get('lyrics',result.get('section_lyrics',''))
    issues=[dict(code=command.get('issue'),message='服务器校验未通过，必须修正后重新完整校验。')]
    if not isinstance(abc,str):return dict(issues=issues)
    raw=abc.encode()
    try:score=parse_abc(raw)
    except ValueError as exc:
        issues.append(dict(code=getattr(exc,'code','INVALID_ABC_BYTES'),message='谱面无法建立解析结果，请检查原文及头部字段。'))
        return dict(issues=issues,notation_preview_only=True)
    for d in score['parse_diagnostics'][:24]:
        pos=d['byte_start'];line=raw[:pos].count(b'\n')+1
        issues.append(dict(code=d['code'],line=line,excerpt=abc.splitlines()[line-1][:180]))
    # A clearly labelled diagnostic preview exposes downstream inconsistencies;
    # it is not accepted as a new draft and never silently changes stored music.
    preview=re.sub(r'^\[(?=")','',abc,flags=re.M)
    preview=re.sub(r'^\[V:\s*([A-Za-z0-9_-]+)\]\s*$',r'V: \1',preview,flags=re.M)
    try:parsed=parse_abc(preview.encode())
    except ValueError:
        return dict(issues=issues,notation_preview_only=True)
    counts={v['voice_id']:sum(b['voice_id']==v['voice_id'] for b in parsed['bars']) for v in parsed['voices']}
    spec=result.get('production_specs');arr=spec.get('arr_spec') if isinstance(spec,dict) else None
    form=arr.get('form',[]) if isinstance(arr,dict) else []
    expected=sum(s.get('bars',0) for s in form) if isinstance(form,list) and all(isinstance(s,dict) and type(s.get('bars')) is int for s in form) else None
    if expected is not None and any(n!=expected for n in counts.values()):
        issues.append(dict(code='SPEC_SCORE_MISMATCH',message=f'仅修正明显记谱外壳后：实际小节 {counts}，规格总计 {expected}。依据确认方案统一结构，不能只改总数。'))
    if parsed['status']=='supported' and isinstance(lyrics,str):
        from .score_lyrics import validate
        for row in result.get('lyric_map',[]) if isinstance(result.get('lyric_map'),list) else []:
            try:validate(preview,lyrics,[row])
            except (StoreError,ValueError,TypeError,KeyError) as exc:
                issues.append(dict(code=getattr(exc,'code',str(exc)),lyric_line=row.get('line') if isinstance(row,dict) else None,
                    message='配谱范围、歌词或音符编号无效；休止不计入音符编号。'))
    return dict(issues=issues[:64],notation_preview_only=True,bar_counts=counts,spec_bars=expected)

def context(store,command):
    repair=command.get('draft_repair')
    if not repair:return None
    payload=read_blob(store,repair['source_result_sha256'])
    require(payload['project_id']==command['project_id'] and payload['command_id']==repair['source_command_id'],'REPAIR_EVIDENCE_MISMATCH')
    return dict(original_result=payload['result'],diagnostics=read_blob(store,repair['diagnostics_sha256']),
        preserve_lyrics=True,round=repair['round'],max_rounds=MAX_ROUNDS)

def inspection(store,command):
    if command.get('issue') not in CODES:return None
    digest=received(store,command['project_id'],command['command_id'])
    return diagnose(command,read_blob(store,digest)['result']) if digest else None

def verify_lyrics(store,command,result):
    value=context(store,command)
    if value:
        key='lyrics' if command.get('scope',{}).get('mode','song')=='song' else 'section_lyrics'
        require(result.get(key)==value['original_result'].get(key),'REPAIR_LYRICS_CHANGED')

def start(producer,pid,cid,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED')
    def reserve(db):
        source=producer._get(db,pid,cid)
        children=[c for c in producer._commands(db,pid).values() if c.get('retry_of')==cid]
        if children:
            require(children[0].get('draft_repair') is not None,'REPAIR_ALREADY_RETRIED')
            return children[0]
        require(eligible(source),'DRAFT_REPAIR_UNAVAILABLE')
        digest=received(producer.store,pid,cid,db);require(digest is not None,'REPAIR_SOURCE_MISSING')
        payload=read_blob(producer.store,digest)
        require(payload['command_id']==cid and payload['project_id']==pid,'REPAIR_EVIDENCE_MISMATCH')
        diagnosis=diagnose(source,payload['result'])
        diagnostic_hash=producer.store._blob(db,canonical(diagnosis))
        child=deepcopy(source)
        for field in ('progress','result','result_sha256','provenance','meter_repairs','format_recovery','lyric_density_review'):
            child.pop(field,None)
        previous=source.get('draft_repair',{})
        child.update(command_id=new_id('command'),retry_of=cid,state='queued',issue=None,
            created_at=now(),created_by=actor,result_revision_id=None,render_id=None,provenance=None,
            draft_repair=dict(source_command_id=cid,source_result_sha256=digest,diagnostics_sha256=diagnostic_hash,
                root_command_id=previous.get('root_command_id',cid),round=previous.get('round',0)+1,max_rounds=MAX_ROUNDS,
                authorized_by=actor))
        producer.store._event(db,pid,'draft_repair_requested',actor,dict(source_command_id=cid,
            command_id=child['command_id'],repair=child['draft_repair']))
        return producer._save(db,child,actor)
    initial=producer.store._operation(actor,key,dict(op='draft_repair',project_id=pid,command_id=cid),reserve)
    return producer.get(pid,initial['command_id'])

def resume(producer):
    # Initial click authorizes at most two repair attempts. No render retry here.
    commands=producer.list()
    parents={(c['project_id'],c.get('retry_of')) for c in commands}
    for c in commands:
        if (c['project_id'],c['command_id']) in parents:continue
        if c.get('draft_repair') and eligible(c) and received(producer.store,c['project_id'],c['command_id']):
            start(producer,c['project_id'],c['command_id'],actor='producer',key=c['command_id']+'_bounded_repair')
