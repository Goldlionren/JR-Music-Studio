"""Macro revision proposals with frozen source and byte-preserved section boundaries."""
import re
from .score import parse_abc
from .score_repair import normalize_meter
from .store import require, text, identifier, canonical
from .lyric_navigation import TITLES


def lyric_sections(lyrics):
    # Performance annotations inside a chorus belong to that chorus, not new sections.
    pattern = r'^\[(intro|verse|pre[-_ ]?chorus|chorus|bridge|outro)(?:[ _-]*\d+)?\][ \t]*\r?$'
    matches = list(re.finditer(pattern, lyrics, re.I | re.M))
    counts, result = {}, []
    for i, match in enumerate(matches):
        kind = match[1].lower()
        kind = 'pre_chorus' if kind.startswith('pre') else kind
        counts[kind] = counts.get(kind, 0) + 1
        result.append(dict(label=f'{kind}_{counts[kind]}', start=match.start(),
            end=matches[i+1].start() if i+1 < len(matches) else len(lyrics)))
    return result


def scopes(snapshot):
    score = parse_abc(snapshot['abc'].encode())
    lyrics = lyric_sections(snapshot['brief']['lyrics'])
    labels = {s['label'] for s in lyrics}
    result = []
    for section in score['sections']:
        label = section['suggested_label']
        kind, number = label.rsplit('_', 1)
        if kind not in TITLES:
            continue
        matching = label in labels and sum(s['suggested_label'].rsplit('_',1)[0]==kind for s in score['sections']) == sum(s['label'].rsplit('_',1)[0]==kind for s in lyrics)
        result.append(dict(section_id=section['section_id'], label=label,
            title=f'{TITLES[kind]} {number}', lyrics_available=matching,
            bars=[b['ordinal'] for b in score['bars'] if b['section_id']==section['section_id']]))
    return result


def freeze_scope(snapshot, scope):
    require(isinstance(scope, dict), 'INVALID_DIRECTION_SCOPE')
    if scope == {'mode':'song'}:
        return scope
    require(set(scope) == {'mode','section_id'} and scope['mode'] == 'section', 'INVALID_DIRECTION_SCOPE')
    score = parse_abc(snapshot['abc'].encode())
    part = next((s for s in score['sections'] if s['section_id']==scope['section_id']), None)
    require(part is not None and part['suggested_label'].rsplit('_',1)[0] in TITLES, 'UNKNOWN_SECTION')
    lyric = next((s for s in lyric_sections(snapshot['brief']['lyrics']) if s['label']==part['suggested_label']), None)
    require(lyric is not None, 'SECTION_LYRICS_UNMAPPED')
    # Check occurrences agree, otherwise an apparent matching label is ambiguous.
    kind = part['suggested_label'].rsplit('_',1)[0]
    require(sum(s['suggested_label'].rsplit('_',1)[0]==kind for s in score['sections']) ==
        sum(s['label'].rsplit('_',1)[0]==kind for s in lyric_sections(snapshot['brief']['lyrics'])), 'SECTION_LYRICS_UNMAPPED')
    return dict(**scope, label=part['suggested_label'], abc_start=part['byte_start'], abc_end=part['byte_end'],
        lyrics_start=lyric['start'], lyrics_end=lyric['end'])


def propose(producer, project_id, command_id, result, provenance, *, actor):
    command = producer.get(project_id, command_id, actor)
    require(command['kind']=='direction', 'UNSUPPORTED_COMMAND')
    require(command['state'] in ('working','rendering','completed'), 'COMMAND_STATE_CONFLICT')
    require(isinstance(provenance,dict) and set(provenance)=={'execution_id','context_policy'}, 'INVALID_PROVENANCE')
    identifier(provenance['execution_id'])
    require(provenance['context_policy']=='fresh_hermes_cli', 'INVALID_PROVENANCE')
    require(isinstance(result,dict), 'INVALID_CREATIVE_PROPOSAL')
    store = producer.store
    payload = dict(op='direction_proposal', project_id=project_id, command_id=command_id, result=result, provenance=provenance)
    def received(db):
        digest = store._blob(db, canonical(payload))
        store._event(db, project_id, 'direction_result_received', actor, dict(command_id=command_id,result_sha256=digest))
        return digest
    digest = store._operation(actor,command_id+'_direction_raw',payload,received)
    source = store.get_revision(project_id,command['source_revision_id'])
    require(source['revision']['snapshot_sha256']==command['source_snapshot_sha256'], 'STALE_BASE')
    snapshot = source['snapshot']
    scope = command['scope']
    brief = dict(snapshot['brief'])
    from . import professional_skills
    base_fields=set(result)-({'production_specs'} if professional_skills.required(command) else set())
    if scope['mode']=='song':
        require(base_fields in ({'abc','lyrics','style','summary'},{'abc','lyrics','style','summary','lyric_map'}), 'INVALID_CREATIVE_PROPOSAL')
        abc, repairs = normalize_meter(result['abc'])
        brief.update(lyrics=result['lyrics'],style=result['style'])
    else:
        require(base_fields in ({'section_abc','section_lyrics','summary'},{'section_abc','section_lyrics','summary','lyric_map'}), 'INVALID_CREATIVE_PROPOSAL')
        text(result['section_abc'],100000)
        text(result['section_lyrics'],20000)
        raw = snapshot['abc'].encode()
        # A standalone section is validated with the source headers; body headers,
        # added sections or other voices cannot escape the selected replacement.
        score = parse_abc(raw)
        header = ''.join(f'{key}:{value}\n' for key,value in score['headers'].items())
        first_voice = next(b['voice_id'] for b in score['bars'] if b['section_id']==scope['section_id'])
        header += f'V:{first_voice}\n'
        before = raw[scope['abc_start']:scope['abc_end']]
        original = parse_abc((header + before.decode()).encode())
        replacement, repairs = normalize_meter(header + result['section_abc'].rstrip()+'\n')
        parsed = parse_abc(replacement.encode())
        require(len(parsed['sections'])==1 and parsed['sections'][0]['suggested_label'].rsplit('_',1)[0]==scope['label'].rsplit('_',1)[0], 'SECTION_SCOPE_VIOLATION')
        require({v['voice_id'] for v in parsed['voices']}=={v['voice_id'] for v in original['voices']}, 'SECTION_SCOPE_VIOLATION')
        section_abc = replacement[len(header):].encode()
        # Require the replacement to start at its section marker, no leading headers.
        require(parsed['sections'][0]['byte_start']==len(header.encode()), 'SECTION_SCOPE_VIOLATION')
        abc = (raw[:scope['abc_start']] + section_abc + raw[scope['abc_end']:]).decode()
        body = result['section_lyrics'].rstrip()+'\n\n'
        parts = lyric_sections(body)
        require(len(parts)==1 and parts[0]['start']==0 and parts[0]['label'].rsplit('_',1)[0]==scope['label'].rsplit('_',1)[0], 'SECTION_SCOPE_VIOLATION')
        if 'lyrics' in command.get('preserve',[]):
            original_lyrics=snapshot['brief']['lyrics'][scope['lyrics_start']:scope['lyrics_end']]
            require(body.strip()==original_lyrics.strip(),'LYRICS_LOCK_VIOLATION')
            body=original_lyrics
        brief['lyrics'] = snapshot['brief']['lyrics'][:scope['lyrics_start']] + body + snapshot['brief']['lyrics'][scope['lyrics_end']:]
    from .constraints import enforce
    checks=enforce(snapshot,abc,brief,command.get('preserve',[]))
    mapped=None
    if 'lyric_map' in result:
        from . import score_lyrics
        if scope['mode']=='song':mapped=score_lyrics.validate(abc,brief['lyrics'],result['lyric_map'],complete=True)
        else:
            local=score_lyrics.validate(replacement,body,result['lyric_map'],complete=True)
            full=parse_abc(abc.encode());offsets={b['voice_id']:b['ordinal']-1 for b in reversed(full['bars']) if b['section_id']==scope['section_id']}
            prefix=len(score_lyrics.lines(brief['lyrics'][:scope['lyrics_start']]))
            mapped=[]
            for row in local:
                delta=offsets[row['voice']];a,b,c,d=row['range']
                mapped.append(dict(line=prefix+row['line'],voice=row['voice'],range=[a+delta,b,c+delta,d],
                    units=[[u[0],u[1]+delta,u[2],u[3]+delta,u[4]] for u in row['units']]))
            score_lyrics.validate(abc,brief['lyrics'],mapped)
    text(result['summary'],1500)
    from .lyric_density import analyze, enforce as enforce_density
    density=analyze(abc,brief['lyrics'],mapped or [])
    enforce_density(density,command.get('density_limits'))
    specs=professional_skills.validate_specs(command,result,abc if scope['mode']=='song' else replacement,brief['lyrics'] if scope['mode']=='song' else body)
    summary = f"宏观调整（{'整曲' if scope['mode']=='song' else scope['label']}）：{result['summary']}"
    if repairs:
        summary += '；拍数规范化：' + '、'.join(f"第 {c['ordinal']} 小节 {c['before_beats']}→{c['after_beats']} 拍" for c in repairs)
    revision = store.add_revision(project_id,abc,brief,summary[:2000],parent_revision_id=command['source_revision_id'],
        expected_parent_snapshot_sha256=command['source_snapshot_sha256'],actor=actor,key=command_id+'_direction_child')
    professional_skills.attach(store,project_id,revision['revision_id'],command,specs)
    if mapped is not None:
        if scope['mode']=='section':
            selected_lines={r['line'] for r in mapped}
            inherited=score_lyrics.read(store,project_id,revision['revision_id'])['rows']
            mapped=[r for r in inherited if r['line'] not in selected_lines]+mapped
        score_lyrics.attach(store,project_id,revision['revision_id'],mapped,actor=actor,key=command_id+'_lyric_map')
    job = producer.bridge.prepare(project_id,revision['revision_id'],actor=actor,key=command_id,binding=command.get('production_binding'))
    def save(db):
        current=producer._get(db,project_id,command_id,actor)
        require(current['state']=='working','COMMAND_STATE_CONFLICT')
        current.update(state='rendering',result_revision_id=revision['revision_id'],render_id=job['render_id'],
            lyric_density_review=density,
            provenance=provenance,result_sha256=digest,meter_repairs=repairs,preservation_verification=checks)
        return producer._save(db,current,actor)
    return store._operation(actor,command_id+'_direction_accept',payload,save)
