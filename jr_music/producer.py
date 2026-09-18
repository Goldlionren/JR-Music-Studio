"""Producer commands stored as append-only events in the existing project ledger.

This is an orchestration inbox, not a second revision store or render lifecycle.
Partial proposal processing is recoverable through fixed idempotency keys.
"""
import json
from .store import require, text, identifier, new_id, now, canonical
from .render import RenderService
from .limits import MAX_DURATION_SECONDS


class ProducerService:
    def __init__(self, store, bridge=None):
        self.store, self.bridge = store, bridge
        self.renders = bridge.renders if bridge else RenderService(store)

    def _commands(self, db, project_id=None):
        query = 'SELECT document FROM events'
        args = ()
        if project_id:
            query += ' WHERE project_id=?'
            args = (project_id,)
        rows = db.execute(query + ' ORDER BY sequence', args)
        result = {}
        for row in rows:
            event = json.loads(row[0])
            if event['type'] == 'producer_command':
                command = event['command']
                result[command['command_id']] = command
        return result

    def list(self, project_id=None, actor=None):
        with self.store.connect() as db:
            values = self._commands(db, project_id).values()
            return [v for v in values if actor is None or v['assigned_to'] == actor]

    def get(self, project_id, command_id, actor=None):
        with self.store.connect() as db:
            return self._get(db, project_id, command_id, actor)

    def _get(self, db, project_id, command_id, actor=None):
        command = self._commands(db, project_id).get(identifier(command_id))
        require(command is not None, 'COMMAND_NOT_FOUND')
        require(actor is None or command['assigned_to'] == actor, 'COMMAND_OWNER_REQUIRED')
        return command

    def _save(self, db, command, actor):
        command['updated_at'] = now()
        self.store._event(db, command['project_id'], 'producer_command', actor, dict(command=command))
        return command

    def create(self, project_id, revision_id, expected_snapshot_sha256, assigned_to, kind,
               instruction, allowed_bar_ids, *, actor, key, target_duration=None, scope=None, preserve=None, songcraft_selection='none', density_limits=None, mcp_alias=None):
        from . import songcraft
        songcraft.validate(songcraft_selection)
        require(kind!='render' or songcraft_selection=='none','SONGCRAFT_REQUIRES_CREATIVE_TASK')
        require(assigned_to in ('yinyue', 'xiaowu'), 'UNKNOWN_AGENT')
        require(self.bridge and assigned_to in self.bridge.bindings, 'HERMES_UNAVAILABLE')
        require(kind in ('pitch_edit', 'render', 'direction'), 'UNSUPPORTED_COMMAND')
        require(scope is None or kind == 'direction', 'INVALID_DIRECTION_SCOPE')
        require(preserve is None or kind == 'direction', 'INVALID_PRESERVE_FIELDS')
        if preserve is not None:
            from .constraints import validate_locks
            preserve=validate_locks(preserve)
        if target_duration is not None:
            require(kind == 'render', 'DURATION_CHANGE_REQUIRES_RENDER')
            require(type(target_duration) in (int, float) and 1 <= target_duration <= MAX_DURATION_SECONDS, 'INVALID_DURATION')
        text(instruction, 4000)
        require(isinstance(allowed_bar_ids,list) and all(isinstance(v,str) for v in allowed_bar_ids)
            and len(set(allowed_bar_ids)) == len(allowed_bar_ids) and len(allowed_bar_ids) <= 128, 'INVALID_EDIT_REGION')
        require(bool(allowed_bar_ids) if kind == 'pitch_edit' else allowed_bar_ids == [], 'INVALID_EDIT_REGION')
        source = self.store.get_revision(project_id, revision_id)
        if density_limits is not None:
            from .lyric_density import validate_limits
            validate_limits(density_limits)
            require(kind=='direction' and scope=={'mode':'song'} and 'lyrics' not in (preserve or []),'INVALID_DENSITY_SCOPE')
        revision = source['revision']
        require(revision['snapshot_sha256'] == expected_snapshot_sha256, 'STALE_BASE')
        if kind == 'direction':
            from .direction import freeze_scope
            scope = freeze_scope(source['snapshot'], scope)
        payload = dict(op='producer_command_create', project_id=project_id, revision_id=revision_id,
            expected_snapshot_sha256=expected_snapshot_sha256, assigned_to=assigned_to,
            kind=kind, instruction=instruction, allowed_bar_ids=allowed_bar_ids)
        if mcp_alias is not None:payload['mcp_alias']=mcp_alias
        if target_duration is not None:
            payload['target_duration'] = target_duration
        if scope is not None:
            payload['scope'] = scope
        if preserve is not None:
            payload['preserve'] = preserve
        if songcraft_selection!='none': payload['songcraft_selection']=songcraft_selection
        if density_limits is not None: payload['density_limits']=density_limits
        # Persist the exact intent first: even interruptions cannot reuse this key
        # with a different agent, instruction or region and silently enqueue work.
        def create(db):
            command = dict(schema_version='producer-command/1', command_id=new_id('command'),
                project_id=project_id, source_revision_id=revision_id,
                source_snapshot_sha256=expected_snapshot_sha256, assigned_to=assigned_to,
                kind=kind, instruction=instruction, allowed_bar_ids=allowed_bar_ids,
                grant=None, state='preparing', result_revision_id=None, render_id=None,
                created_by=actor, created_at=now(), issue=None, provenance=None)
            command['production_binding']=self.bridge.selection(assigned_to,mcp_alias)
            if songcraft_selection!='none': command['songcraft']=songcraft.freeze(self.store,db,songcraft_selection)
            if target_duration is not None:
                command['target_duration'] = target_duration
            if scope is not None:
                command['schema_version'] = 'producer-command/3'
                command['scope'] = scope
            if preserve is not None:
                command['preserve'] = preserve
            if kind=='direction' and songcraft_selection!='professional':
                from .lyric_density import guidance
                command['singing_guidance']=guidance()
            if density_limits is not None:command['density_limits']=dict(density_limits)
            return self._save(db, command, actor)
        initial = self.store._operation(actor, key, payload, create)
        current = self.get(project_id, initial['command_id'])
        if current['state'] != 'preparing':
            return current
        grant = None
        if kind == 'pitch_edit':
            parsed = self.store.derive_score(project_id, revision_id, actor=actor, key=initial['command_id'])
            grant = self.store.create_edit_request(project_id, revision_id, parsed['representation_id'],
                revision['abc_sha256'], allowed_bar_ids, instruction, actor=actor, key=initial['command_id'])
        else:
            require(allowed_bar_ids == [], 'INVALID_EDIT_REGION')
        def ready(db):
            current = self._get(db, project_id, initial['command_id'])
            require(current['state'] == 'preparing', 'COMMAND_STATE_CONFLICT')
            current.update(grant=grant, state='queued')
            return self._save(db, current, actor)
        return self.store._operation(actor, initial['command_id'], dict(op='producer_command_ready', project_id=project_id), ready)

    def claim(self, project_id, command_id, *, actor):
        current = self.get(project_id, command_id, actor)
        claim_key = command_id + ('_format_recovery' if current.get('format_recovery') else '')
        if current.get('format_recovery',{}).get('repair_revision'):
            claim_key += '_'+current['format_recovery']['repair_revision']
        def action(db):
            command = self._get(db, project_id, command_id, actor)
            require(command['state'] == 'queued', 'COMMAND_ALREADY_CLAIMED')
            command['state'] = 'working'
            return self._save(db, command, actor)
        # A replay returns the same claim; worker's local journal prevents a second
        # model invocation. Rendering itself uses the existing one-shot dispatch.
        return self.store._operation(actor, claim_key, dict(op='producer_command_claim', project_id=project_id), action)

    def packet(self, project_id, command_id, *, actor):
        command = self.get(project_id, command_id, actor)
        if command['kind'] in ('plan', 'compose'):
            from .creation import CreationService
            return CreationService(self).packet(command)
        source = self.store.get_revision(project_id, command['source_revision_id'])
        from .professional_skills import read as read_specs
        source['production_specs']=read_specs(self.store,project_id,command['source_revision_id'])
        parsed = None
        if command['grant']:
            parsed = self.store.get_score(project_id, command['source_revision_id'], command['grant']['representation_id'])
        from . import songcraft
        from .draft_repair import context
        result=dict(repair_context=context(self.store,command),command=command, source=source, score=parsed, songcraft_materials=songcraft.resolve(self.store,command))
        if command['kind']=='direction':
            from .score_lyrics import read
            result.update(score_lyric_contract='score-lyrics/1',source_lyric_map=read(self.store,project_id,command['source_revision_id'])['rows'])
            from .lyric_density import analyze
            result['source_lyric_density']=analyze(source['snapshot']['abc'],source['snapshot']['brief']['lyrics'],result['source_lyric_map'])
        return result

    def propose(self, project_id, command_id, edits, summary, provenance, *, actor):
        command = self.get(project_id, command_id, actor)
        require(command['kind'] in ('pitch_edit', 'render'), 'UNSUPPORTED_COMMAND')
        require(command['state'] in ('working', 'rendering', 'completed'), 'COMMAND_STATE_CONFLICT')
        require(isinstance(provenance, dict) and set(provenance) == {'execution_id', 'context_policy'}, 'INVALID_PROVENANCE')
        identifier(provenance['execution_id'])
        require(provenance['context_policy'] == ('fresh_hermes_cli' if command['kind']=='pitch_edit' else 'worker_render_only'), 'INVALID_PROVENANCE')
        if command['kind'] == 'pitch_edit':
            result = self.store.apply_edit(project_id, command['source_revision_id'], command['grant']['request_id'],
                edits, summary, actor=actor, key=command_id)
            revision_id = result['revision']['revision_id']
        else:
            require(edits == [], 'INVALID_EDITS')
            revision_id = command['source_revision_id']
            if command.get('target_duration') is not None:
                source = self.store.get_revision(project_id, revision_id)
                brief = source['snapshot']['brief']
                if brief['max_duration'] != command['target_duration']:
                    revision = self.store.add_revision(project_id, source['snapshot']['abc'],
                        {**brief, 'max_duration':command['target_duration']},
                        f"仅调整生成时长上限：{brief['max_duration']:g} → {command['target_duration']:g} 秒；ABC、歌词、风格和种子保持原样",
                        parent_revision_id=revision_id, expected_parent_snapshot_sha256=command['source_snapshot_sha256'],
                        actor=actor, key=command_id+'_duration')
                    revision_id = revision['revision_id']
        require(self.bridge is not None, 'HERMES_UNAVAILABLE')
        job = self.bridge.prepare(project_id, revision_id, actor=actor, key=command_id, binding=command.get('production_binding'))
        def save(db):
            current = self._get(db, project_id, command_id, actor)
            require(current['state'] == 'working', 'COMMAND_STATE_CONFLICT')
            current.update(state='rendering', result_revision_id=revision_id, render_id=job['render_id'], provenance=provenance)
            return self._save(db, current, actor)
        return self.store._operation(actor, command_id, dict(op='producer_command_propose', project_id=project_id,
            edits=edits, summary=summary, provenance=provenance), save)

    def transition(self, project_id, command_id, action, *, actor, key, issue=None):
        require(action in ('cancel', 'issue', 'refresh'), 'INVALID_COMMAND_ACTION')
        if issue is not None:
            text(issue, 1000)
        def change(db):
            command = self._get(db, project_id, command_id, actor if action == 'issue' else None)
            if action == 'cancel':
                require(command['state'] in ('queued', 'preparing'), 'COMMAND_ALREADY_STARTED')
                command['state'] = 'cancelled'
            elif action == 'issue':
                require(command['state'] in ('working', 'rendering', 'needs_attention'), 'COMMAND_STATE_CONFLICT')
                command.update(state='needs_attention', issue=issue or 'WORKER_INTERRUPTED')
            elif command['render_id']:
                job = self.renders._job(db, project_id, command['render_id'])
                if job['state'] == 'succeeded':
                    command.update(state='completed', issue=None)
                elif job['state'] in ('failed', 'preflight_failed', 'cancelled'):
                    command.update(state='needs_attention', issue=job['state'])
                # Refresh observes only. Never issue another submission permission.
            return self._save(db, command, actor)
        result = self.store._operation(actor, key, dict(op='producer_command_'+action, project_id=project_id,
            command_id=command_id, issue=issue), change)
        if result['state']=='completed':
            from .quality_loop import resume_pending
            resume_pending(self,project_id)
        if result['state']=='needs_attention' and result.get('draft_repair'):
            from .draft_repair import resume
            resume(self)
        return result

    def feedback(self, project_id, revision_id, render_id, content, *, actor, key):
        text(content, 4000)
        def action(db):
            self.store._revision(db, project_id, revision_id)
            if render_id:
                job = self.renders._job(db, project_id, render_id)
                require(job['revision_id'] == revision_id and job['state'] == 'succeeded', 'AUDIO_BINDING_MISMATCH')
            value = dict(feedback_id=new_id('feedback'), revision_id=revision_id, render_id=render_id,
                content=content, created_at=now(), basis='producer_listening' if render_id else 'producer_text_review')
            self.store._event(db, project_id, 'producer_feedback', actor, {k:v for k,v in value.items() if k != 'created_at'})
            return value
        return self.store._operation(actor, key, dict(op='producer_feedback', project_id=project_id,
            revision_id=revision_id, render_id=render_id, content=content), action)
