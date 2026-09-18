"""Producer-supplied style/lyrics, rendered without a creative LLM session."""
from .store import require,Store,now,new_id,sha


def start(producer,pid,style,lyrics,assigned_to,target_duration,seed,abc_planning,*,actor,key,mcp_alias=None,
          source_revision_id=None,expected_snapshot_sha256=None):
    require(actor=='producer','PRODUCER_REQUIRED')
    require(producer.bridge and assigned_to in producer.bridge.bindings,'HERMES_UNAVAILABLE')
    brief=dict(style=style,lyrics=lyrics,checkpoint='yue2_3b_int8_convrot.safetensors',seed=seed,max_duration=target_duration)
    generation=dict(mode='style_lyrics',abc_planning=abc_planning)
    Store.validate_revision('',brief,'Style and lyrics direct generation',generation)
    require(seed<=9007199254740991,'MCP_SEED_PRECISION_LIMIT')
    require((source_revision_id is None)==(expected_snapshot_sha256 is None),'STALE_BASE')
    payload=dict(op='direct_generation',project_id=pid,brief=brief,generation=generation,assigned_to=assigned_to,
        mcp_alias=mcp_alias,source_revision_id=source_revision_id,expected_snapshot_sha256=expected_snapshot_sha256)
    def reserve(db):
        producer.store._project(db,pid)
        if source_revision_id:
            source=producer.store._revision(db,pid,source_revision_id)
            require(source['snapshot_sha256']==expected_snapshot_sha256,'STALE_BASE')
        binding=producer.bridge.selection(assigned_to,mcp_alias)
        revision=producer.store._insert_revision(db,pid,'',brief,'制作人提供 style 与歌词，YuE2 直接生成',source_revision_id,actor,generation)
        command=dict(schema_version='producer-command/1',command_id=new_id('command'),project_id=pid,
            source_revision_id=revision['revision_id'],source_snapshot_sha256=revision['snapshot_sha256'],assigned_to=assigned_to,
            kind='render',instruction='按提供的 style 和歌词直接生成试听',allowed_bar_ids=[],grant=None,state='queued',
            result_revision_id=None,render_id=None,created_by=actor,created_at=now(),issue=None,provenance=None,
            production_binding=binding,direct_generation=generation)
        return producer._save(db,command,actor)
    initial=producer.store._operation(actor,key,payload,reserve)
    return producer.get(pid,initial['command_id'])


def empty_score():
    return dict(status='absent',source_abc_sha256=sha(b''),headers={},bars=[],voices=[],sections=[],events=[],parse_diagnostics=[])


def use_score(producer,pid,render_id,*,actor,key):
    require(actor=='producer','PRODUCER_REQUIRED')
    job=producer.renders.get(pid,render_id)
    require(job['state']=='succeeded','RENDER_STATE_CONFLICT')
    source=producer.store.get_revision(pid,job['revision_id'])
    require(source['snapshot'].get('generation',{}).get('abc_planning'),'GENERATED_SCORE_REQUIRED')
    asset=next((a for a in job['assets'] if a['name']=='generated-score.abc'),None)
    require(asset is not None,'GENERATED_SCORE_REQUIRED')
    metadata,stream=producer.store.open_asset(pid,asset['asset_id'])
    with stream:raw=stream.read(1_000_001)
    require(len(raw)<=1_000_000,'INVALID_BODY_SIZE')
    abc=raw.decode('utf-8')
    require(metadata['verification']=='server_generated','GENERATED_SCORE_REQUIRED')
    # One editable copy per render, even if a second button click has a new key.
    summary='采用 YuE2 规划谱作为后续编辑输入；原音频仍属于直接生成候选'
    Store.validate_revision(abc,source['snapshot']['brief'],summary)
    payload=dict(op='adopt_generated_score',project_id=pid,render_id=render_id,asset_id=asset['asset_id'],abc_sha256=sha(raw))
    def adopt(db):
        revision=producer.store._insert_revision(db,pid,abc,source['snapshot']['brief'],summary,job['revision_id'],actor)
        producer.store._event(db,pid,'generated_score_adopted',actor,dict(payload,revision_id=revision['revision_id']))
        return revision
    return producer.store._operation(actor,render_id+'_generated_score',payload,adopt)


def enrich(store,value):
    """No invented notation or claimed correspondence to the generated audio."""
    from .score_lyrics import read
    from .lyric_navigation import lyric_navigation
    from .lyric_density import analyze
    pid=value['revision']['project_id'];rid=value['revision']['revision_id']
    value.update(score=empty_score(),score_lyric_map=read(store,pid,rid),direction_scopes=[],protected_edits=[],
        score_export=dict(available=False,reason='DIRECT_SCORE_NOT_INPUT',warnings=[]),
        score_audition=dict(status='unavailable',reason='直接生成未提供输入谱；YuE2 规划谱可另行下载。',voices=[]),
        score_repair=dict(available=False,changes=[]),manual_editor=None,production_specs=None)
    value['lyric_navigation']=lyric_navigation(value['score'],value['snapshot']['brief']['lyrics'])
    value['lyric_density']=analyze('',value['snapshot']['brief']['lyrics'],[])
    return value
