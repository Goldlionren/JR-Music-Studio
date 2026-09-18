"""Evidence-based whole-song checks; subjective continuity stays producer-owned."""
import math
from . import audio_analysis,lyric_timeline
from .store import require,canonical,sha


def evaluate(lyrics,asr,timeline):
    lines=lyric_timeline.estimate(lyrics,asr);measure=asr.get('measurements',{});duration=measure.get('duration',0)
    result=[]
    def item(code,label,status,evidence,instruction='',start=None,end=None):
        result.append(dict(code=code,label=label,status=status,evidence=evidence,instruction=instruction,start=start,end=end))
    if not asr:
        item('analysis','音频分析','unknown','先分析这次音频，再查看覆盖和声音测量。')
        return result
    total=sum(len(lyric_timeline.normalized(l['text'])) for l in lines)
    matched=sum(len(lyric_timeline.normalized(l['text']))*l['coverage'] for l in lines)
    coverage=matched/total if total else None
    missing=[l for l in lines if l['coverage']<0.65]
    item('coverage','歌词文本匹配', 'review' if missing else 'observed' if total else 'unknown',
        f'ASR 与原歌词字符匹配约 {coverage:.0%}；{len(missing)}/{len(lines)} 行匹配不足。识别遗漏不等于漏唱。' if total else '没有可比对的歌词。',
        '请先试听核实这些歌词是否漏唱或含混，再调整演唱分句与时长，保留原歌词：'+ ' / '.join(l['text'] for l in missing) if missing else '')
    positioned=[l for l in timeline if l['start'] is not None]
    if positioned:
        first=min(l['start'] for l in positioned);last=max(l['end'] for l in positioned)
        item('intro','进入歌词的位置','review' if first>15 else 'observed',f'首个已定位歌词约在 {first:.1f} 秒；此前可能包含前奏、哼唱或未识别的人声。',
            '请缩短进入正式歌词之前的前奏与哼唱，尽早唱出第一句；保留歌词及主旋律。' if first>15 else '',0,min(duration,first+5))
        item('tail','歌词之后的尾段','review' if duration-last>30 else 'observed',f'最后已定位歌词结束于 {last:.1f} 秒，之后还有 {max(0,duration-last):.1f} 秒；需试听确认是尾奏还是漏识别。',
            '请试听并收束最后一段歌词后的尾奏，保留完整最后一句，避免过长重复。' if duration-last>30 else '',max(0,last-5),duration)
    else:item('intro','进入歌词的位置','unknown','尚无可用歌词时间，不能把器乐响起当成人声起点。')
    energy=measure.get('energy',[]);period=measure.get('sample_period',0.1)
    ending=sum(energy[-5:])/len(energy[-5:]) if energy else 0
    middle=sorted(energy)[len(energy)//2] if energy else 0
    cutoff=ending>max(0.008,middle*0.65)
    item('ending','结尾收束','review' if cutoff else 'observed',
        f'最后 0.5 秒的平均能量 {ending:.4f}。'+('仍有明显声音，请核对是否突然截断。' if cutoff else '末尾能量较低；这不能证明句子已经唱完。'),
        '请让最后一句完整唱完并自然收尾，预留短尾奏与渐弱，避免在持续演唱或伴奏中截断。' if cutoff else '',max(0,duration-15),duration)
    clip=measure.get('clipped_fraction',0)
    item('peak','峰值与饱和采样','review' if clip>0.001 else 'observed',f'16 kHz 分析副本中，接近满幅采样占比 {clip:.3%}；用于提示，不替代原始文件混音测量。',
        '请降低过强的整体响度与失真，保留动态，核对人声和伴奏峰值。' if clip>0.001 else '')
    windows=[]
    for offset in range(0,len(energy),max(1,round(20/period))):
        block=energy[offset:offset+round(20/period)]
        if len(block)*period<5:continue
        rms=math.sqrt(sum(v*v for v in block)/len(block));windows.append(dict(start=offset*period,end=min(duration,(offset+len(block))*period),db=20*math.log10(max(rms,1e-6))))
    jumps=[(a,b) for a,b in zip(windows,windows[1:]) if abs(a['db']-b['db'])>10 and max(a['db'],b['db'])>-45]
    item('dynamics','长曲响度衔接','review' if jumps else 'observed',
        f'按 20 秒窗口检查；{len(jumps)} 处相邻窗口平均能量相差超过 10 dB。段落强弱变化也可能是有意设计。',
        '请核对段落之间突变的响度，保持音乐所需的强弱对比，同时改善听感衔接。' if jumps else '',
        max(0,jumps[0][1]['start']-5) if jumps else None,min(duration,jumps[0][1]['start']+10) if jumps else None)
    sections={}
    for line in lines:sections.setdefault(line['section'],[]).append(line)
    item('sections','分段歌词覆盖','review' if missing else 'observed','；'.join(f'{key}：{sum(l["coverage"]>=0.65 for l in rows)}/{len(rows)} 行达到匹配门槛' for key,rows in sections.items()) or '没有段落标签')
    item('continuity','声线、风格与情绪连续性','manual_required','请对照开头、中段和结尾试听。当前测量不能证明声线或音乐风格一致。')
    return result


def read(producer,pid,render_id):
    job=producer.renders.get(pid,render_id);report=audio_analysis.read(producer,pid,render_id);timeline=lyric_timeline.read(producer,pid,render_id)
    snapshot=producer.store.get_revision(pid,job['revision_id'])['snapshot']
    result=dict(schema_version='song-check/1',render_id=render_id,revision_id=job['revision_id'],audio_sha256=timeline['audio_sha256'],
        analysis_command_id=report.get('command_id'),asr_sha256=sha(canonical(report['asr'])) if report.get('asr') else None,
        timeline_generation=timeline['generation'],checks=evaluate(snapshot['brief'].get('lyrics',''),report.get('asr',{}),timeline['lines']),
        listening_points=[round(job['verification']['duration_seconds']*f,2) for f in (0,0.45,0.85)])
    result['report_sha256']=sha(canonical(result))
    records=[e for e in producer.store.events(pid) if e['type']=='song_check_decision' and e['render_id']==render_id]
    result['decisions']=records
    return result


def save(producer,pid,render_id,expected_report_sha256,decision,note,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED');require(decision in ('ready','revise','undecided'),'INVALID_CHECK_DECISION')
    require(isinstance(note,str) and len(note)<=4000,'INVALID_TEXT')
    report=read(producer,pid,render_id);require(report['report_sha256']==expected_report_sha256,'CHECK_REPORT_CHANGED')
    def action(db):
        # A concurrent manual timing save invalidates the reviewed report.
        import json
        count=sum(e['type']=='lyric_timeline' and e.get('render_id')==render_id for e in (json.loads(r[0]) for r in db.execute('SELECT document FROM events WHERE project_id=?',(pid,))))
        require(count==report['timeline_generation'],'CHECK_REPORT_CHANGED')
        evidence={k:v for k,v in report.items() if k!='decisions'}
        digest=producer.store._blob(db,canonical(evidence))
        value=dict(render_id=render_id,revision_id=report['revision_id'],audio_sha256=report['audio_sha256'],report_blob_sha256=digest,
            decision=decision,note=note,report_sha256=expected_report_sha256)
        producer.store._event(db,pid,'song_check_decision',actor,value)
        return value
    return producer.store._operation(actor,key,dict(op='song_check_decision',project_id=pid,render_id=render_id,report_sha256=expected_report_sha256,decision=decision,note=note),action)
