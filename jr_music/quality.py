"""Versioned listening checks: observable evidence, explicit goals and skill provenance."""
import json
import math
from .store import require, canonical, sha
from . import audio_analysis, lyric_timeline, lyric_density, score_lyrics, professional_skills

VERSION = 'listening-quality/1'
DEFAULTS = dict(delivery='melodic', intro_min=0.0, intro_max=15.0,
                max_han_per_second=2.2, dense_run_seconds=12.0, short_gap_seconds=0.4)
ROUTES = {
    'density': [('lyric-writing-skills','lw-structure','§1.2–1.3'),('music-composition-skills','mc-vocal-direction','§2')],
    'intro': [('music-composition-skills','mc-arrangement-arch','§4、§7')],
    'ending': [('music-composition-skills','mc-arrangement-arch','§8')],
    'coverage': [('lyric-writing-skills','lw-workflow','§2–3、§6')],
    'breathing': [('music-composition-skills','mc-ai-tell-audit','§6'),('music-composition-skills','mc-vocal-direction','§2')],
    'development': [('music-composition-skills','mc-arrangement-arch','§2、§6–7'),('lyric-writing-skills','lw-structure','§2')],
}


def validate_policy(value):
    require(isinstance(value,dict) and set(value)==set(DEFAULTS),'INVALID_QUALITY_POLICY')
    require(value['delivery'] in ('melodic','rap_or_fast'),'INVALID_QUALITY_POLICY')
    for key in set(DEFAULTS)-{'delivery'}:
        require(type(value[key]) in (int,float) and math.isfinite(value[key]),'INVALID_QUALITY_POLICY')
    require(0<=value['intro_min']<=value['intro_max']<=120 and
            0.5<=value['max_han_per_second']<=10 and 5<=value['dense_run_seconds']<=60 and
            0<=value['short_gap_seconds']<=3,'INVALID_QUALITY_POLICY')
    return dict(value)


def policies(store,pid,rid):
    return [e for e in store.events(pid) if e['type']=='quality_policy' and e['render_id']==rid]


def policy(store,pid,rid):
    rows=policies(store,pid,rid)
    return dict(generation=len(rows),values=rows[-1]['policy'] if rows else dict(DEFAULTS))


def save_policy(producer,pid,rid,values,expected_generation,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED')
    values=validate_policy(values)
    job=producer.renders.get(pid,rid);require(job['state']=='succeeded','AUDIO_NOT_READY')
    def save(db):
        events=[json.loads(r[0]) for r in db.execute('SELECT document FROM events WHERE project_id=?',(pid,))]
        count=sum(e['type']=='quality_policy' and e['render_id']==rid for e in events)
        require(type(expected_generation) is int and count==expected_generation,'QUALITY_POLICY_CHANGED')
        producer.store._event(db,pid,'quality_policy',actor,dict(render_id=rid,policy=values,generation=count+1))
        return dict(generation=count+1,values=values)
    return producer.store._operation(actor,key,dict(op='quality_policy',project_id=pid,render_id=rid,
        values=values,expected_generation=expected_generation),save)


def sources(codes):
    manifest=json.loads((professional_skills.ROOT/'manifest.json').read_text(encoding='utf-8'))
    result=[]
    for name,skill,section in sorted({entry for code in codes for entry in ROUTES.get(code,[])}):
        lib=next(lib for lib in manifest['libraries'] if lib['repository_name']==name)
        relative=skill+'/SKILL.md'
        result.append(dict(skill=skill,section=section,commit=lib['commit'],sha256=lib['files'][relative],
            url=lib['repository']+'/blob/'+lib['commit']+'/plugins/'+lib['plugin']+'/skills/'+relative))
    return result


def metrics(lyrics,asr,timeline,goals,duration):
    """ASR/manual lyric windows are proxies, NOT a vocal/rap/breath classifier."""
    estimates=lyric_timeline.estimate(lyrics,asr)
    expected={r['line_id']:r for r in estimates}
    confident=[];timed=[]
    for row in timeline:
        original=expected.get(row.get('line_id'))
        if not original:continue
        a,b=row.get('start'),row.get('end')
        if not all(type(t) in (int,float) and math.isfinite(t) for t in (a,b)) or not 0<=a<b<=duration:continue
        if row.get('basis')!='manual' and original['coverage']<0.8:continue
        # Reject a weak ASR segment even if it happens to match source text.
        overlaps=[s for s in asr.get('segments',[]) if s.get('start',a)<b and s.get('end',b)>a]
        if row.get('basis')!='manual' and any(s.get('no_speech_prob',0)>0.5 or s.get('avg_logprob',0)<-1.0 for s in overlaps):continue
        timed.append(dict(row,start=a,end=b))
        text=original['text']
        if any(c.isalnum() and not lyric_density.HAN.fullmatch(c) for c in text):continue
        count=len(lyric_density.HAN.findall(text))
        if count:confident.append(dict(line_id=row['line_id'],section=original['section'],start=a,end=b,
            han_characters=count,rate=round(count/(b-a),3),basis=row.get('basis')))
    # Overlapping/reversed timing cannot establish a continuous delivery rate.
    order_valid=all(b['start']>=a['end'] for a,b in zip(confident,confident[1:]))
    runs=[];current=[]
    def flush():
        if current and current[-1]['end']-current[0]['start']>=goals['dense_run_seconds']:
            runs.append(dict(start=current[0]['start'],end=current[-1]['end'],
                line_ids=[r['line_id'] for r in current]))
        current.clear()
    if order_valid:
        for row in confident:
            if current and (int(row['line_id'].split('_')[1])!=int(current[-1]['line_id'].split('_')[1])+1 or
                            row['section']!=current[-1]['section'] or row['start']-current[-1]['end']>goals['short_gap_seconds']):
                flush()
            if row['rate']>goals['max_han_per_second']:current.append(row)
            else:flush()
        flush()
    total=sum(len(lyric_timeline.normalized(r['text'])) for r in estimates)
    matched=sum(len(lyric_timeline.normalized(r['text']))*r['coverage'] for r in estimates)
    first=next((r for r in timed if estimates and r['line_id']==estimates[0]['line_id']),None)
    measure=asr.get('measurements',{})
    energy=measure.get('energy',[]);period=measure.get('sample_period',0.1)
    usable_energy=bool(energy) and type(period) in (int,float) and period>0 and all(type(v) in (int,float) and math.isfinite(v) and v>=0 for v in energy)
    ending=None
    if usable_energy:
        block=energy[-max(1,round(0.5/period)):]
        ending=sum(block)/len(block)>max(0.008,sorted(energy)[len(energy)//2]*0.65)
    return dict(density_lines=confident,valid_order=order_valid,dense_runs=runs,
        density_coverage=len(confident)/len(estimates) if estimates else 0,
        max_rate=max((r['rate'] for r in confident),default=None) if order_valid else None,
        first_lyric_seconds=first['start'] if first else None,
        text_coverage=matched/total if total and asr else None,
        possible_cutoff=ending,
        dense_seconds=sum(r['end']-r['start'] for r in runs) if order_valid and confident else None)


def read(producer,pid,rid,*,frozen_policy=None):
    job=producer.renders.get(pid,rid);require(job['state']=='succeeded','AUDIO_NOT_READY')
    source=producer.store.get_revision(pid,job['revision_id'])
    snapshot=source['snapshot'];duration=job['verification']['duration_seconds']
    analysis=audio_analysis.read(producer,pid,rid)
    timeline=lyric_timeline.read(producer,pid,rid)
    wav=next(a for a in job['assets'] if a['media_type']=='audio/wav')
    require(timeline['audio_sha256']==wav['sha256'],'ANALYSIS_AUDIO_MISMATCH')
    asr=analysis.get('asr',{})
    if asr:require(asr['source_audio_sha256']==wav['sha256'],'ANALYSIS_AUDIO_MISMATCH')
    selected=frozen_policy or policy(producer.store,pid,rid)
    goals=validate_policy(selected['values'])
    measured=metrics(snapshot['brief']['lyrics'],asr,timeline['lines'],goals,duration)
    checks=[]
    def add(code,label,status,basis,evidence,instruction,start=None,end=None):
        checks.append(dict(code=code,label=label,status=status,basis=basis,evidence=evidence,
            instruction=instruction,start=start,end=end,sources=sources([code])))
    runs=measured['dense_runs'];valid=measured['max_rate'] is not None
    density_status='review' if runs else 'observed' if valid and measured['density_coverage']>=0.8 else 'unknown'
    if goals['delivery']=='rap_or_fast':density_status='intentional'
    add('density','持续密集吐字风险',density_status,'lyric_timing_proxy',
        f"可用行 {len(measured['density_lines'])}/{len(timeline['lines'])}；连续密集窗口 {len(runs)} 处。"+
        ('估计峰值 %.2f 汉字/秒。'%measured['max_rate'] if valid else '时间不足、重叠或语言不适用。')+
        '这是匹配歌词时间窗中的文本负荷，不是说唱分类或换气测量。',
        '先按 lw-structure 调整句数、句长与语义断句，删掉冗余表达；再按 mc-vocal-direction 安排元音延展、句末留白和分段 delivery。同步修改词曲规格，保留核心意象；不要只改换行或把歌词藏到标签里。',
        runs[0]['start'] if runs else None,runs[0]['end'] if runs else None)
    first=measured['first_lyric_seconds']
    add('intro','歌词进入与前奏目标','unknown' if first is None else 'review' if not goals['intro_min']<=first<=goals['intro_max'] else 'observed',
        'first_lyric_proxy',f"目标：首句约 {goals['intro_min']:g}–{goals['intro_max']:g} 秒进入；"+
        (f'首句定位约 {first:.1f} 秒。' if first is not None else '首句未可靠定位，不能用后面的歌词替代。')+
        '此前可能有人声哼唱；纯器乐前奏需要试听确认。',
        f"按 mc-arrangement-arch 的比例和器乐钩子设计前奏，正式首句在约 {goals['intro_min']:g}–{goals['intro_max']:g} 秒进入；器乐前奏不填写要唱的歌词。若后端偏离输入，明确说明，不伪造已测结果。",0,min(duration,(first or goals['intro_max'])+3))
    cutoff=measured['possible_cutoff']
    add('ending','结尾截断风险','unknown' if cutoff is None else 'review' if cutoff else 'observed','mixed_audio_energy',
        '末尾仍有明显能量，需试听是否截断。' if cutoff else '末尾能量较低，但不能证明最后一句完整。' if cutoff is False else '尚无波形测量。',
        '按 mc-arrangement-arch §8 选择适合此曲的镜像、循环减法或有理由的淡出；给最后一句和尾奏留足时间，不默认要求淡出。',max(0,duration-15),duration)
    coverage=measured['text_coverage']
    add('coverage','歌词覆盖','unknown' if coverage is None else 'review' if coverage<0.85 else 'observed','asr_text_match',
        ('识别与原歌词匹配约 %.0f%%；'% (coverage*100) if coverage is not None else '尚无识别。')+'识别错误不等于漏唱，修改歌词后的覆盖率也不能直接当同一文本改善。',
        '逐段核对漏识别位置是否实际漏唱。按 lw-workflow 的词曲双向接口调整字数预算、段落长度与咬字；不得为提高匹配率删除关键意思。')
    add('breathing','换气与持续说唱感','manual_required','producer_listening',
        '按 Terry 人声诊断方法试听换气、辅音／元音和长句。未做人声分离和已标定分类，不输出“已经没有说唱感”。',
        '按 mc-vocal-direction 与词曲分工，减少持续密集辅音、保留元音延展和自然分句；以本曲情绪为准，不机械插入呼吸。')
    add('development','段落起伏与副歌变化','manual_required','producer_listening',
        '试听主副歌推进、能量回落、器乐记忆点和重复副歌变化；不把混音总能量当人声力度。',
        '按 mc-arrangement-arch 调整能量和进退场，并按 lw-structure 区分主歌推进与副歌收束。给副歌变化写明音乐理由，保留核心钩子。')
    mapping=score_lyrics.read(producer.store,pid,job['revision_id'])
    specs=professional_skills.read(producer.store,pid,job['revision_id'])
    result=dict(schema_version=VERSION,render_id=rid,revision_id=job['revision_id'],snapshot_sha256=source['revision']['snapshot_sha256'],
        audio_sha256=wav['sha256'],asr_sha256=sha(canonical(asr)) if asr else None,timeline_generation=timeline['generation'],
        lyrics_sha256=sha(snapshot['brief']['lyrics'].encode()),duration=duration,seed=job.get('seed_decimal',str(snapshot['brief']['seed'])),
        policy=selected,measurements=measured,checks=checks,analysis_status=analysis['status'],
        score_density=lyric_density.analyze(snapshot['abc'],snapshot['brief']['lyrics'],mapping['rows']),
        specs_sha256=specs['record']['specs_sha256'] if specs else None,
        principles=dict(compliance='按明确目标对照，未知项不算失败',listening='听感风险与方案执行分开记录；不合成总分',
            thresholds='字速、连续时长和间隙阈值是 JR 可调启发值，未经模型标定；不是 Terry 教材的通用定律'))
    result['report_sha256']=sha(canonical(result))
    return result


def compare(before,after):
    require(before['policy']==after['policy'],'QUALITY_POLICY_CHANGED')
    rows=[]
    for field,label,direction in [('dense_seconds','连续密集窗口秒数','lower'),('max_rate','估计最大汉字/秒','lower'),
                                  ('first_lyric_seconds','首句进入秒数','target'),('text_coverage','歌词匹配比例','separate'),
                                  ('possible_cutoff','末尾截断风险','lower')]:
        a,b=before['measurements'][field],after['measurements'][field]
        comparable=a is not None and b is not None
        if field in ('dense_seconds','max_rate'):
            comparable=comparable and min(before['measurements']['density_coverage'],after['measurements']['density_coverage'])>=0.8
        if field=='text_coverage' and before['lyrics_sha256']!=after['lyrics_sha256']:comparable=False
        rows.append(dict(metric=field,label=label,before=a,after=b,comparable=comparable,direction=direction,
            conclusion='需试听复核' if comparable else '证据不足或比较对象改变'))
    return dict(rows=rows,verdict='producer_review_required',
        limitation='指标变化不是听感改善证明；人声风格、意境和核心意思是否保留由制作人试听确认。')
