"""Producer-authored structural and note edits; every save creates an immutable child."""
import re
from .score import parse_abc, EXT_NOTE, note_length
from .direction import lyric_sections
from .store import require, text, sha, canonical

KINDS=('intro','verse','pre_chorus','chorus','bridge','outro')


def layout(snapshot):
    raw=snapshot['abc'].encode();score=parse_abc(raw);lyrics=snapshot['brief']['lyrics'];parts=[]
    lyric_parts=lyric_sections(lyrics)
    for section in score['sections']:
        value=raw[section['byte_start']:section['byte_end']].decode()
        kind=section['suggested_label'].rsplit('_',1)[0]
        lyric=next((p for p in lyric_parts if p['label']==section['suggested_label']),None)
        voice=next((b['voice_id'] for b in score['bars'] if b['section_id']==section['section_id']),'Vocal')
        parts.append(dict(section_id=section['section_id'],kind=kind,voice=voice,
            abc_body=value.split('\n',1)[1] if value.lstrip().startswith('%') and '\n' in value else value,
            lyrics=lyrics[lyric['start']:lyric['end']].split('\n',1)[1] if lyric and '\n' in lyrics[lyric['start']:lyric['end']] else '',
            mapped=bool(lyric)))
    editable=bool(parts) and all(p['mapped'] and p['kind'] in KINDS for p in parts) and len(parts)==len(lyric_parts)
    chords={}
    for token in score['tokens']:
        if token['kind']=='chord_symbol':
            event=next((e for e in score['events'] if e['byte_start']>=token['byte_end']),None)
            if event:chords.setdefault(event['event_id'],[]).append(token)
    notes=[]
    for event in score['events']:
        if event['kind'] not in ('note','rest'):continue
        token=raw[event['byte_start']:event['byte_end']].decode()
        notes.append(dict(event_id=event['event_id'],bar_id=event['bar_id'],voice_id=event['voice_id'],
            token=token,midi=event.get('midi_pitch'),duration=event['duration'],
            chord=' '.join(t['text'][1:-1] for t in chords.get(event['event_id'],[]))))
    return dict(sections=parts,structure_editable=editable,notes=notes)


def preview(store,pid,rid,expected_snapshot_sha256,operation):
    source=store.get_revision(pid,rid)
    require(source['revision']['snapshot_sha256']==expected_snapshot_sha256,'STALE_BASE')
    snapshot=source['snapshot'];abc=snapshot['abc'];brief=dict(snapshot['brief'])
    require(isinstance(operation,dict) and isinstance(operation.get('type'),str),'INVALID_MANUAL_EDIT')
    kind=operation['type'];raw=abc.encode();score=parse_abc(raw)
    if kind=='lyrics':
        require(set(operation)=={'type','lyrics'},'INVALID_MANUAL_EDIT')
        text(operation['lyrics'],100000);brief['lyrics']=operation['lyrics']
    elif kind=='sections':
        require(set(operation)=={'type','sections'},'INVALID_MANUAL_EDIT')
        require(layout(snapshot)['structure_editable'],'SECTION_LYRICS_UNMAPPED')
        parts=operation['sections'];require(isinstance(parts,list) and 1<=len(parts)<=100,'INVALID_SECTION_LIST')
        music=[];words=[]
        for part in parts:
            require(isinstance(part,dict) and set(part)=={'kind','voice','abc_body','lyrics'},'INVALID_SECTION_LIST')
            require(part['kind'] in KINDS and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*',part['voice']),'INVALID_SECTION_LIST')
            text(part['abc_body'],100000)
            require(isinstance(part['lyrics'],str) and len(part['lyrics'])<=100000,'INVALID_TEXT')
            # Only one music section per card; no hidden headers or section escapes.
            body=part['abc_body'].strip()
            require(not re.search(r'^(?:[XTM LQK]:|%\s*(?:intro|verse|chorus|pre|bridge|outro))',body,re.M|re.I),'SECTION_SCOPE_VIOLATION')
            music.append(f"% {part['kind']}\nV:{part['voice']}\n{body}\n")
            lyric_body=part['lyrics'].strip() or '[Instrumental]'
            require(not lyric_sections(lyric_body),'SECTION_SCOPE_VIOLATION')
            words.append(f"[{part['kind']}]\n{lyric_body}\n")
        abc=raw[:score['sections'][0]['byte_start']].decode()+''.join(music)
        brief['lyrics']='\n'.join(words)
    elif kind=='notes':
        require(set(operation)=={'type','edits'},'INVALID_MANUAL_EDIT')
        edits=operation['edits'];require(isinstance(edits,list) and 1<=len(edits)<=1000,'INVALID_EDITS')
        lookup={e['event_id']:e for e in score['events']};seen=set();patches=[]
        for edit in edits:
            require(isinstance(edit,dict) and set(edit)=={'event_id','token','chord'},'INVALID_EDIT_FIELDS')
            eid=edit['event_id'];require(eid in lookup and eid not in seen,'INVALID_EDIT_REGION');seen.add(eid)
            event=lookup[eid];require(event['kind'] in ('note','rest') and not event.get('tie_in') and not event.get('tie_out'),'UNSUPPORTED_TIED_OR_NON_NOTE_EDIT')
            require(isinstance(edit['token'],str) and EXT_NOTE.fullmatch(edit['token']) is not None,'INVALID_NOTE')
            require(isinstance(edit['chord'],str) and len(edit['chord'])<=80 and not any(c in edit['chord'] for c in '\n\r"'),'INVALID_CHORD')
            chord_tokens=[t for t in score['tokens'] if t['kind']=='chord_symbol' and
                next((e['event_id'] for e in score['events'] if e['byte_start']>=t['byte_end']),None)==eid]
            for t in chord_tokens:patches.append((t['byte_start'],t['byte_end'],b''))
            replacement=(('"'+edit['chord']+'"' if edit['chord'] else '')+edit['token']).encode()
            patches.append((event['byte_start'],event['byte_end'],replacement))
        for start,end,value in sorted(patches,reverse=True):raw=raw[:start]+value+raw[end:]
        abc=raw.decode()
    else: require(False,'INVALID_MANUAL_EDIT')
    parsed=parse_abc(abc.encode())
    require(abc!=snapshot['abc'] or brief!=snapshot['brief'],'NO_MUSICAL_CHANGE')
    return dict(abc=abc,brief=brief,parser_status=parsed['status'],diagnostics=parsed['parse_diagnostics'],
        bars=len(parsed['bars']),sections=len(parsed['sections']),can_save=kind=='lyrics' or parsed['status']=='supported',
        source_snapshot_sha256=expected_snapshot_sha256,operation=kind)


def save(store,pid,rid,expected_snapshot_sha256,operation,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED')
    draft=preview(store,pid,rid,expected_snapshot_sha256,operation)
    require(draft['can_save'],'MANUAL_SCORE_INVALID')
    result=store.add_revision(pid,draft['abc'],draft['brief'],
        {'lyrics':'制作人直接编辑歌词','sections':'制作人编辑曲式、段落与歌词','notes':'制作人编辑音符、时值与和弦'}[operation['type']],
        parent_revision_id=rid,expected_parent_snapshot_sha256=expected_snapshot_sha256,actor=actor,key=key)
    def record(db):
        store._event(db,pid,'manual_score_edit',actor,dict(source_revision_id=rid,result_revision_id=result['revision_id'],
            source_snapshot_sha256=expected_snapshot_sha256,result_snapshot_sha256=result['snapshot_sha256'],
            operation_sha256=store._blob(db,canonical(operation)),parser_status=draft['parser_status']))
        return result
    return store._operation(actor,key+'_manual_record',dict(op='manual_score_edit',project_id=pid,revision_id=result['revision_id']),record)
