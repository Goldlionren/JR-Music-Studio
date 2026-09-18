"""On-demand audio analysis: existing Hermes MCP for SheetSage, local CPU ASR."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.request import build_opener,ProxyHandler
from .store import require,new_id,now,canonical,sha
from .score import parse_abc
from .score_audition import audition

ROOT=Path(__file__).resolve().parents[1]
POOL=ThreadPoolExecutor(max_workers=1,thread_name_prefix='jr-audio-analysis')
ACTIVE=set();ACTIVE_LOCK=threading.Lock()


def folder(cid):return ROOT/'data/analyses'/cid


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_bytes(canonical(value));temporary.replace(path)


def latest(producer,pid,render_id):
    values=[c for c in producer.list(pid) if c['kind']=='analyze' and c['source_render_id']==render_id]
    return values[-1] if values else None


def start(producer,pid,render_id,assigned_to,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED')
    require(producer.bridge and assigned_to in producer.bridge.bindings,'HERMES_UNAVAILABLE')
    job=producer.renders.get(pid,render_id);require(job['state']=='succeeded','AUDIO_NOT_READY')
    require((ROOT/'data/models/faster-whisper-large-v3-turbo/model.bin').is_file(),'ASR_MODEL_REQUIRED')
    asset=next((a for a in job['assets'] if a['media_type']=='audio/wav'),None)
    require(asset is not None,'AUDIO_NOT_READY')
    def create(db):
        existing=[c for c in producer._commands(db,pid).values() if c['kind']=='analyze' and c['source_render_id']==render_id]
        if existing and existing[-1].get('issue')!='ANALYSIS_PREFLIGHT_FAILED':return existing[-1]
        c=dict(schema_version='producer-command/4',command_id=new_id('command'),project_id=pid,
            source_revision_id=job['revision_id'],source_snapshot_sha256=job['snapshot_sha256'],source_render_id=render_id,
            source_asset=asset,source_duration=job['verification']['duration_seconds'],kind='analyze',assigned_to=assigned_to,
            instruction='分析实际歌曲：旋律转谱、歌词时间戳与波形',allowed_bar_ids=[],grant=None,state='preparing',
            result_revision_id=None,render_id=None,created_by=actor,created_at=now(),issue=None,provenance=None)
        return producer._save(db,c,actor)
    c=producer.store._operation(actor,key,dict(op='audio_analysis_start',project_id=pid,render_id=render_id,assigned_to=assigned_to),create)
    c=producer.get(pid,c['command_id']);directory=folder(c['command_id']);directory.mkdir(parents=True,exist_ok=True)
    source=directory/'source.wav'
    if not source.exists():
        _,stream=producer.store.open_asset(pid,asset['asset_id'])
        with stream,source.with_suffix('.tmp').open('wb') as out:shutil.copyfileobj(stream,out)
        source.with_suffix('.tmp').replace(source)
    require(sha(source.read_bytes())==asset['sha256'],'ANALYSIS_AUDIO_MISMATCH')
    with producer.store.connect() as db:
        db.execute('BEGIN IMMEDIATE');c=producer._get(db,pid,c['command_id'])
        if c['state']=='preparing':c['state']='queued';producer._save(db,c,actor)
        db.commit()
    return c


def packet(producer,pid,cid,*,actor):
    command=producer.get(pid,cid,actor);require(command['kind']=='analyze' and command['state']=='working','COMMAND_STATE_CONFLICT')
    directory=folder(cid);source=directory/'source.wav'
    # Configuration belongs to the local deployment, never comes from an agent.
    config=json.loads((ROOT/'data/hermes-deployment.json').read_text())
    argv=fetch('/system_stats')['system'].get('argv',[])
    input_dir=Path(argv[argv.index('--input-directory')+1]) if '--input-directory' in argv else Path(config['environment']['comfy_root'])/'input'
    require(input_dir.is_dir(),'COMFY_INPUT_DIRECTORY_REQUIRED')
    target=input_dir/(cid+'.wav')
    if not target.exists():shutil.copyfile(source,target)
    require(sha(target.read_bytes())==command['source_asset']['sha256'],'ANALYSIS_AUDIO_MISMATCH')
    graph={'1':dict(class_type='AudioEncoderLoader',inputs=dict(audio_encoder_name='sheetsage2_bf16.safetensors')),
        '2':dict(class_type='LoadAudio',inputs=dict(audio=target.name)),
        '3':dict(class_type='SheetSage2AudioToABC',inputs=dict(audio_encoder=['1',0],audio=['2',0],mode='melody')),
        '4':dict(class_type='PreviewAny',inputs=dict(source=['3',0]))}
    workflow=directory/'workflow.api.json'
    if workflow.exists():require(workflow.read_bytes()==canonical(graph),'ANALYSIS_WORKFLOW_CHANGED')
    else:write(workflow,graph)
    def claim(db):
        c=producer._get(db,pid,cid,actor)
        require(not c.get('analysis_dispatched'),'DISPATCH_ALREADY_CLAIMED')
        c['analysis_dispatched']=now();return producer._save(db,c,actor)
    # This permission is not replayed after an ambiguous HTTP response.
    with producer.store.connect() as db:
        db.execute('BEGIN IMMEDIATE');claim(db);db.commit()
    return dict(mcp_alias=producer.bridge.bindings[actor]['mcp_alias'],tool='run_workflow',retry_allowed=False,
        arguments=dict(workflow_path=str(workflow),wait=False,timeout_seconds=110.0,confirm_spend=False))


def receive(producer,pid,cid,receipt,*,actor):
    command=producer.get(pid,cid,actor);require(command['kind']=='analyze','UNSUPPORTED_COMMAND')
    payload=dict(op='audio_analysis_receipt',project_id=pid,command_id=cid,receipt=receipt)
    def action(db):
        c=producer._get(db,pid,cid,actor)
        require(c.get('analysis_dispatched'),'ANALYSIS_NOT_DISPATCHED')
        preflight=receipt.get('is_error') and '[workflow_unknown_nodes]' in str(receipt)
        c.update(state='needs_attention' if preflight else 'analyzing',issue='ANALYSIS_PREFLIGHT_FAILED' if preflight else None,
            analysis_receipt_sha256=producer.store._blob(db,canonical(receipt)))
        return producer._save(db,c,actor)
    producer.store._operation(actor,cid+'_analysis_receipt',payload,action)
    if producer.get(pid,cid).get('issue')!='ANALYSIS_PREFLIGHT_FAILED':resume(producer,pid,cid)
    return producer.get(pid,cid)


def resume(producer,pid,cid):
    c=producer.get(pid,cid);require(c['kind']=='analyze','UNSUPPORTED_COMMAND')
    if c['state']=='completed':return c
    require(c.get('issue')!='ANALYSIS_PREFLIGHT_FAILED','ANALYSIS_PREFLIGHT_FAILED')
    require(c.get('analysis_dispatched'),'ANALYSIS_NOT_DISPATCHED')
    with ACTIVE_LOCK:
        if cid not in ACTIVE:
            with producer.store.connect() as db:
                db.execute('BEGIN IMMEDIATE');c=producer._get(db,pid,cid)
                c.update(state='analyzing',issue=None);producer._save(db,c,'audio_analyzer');db.commit()
            ACTIVE.add(cid);POOL.submit(run,producer,pid,cid)
    return c


def fetch(route):
    return json.load(build_opener(ProxyHandler({})).open('http://127.0.0.1:8188'+route,timeout=20))


def run(producer,pid,cid):
    directory=folder(cid)
    try:
        c=producer.get(pid,cid);expected=json.loads((directory/'workflow.api.json').read_text())
        report_path=directory/'transcription.json'
        if not report_path.exists():
            deadline=time.monotonic()+900
            while time.monotonic()<deadline:
                history=fetch('/history?max_items=100')
                matched=[(identifier,row) for identifier,row in history.items() if canonical(row.get('prompt',[None,None,{}])[2])==canonical(expected)]
                if len(matched)>1:require(False,'AMBIGUOUS_ANALYSIS_HISTORY')
                if matched:
                    prompt_id,row=matched[0];require(row['status']['status_str']=='success','SHEETSAGE_ANALYSIS_FAILED')
                    abc=row['outputs']['4']['text'][0];require(isinstance(abc,str),'INVALID_TRANSCRIPTION')
                    parsed=parse_abc(abc.encode());plan=audition(parsed)
                    report=dict(status='transcribed_estimate',project_id=pid,revision_id=c['source_revision_id'],render_id=c['source_render_id'],
                        source_asset_id=c['source_asset']['asset_id'],source_audio_sha256=c['source_asset']['sha256'],
                        source_abc_sha256=producer.store.get_revision(pid,c['source_revision_id'])['revision']['abc_sha256'],
                        source_duration=c['source_duration'],transcribed_abc=abc,transcribed_abc_sha256=sha(abc.encode()),
                        prompt_id=prompt_id,tempo=parsed['headers'].get('Q'),sections=[s['suggested_label'] for s in parsed['sections']],
                        audition=plan,workflow_verified=True,history_sha256=sha(canonical(row)),acoustic_accuracy='not_measured')
                    write(directory/'history.json',row);write(report_path,report);break
                time.sleep(3)
            require(report_path.exists(),'ANALYSIS_HISTORY_PENDING')
        output=directory/'asr.json'
        if not output.exists():
            command=[str(ROOT/'data/audio-analysis-env/Scripts/python.exe'),str(ROOT/'tools/analyze_audio.py'),
                '--audio',str(directory/'source.wav'),'--output',str(output),'--model',str(ROOT/'data/models/faster-whisper-large-v3-turbo')]
            with (directory/'asr.log').open('wb') as log:
                result=subprocess.run(command,stdout=log,stderr=log,timeout=1800)
            require(result.returncode==0 and output.exists(),'ASR_ANALYSIS_FAILED')
        asr=json.loads(output.read_text(encoding='utf-8'));require(asr['source_audio_sha256']==c['source_asset']['sha256'],'ANALYSIS_AUDIO_MISMATCH')
        verify_report(c,json.loads(report_path.read_text(encoding='utf-8')))
        with producer.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');current=producer._get(db,pid,cid)
            current.update(state='completed',issue=None,asr_sha256=producer.store._blob(db,canonical(asr)),
                transcription_sha256=producer.store._blob(db,report_path.read_bytes()))
            producer._save(db,current,'audio_analyzer');db.commit()
    except Exception as exc:
        code=getattr(exc,'code',None) or ('AUDIO_ANALYSIS_TIMEOUT' if isinstance(exc,subprocess.TimeoutExpired) else type(exc).__name__.upper())
        with producer.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');current=producer._get(db,pid,cid);current.update(state='needs_attention',issue=code)
            producer._save(db,current,'audio_analyzer');db.commit()
    finally:
        with ACTIVE_LOCK:ACTIVE.discard(cid)


def read(producer,pid,render_id):
    job=producer.renders.get(pid,render_id)
    c=latest(producer,pid,render_id)
    if not c:return dict(status='not_analyzed')
    result=dict(status=c['state'],command_id=c['command_id'],issue=c['issue'],can_restart=c.get('issue')=='ANALYSIS_PREFLIGHT_FAILED',
        can_resume=bool(c.get('analysis_dispatched')) and c['state']!='completed' and c.get('issue')!='ANALYSIS_PREFLIGHT_FAILED')
    directory=folder(c['command_id'])
    for key,name in [('transcription','transcription.json'),('asr','asr.json'),('progress','asr.progress.json')]:
        path=directory/name
        digest=c.get(key+'_sha256')
        if digest:
            with producer.store.connect() as db:raw=db.execute('SELECT content FROM blobs WHERE hash=?',(digest,)).fetchone()[0]
            require(sha(raw)==digest,'ANALYSIS_BLOB_MISMATCH');result[key]=json.loads(raw)
        elif path.exists():result[key]=json.loads(path.read_text(encoding='utf-8'))
    if result.get('asr'):require(result['asr']['source_audio_sha256']==c['source_asset']['sha256'],'ANALYSIS_AUDIO_MISMATCH')
    if result.get('transcription'):
        verify_report(c,result['transcription'])
        require(result['transcription']['source_abc_sha256']==job['abc_sha256'],'REVIEW_BINDING_MISMATCH')
    result['stage']='completed' if c['state']=='completed' else 'recognizing_lyrics' if result.get('transcription') else 'waiting_transcription'
    return result


def verify_report(c,report):
    require(report['project_id']==c['project_id'] and report['render_id']==c['source_render_id'] and
        report['revision_id']==c['source_revision_id'] and report['source_asset_id']==c['source_asset']['asset_id'] and
        report['source_audio_sha256']==c['source_asset']['sha256'],'ANALYSIS_AUDIO_MISMATCH')
    require(sha(report['transcribed_abc'].encode())==report['transcribed_abc_sha256'],'REVIEW_SCORE_MISMATCH')
