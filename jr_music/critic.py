"""Independent symbolic Critic: accepts frozen inputs, never Composer assertions.

This is a rule-based invocation, not an LLM judge, audio listener or musicologist.
Measurements describe notation. Aesthetic conclusions are explicitly deferred.
"""
from collections import Counter
import re
from . import score
from .music_contracts import validate
from .store import canonical, sha, new_id, require

VERSION = 'symbolic-rules/1'


def critique(request, snapshot, brief, rubric):
    version = 2 if request.get('schema_version') == 'music-critic/2' else 1
    validate('music-critic', 'request', request, version)
    validate('music-critic', 'critic-rubric', rubric)
    require(sha(canonical(snapshot)) == request['snapshot_sha256'], 'CRITIC_SNAPSHOT_MISMATCH')
    require(sha(canonical(brief)) == request['brief_sha256'], 'CRITIC_BRIEF_MISMATCH')
    parsed = score.parse_abc(snapshot['abc'].encode('utf-8'))
    supported = parsed['status'] == 'supported'
    voices = []
    if supported:
        for voice in parsed['voices']:
            notes = [e for e in parsed['events'] if e['kind'] == 'note' and e['voice_id'] == voice['voice_id']]
            pitches = [n['midi_pitch'] for n in notes]
            # Do not count the continuation of a tied note as a new attack.
            attacks = [n['midi_pitch'] for n in notes if not n.get('tie_in')]
            intervals = [abs(b-a) for a,b in zip(attacks, attacks[1:])]
            voices.append(dict(voice_id=voice['voice_id'], note_count=len(notes),
                ambitus_semitones=max(pitches)-min(pitches) if pitches else None,
                stepwise_ratio=sum(i <= 2 for i in intervals)/len(intervals) if intervals else None))
    lines = [x.strip() for x in snapshot['brief']['lyrics'].splitlines() if x.strip() and not re.fullmatch(r'\[.*\]', x.strip())]
    repeats = sorted(x for x,n in Counter(lines).items() if n > 1)
    labels = sorted({re.sub(r'_\d+$', '', s['suggested_label']) for s in parsed['sections']})
    metrics = dict(note_count=sum(v['note_count'] for v in voices), voices=voices, lyric_line_count=len(lines),
                   repeated_lyric_lines=repeats, section_labels=labels)
    findings = []
    def finding(dim, status, message, evidence=(), suggestions=(), level='observed', confidence='high'):
        findings.append(dict(dimension=dim, status=status, finding=message, evidence=list(evidence),
                             suggestions=list(suggestions), evidence_level=level, confidence=confidence))
    missing = sorted(set(brief['required_sections']) - set(labels))
    finding('brief_alignment', 'warning' if missing else 'needs_human_review',
        '缺少要求的段落标签。' if missing else '所需段落标签存在；主题、语言质量及叙事符合度仍须人工复核。',
        [f'required={brief["required_sections"]}; parsed={labels}'], ['人工阅读歌词并核对创作要求。'])
    target = rubric['heuristic_targets']
    below = [v['voice_id'] for v in voices if v['stepwise_ratio'] is not None and v['stepwise_ratio'] < target['preferred_stepwise_ratio']]
    finding('master_alignment', 'warning' if below else 'needs_human_review',
        '部分声部低于档案的探索性级进阈值；这不是质量扣分。' if below else '仅能检查档案的少量符号启发式；不能据此判定整体风格贴合。',
        [f'{v["voice_id"]}: stepwise_ratio={v["stepwise_ratio"]}' for v in voices] + [f'heuristic_target={target["preferred_stepwise_ratio"]}'],
        ['逐句考虑是否需要大跳；不要为满足比例抹平旋律。'], level='heuristic', confidence='low')
    finding('lyric_coherence', 'needs_human_review', '已提取歌词行；规则组件不判断中文语义连贯性。', [f'lyric_line_count={len(lines)}'])
    finding('narrative_coherence', 'not_evaluated', '人物、因果与视角一致性需要语义评审。', suggestions=['核对人物、时间与动作是否前后一致。'])
    finding('melodic_coherence', 'needs_human_review', '报告音程与音域，不把解析通过等同旋律连贯。',
        [f'{v["voice_id"]}: ambitus={v["ambitus_semitones"]}, note_count={v["note_count"]}' for v in voices])
    finding('hook_strength', 'needs_human_review', '重复句只提供可见复现证据，不证明副歌记忆度。',
        ['repeated_line=' + x for x in repeats], ['试听后判断核心句是否容易记住。'])
    finding('section_contrast', 'needs_human_review', '段落来源是 ABC 注释标签，不是音频分段或听感对比。', labels)
    wide = [v for v in voices if v['ambitus_semitones'] is not None and v['ambitus_semitones'] > target['max_ambitus_semitones']]
    finding('singability', 'warning' if wide else 'needs_human_review',
        '存在超过探索音域的声部；声部可能是器乐，需按实际演唱角色检查。' if wide else '音域只是初筛；未验证汉字对音、换气或真实歌手适配。',
        [f'{v["voice_id"]}: {v["ambitus_semitones"]} semitones' for v in wide], level='heuristic', confidence='low')
    finding('harmonic_fit', 'not_evaluated', '尚未做和弦功能与旋律的联合评估。')
    finding('arrangement_fit', 'not_evaluated', '没有听音；ABC 声部与风格文字不能证明实际配器。')
    finding('emotional_arc', 'not_evaluated', '尚未进行独立语义评审与试听，不能声称情绪感染力。')
    def grams(value):
        value = re.sub(r'\W+', '', value, flags=re.UNICODE).lower()
        return {value[i:i+8] for i in range(max(0,len(value)-7))}
    candidate_grams = grams(snapshot['brief']['lyrics'])
    overlap = [i for i,t in enumerate(request['reference_texts']) if candidate_grams & grams(t)]
    finding('originality_risk', 'warning' if overlap else 'needs_human_review' if request['reference_texts'] else 'not_evaluated',
        '检测到连续八字符重合，需人工核对。' if overlap else '提供的文本未出现连续八字符重合；不代表原创性已获证明。' if request['reference_texts'] else '没有提供可比较的参考文本；未执行相似片段检测。',
        [f'reference_index={i}' for i in overlap], ['此检测不覆盖旋律、语义改写或外部歌曲库。'], level='heuristic', confidence='low')
    diagnostics = [d['code'] for d in parsed['parse_diagnostics']]
    finding('technical_validity', 'pass' if supported else 'fail',
        'ABC 在当前受支持子集内通过解析。' if supported else 'ABC 含不支持或不合法内容，符号音高统计已禁用。',
        [f'parser={score.PARSER_VERSION}', f'abc_sha256={parsed["source_abc_sha256"]}'] + diagnostics)
    result = dict(schema_version='music-critic/' + str(version), critic_invocation_id=new_id('critic'), request_sha256=sha(canonical(request)), critic_version=VERSION,
        technical_validity=dict(status=parsed['status'], diagnostics=diagnostics, scope='ABC subset syntax, bar durations and supported context; not audio or aesthetic quality'),
        measurements=metrics, findings=findings, audio_evaluated=False, overall_score=None,
        strengths=['当前 ABC 子集解析通过。'] if supported else [],
        weaknesses=[f['finding'] for f in findings if f['status'] in ('warning','fail')],
        limitations=['独立规则调用，不是独立 LLM 或听音专家。', '相邻音程含跨小节/段落连接；计入同音，排除连音延续；不能当作短语内旋律统计。',
            '无音频意见、总分、自动胜出或自动 HEAD 修改。', '单次 A/B 不证明因果、跨模型泛化或统计显著性。'])
    return validate('music-critic', 'result', result, version)
