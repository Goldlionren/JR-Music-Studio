"""One producer-authorized revision, automatic remeasurement, then human review."""
import json
import threading
from .store import require, canonical, sha, new_id, now
from . import quality, audio_analysis

LOCK=threading.RLock()


def records(store,pid,db=None):
    if db:
        events=[json.loads(r[0]) for r in db.execute('SELECT document FROM events WHERE project_id=? ORDER BY sequence',(pid,))]
    else:events=store.events(pid)
    current={}
    for event in events:
        if event['type']=='quality_cycle':current[event['cycle']['cycle_id']]=event['cycle']
    return current


def get(producer,pid,cid):
    result=records(producer.store,pid).get(cid)
    require(result is not None,'QUALITY_CYCLE_NOT_FOUND')
    return result


def blob(store,digest):
    with store.connect() as db:row=db.execute('SELECT content FROM blobs WHERE hash=?',(digest,)).fetchone()
    require(row is not None and sha(row[0])==digest,'QUALITY_EVIDENCE_CORRUPT')
    return json.loads(row[0])


def update(producer,pid,cid,**values):
    with producer.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        cycle=records(producer.store,pid,db)[cid]
        cycle.update(values,updated_at=now())
        producer.store._event(db,pid,'quality_cycle','quality_coordinator',dict(cycle=cycle))
        db.commit()
    return cycle


def list_for_render(producer,pid,rid):
    result=[]
    for cycle in records(producer.store,pid).values():
        if rid not in (cycle['source_render_id'],cycle.get('result_render_id')):continue
        value=dict(cycle)
        if cycle.get('after_sha256'):
            value['comparison']=quality.compare(blob(producer.store,cycle['before_sha256']),blob(producer.store,cycle['after_sha256']))
        result.append(value)
    return result


def instruction(report,codes,note):
    selected=[c for c in report['checks'] if c['code'] in codes]
    lines=['本次是一轮听感质量修订。问题已由制作人选择；证据是启发测量或试听意见，不是客观缺陷定论。',
        '以本任务冻结的 Terry 专业技能为主要方法，先读 mc-workflow / lw-workflow。先区分后端未按意图执行与词曲规格设计问题；合理例外写理由。',
        '每一项按对应技能修改并同步 ARR-SPEC、LYR-SPEC、ABC、歌词与配谱。只生成一个候选，不调用工具渲染，不自动 Accept，不宣称已经听过新结果。']
    for item in selected:
        refs='、'.join(s['skill']+' '+s['section'] for s in item['sources'])
        lines.append(item['label']+'：'+item['evidence']+'\n方法：'+refs+'\n修改目标：'+item['instruction'])
    lines += ['制作人补充：'+note,
        '完成后 summary 简述实际改动和无法满足的目标。输入谱修改不证明成品遵循；JR 会对新音频复核，最终听感由制作人判断。']
    result='\n\n'.join(lines)
    require(len(result)<=4000,'QUALITY_INSTRUCTION_TOO_LONG')
    return result


def start(producer,pid,rid,expected_report_sha256,codes,assigned_to,preserve,note,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED')
    require(producer.bridge and assigned_to in producer.bridge.bindings,'HERMES_UNAVAILABLE')
    require(isinstance(codes,list) and 0<len(codes)<=6 and len(set(codes))==len(codes)
            and all(isinstance(c,str) for c in codes),'INVALID_QUALITY_SELECTION')
    require(isinstance(note,str) and len(note)<=800,'INVALID_TEXT')
    from .constraints import validate_locks
    preserve=validate_locks(preserve)
    require(not ('density' in codes and {'lyrics','rhythm'}<=set(preserve)),'QUALITY_LOCK_CONFLICT')
    request=dict(op='quality_cycle_start',project_id=pid,render_id=rid,expected_report_sha256=expected_report_sha256,
                 codes=codes,assigned_to=assigned_to,preserve=preserve,note=note)
    with LOCK:
        def reserve(db):
            report=quality.read(producer,pid,rid)
            require(report['report_sha256']==expected_report_sha256,'CHECK_REPORT_CHANGED')
            available={c['code']:c for c in report['checks']}
            require(set(codes)<=set(available),'INVALID_QUALITY_SELECTION')
            require(not ('density' in codes and report['policy']['values']['delivery']=='rap_or_fast'),'QUALITY_INTENT_CONFLICT')
            require(not any(c['source_render_id']==rid and c['state']!='closed' for c in records(producer.store,pid,db).values()),'QUALITY_CYCLE_ACTIVE')
            report_hash=producer.store._blob(db,canonical(report))
            cycle=dict(schema_version='quality-cycle/1',cycle_id=new_id('quality'),project_id=pid,
                source_render_id=rid,source_revision_id=report['revision_id'],source_snapshot_sha256=report['snapshot_sha256'],
                before_sha256=report_hash,policy=report['policy'],codes=codes,preserve=preserve,note=note,assigned_to=assigned_to,
                mcp_alias=producer.bridge.selection(assigned_to).get('mcp_alias'),
                instruction=instruction(report,codes,note),state='preparing',command_id=None,result_render_id=None,
                after_sha256=None,budget=dict(revisions=1,automatic_retries=0),created_at=now(),updated_at=now(),issue=None)
            producer.store._event(db,pid,'quality_cycle',actor,dict(cycle=cycle))
            return cycle
        cycle=producer.store._operation(actor,key,request,reserve)
        return advance(producer,pid,cycle['cycle_id'])


def advance(producer,pid,cid,*,retry_analysis=False):
    with LOCK:
        cycle=get(producer,pid,cid)
        if cycle['state'] in ('closed','awaiting_review'):return cycle
        if not cycle['command_id']:
            command=producer.create(pid,cycle['source_revision_id'],cycle['source_snapshot_sha256'],cycle['assigned_to'],
                'direction',cycle['instruction'],[],actor='producer',key=cid+'_revision',scope={'mode':'song'},
                preserve=cycle['preserve'],songcraft_selection='professional',mcp_alias=cycle['mcp_alias'])
            cycle=update(producer,pid,cid,command_id=command['command_id'],state='revising',issue=None)
        command=producer.get(pid,cycle['command_id'])
        # Follow only a retry already explicitly created by the producer. Never
        # create retries here, or replace a command that may have rendered.
        if command['state']=='needs_attention' and not command.get('render_id'):
            child=next((c for c in producer.list(pid) if c.get('retry_of')==command['command_id']),None)
            if child:
                cycle=update(producer,pid,cid,command_id=child['command_id'],state='revising',issue=None,
                    previous_command_ids=cycle.get('previous_command_ids',[])+[command['command_id']])
                command=child
        if command['state'] in ('needs_attention','cancelled'):
            return update(producer,pid,cid,state='needs_attention',issue=command.get('issue') or command['state'])
        if command['state']!='completed':
            if cycle['state']=='needs_attention':return update(producer,pid,cid,state='revising',issue=None)
            return cycle
        rid=command['render_id']
        require(rid is not None and command['result_revision_id']!=cycle['source_revision_id'],'QUALITY_RESULT_REQUIRED')
        if cycle['result_render_id']!=rid:cycle=update(producer,pid,cid,result_render_id=rid,state='analyzing',issue=None)
        # Existing full analyses can be reused; otherwise lyrics/waveform run on
        # local CPU, without another Comfy submission or dependence on SheetSage.
        analysis=audio_analysis.start(producer,pid,rid,cycle['assigned_to'],actor='producer',key=cid+'_analysis',mode='lyrics')
        if retry_analysis and analysis['state']=='needs_attention' and analysis.get('analysis_mode')=='lyrics':
            audio_analysis.resume(producer,pid,analysis['command_id'])
            return update(producer,pid,cid,state='analyzing',issue=None)
        if analysis['state']=='needs_attention':
            return update(producer,pid,cid,state='needs_attention',issue=analysis.get('issue') or 'QUALITY_ANALYSIS_FAILED')
        if analysis['state']!='completed':return cycle
        report=quality.read(producer,pid,rid,frozen_policy=cycle['policy'])
        with producer.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            digest=producer.store._blob(db,canonical(report));db.commit()
        return update(producer,pid,cid,after_sha256=digest,state='awaiting_review',issue=None)


def resume_pending(producer,pid=None):
    """Called on explicit refresh and worker heartbeat; never auto-retries generation."""
    projects=[dict(project_id=pid)] if pid else producer.store.list_projects()
    for project in projects:
        project_id=project['project_id']
        for cycle in records(producer.store,project_id).values():
            if cycle['state']=='needs_attention' and cycle.get('command_id') and not cycle.get('result_render_id'):
                command=producer.get(project_id,cycle['command_id'])
                retry=any(c.get('retry_of')==command['command_id'] for c in producer.list(project_id))
                if command['state'] not in ('queued','working','rendering','completed') and not retry:continue
            elif cycle['state'] not in ('preparing','revising','analyzing'):continue
            try:advance(producer,project_id,cycle['cycle_id'])
            except Exception as exc:
                update(producer,project_id,cycle['cycle_id'],state='needs_attention',
                       issue=getattr(exc,'code',None) or type(exc).__name__.upper())


def decide(producer,pid,cid,verdict,note,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED')
    require(verdict in ('improved','worse','unclear','abandoned'),'INVALID_QUALITY_VERDICT')
    require(isinstance(note,str) and len(note)<=2000,'INVALID_TEXT')
    def save(db):
        cycle=records(producer.store,pid,db).get(cid)
        require(cycle is not None,'QUALITY_CYCLE_NOT_FOUND')
        require(cycle['state']=='awaiting_review' or (cycle['state']=='needs_attention' and verdict=='abandoned'),'QUALITY_REVIEW_NOT_READY')
        cycle.update(state='closed',verdict=verdict,review_note=note,reviewed_by=actor,updated_at=now())
        producer.store._event(db,pid,'quality_cycle',actor,dict(cycle=cycle))
        return cycle
    return producer.store._operation(actor,key,dict(op='quality_cycle_decide',project_id=pid,cycle_id=cid,verdict=verdict,note=note),save)


def route(producer,pid,rid,action,method,body,*,actor):
    if action=='quality' and method=='GET':
        return dict(report=quality.read(producer,pid,rid),cycles=list_for_render(producer,pid,rid))
    require(method=='POST','INVALID_ROUTE')
    require(actor=='producer','PRODUCER_REQUIRED')
    if action=='quality-policy':
        value=body(('values','expected_generation','idempotency_key'))
        return quality.save_policy(producer,pid,rid,actor=actor,key=value.pop('idempotency_key'),**value)
    if action=='quality-revise':
        value=body(('expected_report_sha256','codes','assigned_to','preserve','note','idempotency_key'))
        return start(producer,pid,rid,actor=actor,key=value.pop('idempotency_key'),**value)
    if action=='quality-analyze':
        value=body(('assigned_to','idempotency_key'))
        return audio_analysis.start(producer,pid,rid,value['assigned_to'],actor=actor,key=value['idempotency_key'],mode='lyrics')
    if action in ('quality-refresh','quality-review'):
        required=('cycle_id','verdict','note','idempotency_key') if action=='quality-review' else ('cycle_id','idempotency_key')
        value=body(required);cycle=get(producer,pid,value['cycle_id'])
        require(rid in (cycle['source_render_id'],cycle.get('result_render_id')),'QUALITY_CYCLE_NOT_FOUND')
        if action=='quality-refresh':return advance(producer,pid,cycle['cycle_id'],retry_analysis=True)
        return decide(producer,pid,value['cycle_id'],value['verdict'],value['note'],actor=actor,key=value['idempotency_key'])
    require(False,'INVALID_ROUTE')
