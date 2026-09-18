"""Small, authenticated progress reports and explicit retry lineage."""
from copy import deepcopy
from .store import require, text, new_id, now
from .creative_format import REPAIR_REVISION

RETRYABLE = {'NO_MUSICAL_CHANGE','TIMEOUTEXPIRED','HERMES_PROVIDER_TIMEOUT','INVALID_CREATIVE_RESPONSE_JSON','INVALID_CREATIVE_RESPONSE_FIELDS',
    'CREATIVE_JSON_DUPLICATE_KEY','CREATIVE_FORMAT_AMBIGUOUS','CREATIVE_RESPONSE_TRUNCATED','CREATIVE_FORMAT_COMPLETION_UNVERIFIED','CREATIVE_RECOVERY_SOURCE_MISSING',
    'PRODUCTION_SPECS_REQUIRED','INVALID_PRODUCTION_SPECS','SPEC_SCORE_MISMATCH','SPEC_LYRICS_MISMATCH','ABC_LYRICS_MISMATCH',
    'LYRIC_DENSITY_LIMIT','LYRIC_DENSITY_UNMEASURABLE',
    'SCORE_LYRIC_MAP_INVALID','SCORE_LYRIC_MAP_RANGE','SCORE_LYRIC_MAP_UNITS','SCORE_LYRIC_MAP_TEXT','SCORE_LYRIC_MAP_OVERLAP','SCORE_LYRIC_MAP_INCOMPLETE',
    'HERMES_PROPOSAL_MISSING','INVALID_HERMES_PROPOSAL','MODEL_INTERRUPTED_REVIEW_REQUIRED',
    'REQUEST_OUTSIDE_PITCH_SCOPE','INVALID_CREATION_PLAN','INVALID_TEXT','SCORE_REQUIRES_NOTATION_REPAIR',
    'SECTION_SCOPE_VIOLATION','LYRICS_LOCK_VIOLATION','MELODY_LOCK_VIOLATION','RHYTHM_LOCK_VIOLATION',
    'HERMES_PROCESS_FAILED','CHORDS_LOCK_VIOLATION','STRUCTURE_LOCK_VIOLATION','TEMPO_LOCK_VIOLATION','KEY_LOCK_VIOLATION','STYLE_LOCK_VIOLATION'}


def progress(producer, pid, cid, report, *, actor):
    require(isinstance(report,dict) and set(report) <= {'stage','model','model_evidence','session_id','reasoning','elapsed_seconds'}, 'INVALID_PROGRESS')
    require(report.get('stage') in ('starting','composing','format_check','format_repair','validating','dispatching','collecting'), 'INVALID_PROGRESS')
    for key in ('model','session_id','reasoning'):
        if report.get(key) is not None: text(report[key],160)
    require(report.get('model_evidence') in (None,'configuration','session_log'), 'INVALID_PROGRESS')
    seconds=report.get('elapsed_seconds',0)
    require(type(seconds) in (int,float) and 0 <= seconds <= 86400,'INVALID_PROGRESS')
    with producer.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        command=producer._get(db,pid,cid,actor)
        require(command['state'] in ('working','rendering'),'COMMAND_STATE_CONFLICT')
        command['progress']={**command.get('progress',{}),**report,'reported_at':now()}
        producer._save(db,command,actor)
        db.commit()
        return command


def can_retry(command):
    return command['kind']!='analyze' and command['state']=='needs_attention' and not command.get('render_id') and command.get('issue') in RETRYABLE


FORMAT_RECOVERABLE={'INVALID_CREATIVE_RESPONSE_JSON','INVALID_CREATIVE_RESPONSE_FIELDS','MODEL_INTERRUPTED_REVIEW_REQUIRED',
    'CREATIVE_FORMAT_COMPLETION_UNVERIFIED','INVALID_PRODUCTION_SPECS'}


def can_recover_format(command):
    return (command['kind'] in ('plan','compose','direction') and command['state']=='needs_attention'
        and command.get('format_recovery',{}).get('repair_revision')!=REPAIR_REVISION
        and command.get('issue') in FORMAT_RECOVERABLE and not any(command.get(k) for k in ('render_id','result_revision_id','result_sha256')))


def recover_format(producer,pid,cid,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED')
    def action(db):
        source=producer._get(db,pid,cid)
        if source.get('format_recovery') and source['state'] in ('queued','working','rendering','completed'):return source
        require(can_recover_format(source),'FORMAT_RECOVERY_NOT_AVAILABLE')
        # A received creative proposal is frozen under its idempotency key. Never
        # replace it or re-enter a render after uncertainty, even if state is stale.
        received=any(e['type'] in ('creation_result_received','direction_result_received') and e.get('command_id')==cid for e in producer.store.events(pid))
        require(not received,'FORMAT_RECOVERY_NOT_AVAILABLE')
        if source['kind']=='plan':
            latest=[c for c in producer._commands(db,pid).values() if c['kind']=='plan'][-1]
            require(latest['command_id']==cid,'STALE_CREATION_PLAN')
        request=dict(requested_at=now(),previous_issue=source['issue'],mode='existing_response_only',repair_revision=REPAIR_REVISION)
        producer.store._event(db,pid,'creative_format_recovery_requested',actor,dict(command_id=cid,**request))
        source.update(state='queued',issue=None,format_recovery=request)
        source['progress']={**source.get('progress',{}),'stage':'format_check','reported_at':now()}
        return producer._save(db,source,actor)
    initial=producer.store._operation(actor,key,dict(op='recover_creative_format',project_id=pid,command_id=cid),action)
    return producer.get(pid,initial['command_id'])


def retry(producer,pid,cid,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED')
    def action(db):
        source=producer._get(db,pid,cid)
        require(can_retry(source),'RETRY_REQUIRES_RECONCILIATION')
        commands=producer._commands(db,pid)
        prior=next((c for c in commands.values() if c.get('retry_of')==cid),None)
        if prior: return prior
        if source['kind']=='plan':
            latest=[c for c in commands.values() if c['kind']=='plan'][-1]
            require(latest['command_id']==cid,'STALE_CREATION_PLAN')
        child=deepcopy(source)
        if source.get('issue')=='NO_MUSICAL_CHANGE':
            child['instruction'] += ('\n\n上次候选只有规格说明变化，ABC、歌词、style 与源版本完全相同，未产生可渲染修改。'
                '本次必须在未锁定的生成输入中落实实际变化，不能只改 ARR-SPEC/LYR-SPEC 或 summary。'
                '唯一强制保留项以任务 preserve 为准：'+', '.join(source.get('preserve',[]))+'。'
                '源规格中对原稿的描述不是新增锁定。若歌词锁定，可调整未锁定的音符时值、休止、配谱与 style，'
                '给乐句留下真实空间；同步规格，不宣称已经听过新音频。')
            text(child['instruction'],4000)
        for field in ('progress','result','result_sha256','provenance','meter_repairs','format_recovery'):
            child.pop(field,None)
        child.update(command_id=new_id('command'),retry_of=cid,state='queued',issue=None,
            created_at=now(),created_by=actor,result_revision_id=None,render_id=None,provenance=None)
        return producer._save(db,child,actor)
    initial=producer.store._operation(actor,key,dict(op='command_retry',project_id=pid,command_id=cid),action)
    return producer.get(pid,initial['command_id'])
