"""Pinned upstream knowledge and versioned ARR/LYR handoff artifacts."""
import json
from pathlib import Path
from .store import canonical,sha,require

ROOT=Path(__file__).resolve().parent.parent/'integrations/music-skills'


def bundle():
    manifest=json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
    files={}
    for lib in manifest['libraries']:
        base=ROOT/'vendor'/lib['repository_name']/'plugins'/lib['plugin']/'skills'
        for relative,digest in lib['files'].items():
            require((base/relative).is_file(),'PROFESSIONAL_SKILLS_NOT_INSTALLED')
            data=(base/relative).read_bytes()
            require(sha(data)==digest,'PROFESSIONAL_SKILL_FILE_CHANGED')
            files[relative]=data.decode('utf-8')
    return dict(schema_version='songcraft-materials/2',name='jtydhr88-professional-music',version='2026.09.18',
        selection='professional',sources=manifest['libraries'],files=files,
        adapter=(Path(__file__).resolve().parent/'resources/production-guidance.txt').read_text(encoding='utf-8'))


def required(command):return command.get('songcraft',{}).get('selection')=='professional'


def validate_specs(command,result,abc,lyrics):
    if not required(command):return None
    from .score import parse_abc
    from .score_lyrics import lines,validate_abc_lyrics
    validate_abc_lyrics(abc,lyrics)
    spec=result.get('production_specs')
    require(isinstance(spec,dict) and set(spec)=={'arr_spec','lyr_spec'},'PRODUCTION_SPECS_REQUIRED')
    arr,lyr=spec['arr_spec'],spec['lyr_spec']
    for value,keys in [(arr,{'meta','intent','material','form','arrangement','vocal','checks'}),
                       (lyr,{'meta','intent','prosody','lyrics','checks'})]:
        require(isinstance(value,dict) and keys<=value.keys(),'INVALID_PRODUCTION_SPECS')
        require(isinstance(value['checks'],dict) and isinstance(value['checks'].get('self_audit'),list),'INVALID_PRODUCTION_SPECS')
    parsed=parse_abc(abc.encode())
    form=arr['form'];require(isinstance(form,list) and form,'INVALID_PRODUCTION_SPECS')
    start=1;ids=[]
    for section in form:
        require(isinstance(section,dict) and type(section.get('bars')) is int and section['bars']>0 and section.get('start_bar')==start and isinstance(section.get('id'),str),'SPEC_SCORE_MISMATCH')
        start+=section['bars'];ids.append(section['id'])
    require(len(set(ids))==len(ids),'SPEC_SCORE_MISMATCH')
    # Only validate observable output consistency, not the upstream aesthetic heuristics.
    voices={b['voice_id'] for b in parsed['bars']}
    require(bool(voices) and all(start-1==sum(b['voice_id']==v for b in parsed['bars']) for v in voices),'SPEC_SCORE_MISMATCH')
    require(isinstance(arr['meta'],dict) and str(arr['meta'].get('tempo'))==parsed['headers'].get('Q','').split('=')[-1] and arr['meta'].get('meter')==parsed['headers'].get('M'),'SPEC_SCORE_MISMATCH')
    require(isinstance(lyr['lyrics'],list),'INVALID_PRODUCTION_SPECS')
    texts=[]
    for section in lyr['lyrics']:
        require(isinstance(section,dict) and isinstance(section.get('lines'),list) and all(isinstance(t,str) for t in section['lines']),'INVALID_PRODUCTION_SPECS')
        texts.extend(t.strip() for t in section['lines'] if t.strip() and not (t.strip().startswith('[') and t.strip().endswith(']')))
    require(texts==[l['text'] for l in lines(lyrics)],'SPEC_LYRICS_MISMATCH')
    require(len(canonical(spec))<=500000,'INVALID_PRODUCTION_SPECS')
    return spec


def attach(store,pid,rid,command,spec):
    if spec is None:return
    def action(db):
        digest=store._blob(db,canonical(spec))
        store._event(db,pid,'production_specs',command['assigned_to'],dict(revision_id=rid,command_id=command['command_id'],
            scope='scoped_segment' if command.get('scope',{}).get('mode')=='section' else 'whole_song',
            specs_sha256=digest,materials_sha256=command['songcraft']['sha256'],validation='structural_consistency_not_audio_or_independent_critic'))
        return digest
    return store._operation(command['assigned_to'],command['command_id']+'_specs',dict(op='production_specs',project_id=pid,revision_id=rid,specs=spec),action)


def read(store,pid,rid):
    records=[e for e in store.events(pid) if e['type']=='production_specs' and e['revision_id']==rid]
    if not records:return None
    record=records[-1]
    with store.connect() as db:raw=db.execute('SELECT content FROM blobs WHERE hash=?',(record['specs_sha256'],)).fetchone()[0]
    require(sha(raw)==record['specs_sha256'],'PRODUCTION_SPECS_CORRUPT')
    return dict(record=record,specs=json.loads(raw))
