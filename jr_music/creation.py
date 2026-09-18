"""From an idea to a confirmed plan and an original candidate.

Uses the producer command ledger and existing revision/render stores. No HEAD
mutation and no dependency on the historical Master experiment contracts.
"""
import secrets
from .store import require, text, identifier, new_id, now, canonical, sha
from .limits import MAX_DURATION_SECONDS


class CreationService:
    def __init__(self, producer):
        self.producer = producer
        self.store = producer.store

    def latest(self, db, project_id):
        plans = [c for c in self.producer._commands(db, project_id).values() if c['kind'] == 'plan']
        return plans[-1] if plans else None

    def _conversation(self, db, command, include_current=False):
        history = []
        current = command
        if include_current:
            history.append(dict(instruction=current['instruction'], result=current.get('result')))
        while current.get('parent_command_id'):
            current = self.producer._get(db, command['project_id'], current['parent_command_id'])
            history.append(dict(instruction=current['instruction'], result=current.get('result')))
        return list(reversed(history))

    def _command(self, project_id, assigned_to, kind, instruction, duration):
        from .lyric_density import guidance
        return dict(schema_version='producer-command/2', command_id=new_id('command'),
            singing_guidance=guidance(),
            project_id=project_id, source_revision_id=None, source_snapshot_sha256=None,
            assigned_to=assigned_to, kind=kind, instruction=instruction, allowed_bar_ids=[],
            grant=None, state='queued', result_revision_id=None, render_id=None,
            created_by='producer', created_at=now(), issue=None, provenance=None,
            target_duration=duration)

    def discuss(self, project_id, assigned_to, instruction, target_duration, parent_command_id, *, actor, key, songcraft_selection='none', mcp_alias=None):
        from . import songcraft
        songcraft.validate(songcraft_selection)
        require(actor == 'producer', 'PRODUCER_REQUIRED')
        require(self.producer.bridge and assigned_to in self.producer.bridge.bindings, 'HERMES_UNAVAILABLE')
        text(instruction, 4000)
        require(type(target_duration) in (int, float) and 1 <= target_duration <= MAX_DURATION_SECONDS, 'INVALID_DURATION')
        payload = dict(op='creation_discuss', project_id=project_id, assigned_to=assigned_to,
            instruction=instruction, target_duration=target_duration, parent_command_id=parent_command_id)
        if mcp_alias is not None:payload['mcp_alias']=mcp_alias
        if songcraft_selection!='none': payload['songcraft_selection']=songcraft_selection
        def action(db):
            self.store._project(db, project_id)
            parent = self.latest(db, project_id)
            require((parent['command_id'] if parent else None) == parent_command_id, 'STALE_CREATION_PLAN')
            require(not parent or parent['state'] in ('completed', 'needs_attention', 'cancelled'), 'CREATION_BUSY')
            command = self._command(project_id, assigned_to, 'plan', instruction, target_duration)
            command['production_binding']=self.producer.bridge.selection(assigned_to,mcp_alias)
            if songcraft_selection!='none': command['songcraft']=songcraft.freeze(self.store,db,songcraft_selection)
            if songcraft_selection=='professional': command.pop('singing_guidance',None)
            command.update(parent_command_id=parent_command_id,
                seed=parent['seed'] if parent else secrets.randbits(32), result=None, plan_sha256=None)
            return self.producer._save(db, command, actor)
        initial = self.store._operation(actor, key, payload, action)
        return self.producer.get(project_id, initial['command_id'])

    def confirm(self, project_id, plan_command_id, expected_plan_sha256, *, actor, key):
        require(actor == 'producer', 'PRODUCER_REQUIRED')
        payload = dict(op='creation_confirm', project_id=project_id,
            plan_command_id=plan_command_id, expected_plan_sha256=expected_plan_sha256)
        def action(db):
            plan = self.producer._get(db, project_id, plan_command_id)
            require(plan['kind'] == 'plan' and plan['state'] == 'completed', 'PLAN_NOT_READY')
            require(plan['plan_sha256'] == expected_plan_sha256, 'STALE_CREATION_PLAN')
            # Distinct clicks/keys on one plan still schedule exactly one draft.
            existing = next((c for c in self.producer._commands(db, project_id).values()
                if c['kind'] == 'compose' and c['plan_command_id'] == plan_command_id), None)
            if existing:
                return existing
            require(self.latest(db, project_id)['command_id'] == plan_command_id, 'STALE_CREATION_PLAN')
            command = self._command(project_id, plan['assigned_to'], 'compose',
                '根据已确认的创作方案生成原创首版并试听', plan['target_duration'])
            if plan.get('production_binding'):command['production_binding']=dict(plan['production_binding'])
            if plan.get('songcraft'): command['songcraft']=dict(plan['songcraft'])
            if plan.get('singing_guidance'):command['singing_guidance']=dict(plan['singing_guidance'])
            else:command.pop('singing_guidance',None)
            conversation = self._conversation(db, plan, include_current=True)
            command.update(plan_command_id=plan_command_id, plan_sha256=expected_plan_sha256,
                plan=plan['result']['plan'], seed=plan['seed'], confirmed_by=actor, confirmed_at=now(),
                conversation=conversation, conversation_sha256=sha(canonical(conversation)))
            return self.producer._save(db, command, actor)
        initial = self.store._operation(actor, key, payload, action)
        return self.producer.get(project_id, initial['command_id'])

    def packet(self, command):
        history = command.get('conversation', [])
        if command['kind'] == 'plan':
            # Ordered, explicit conversation; the worker uses a fresh CLI session.
            with self.store.connect() as db:
                history = self._conversation(db, command)
        from . import songcraft
        return dict(songcraft_materials=songcraft.resolve(self.store,command),score_lyric_contract='score-lyrics/1' if command['kind']=='compose' else None,command={k:v for k,v in command.items() if k != 'conversation'}, conversation=history,
            project_title=self.store.get_project(command['project_id'])['title'])

    def propose(self, project_id, command_id, result, provenance, *, actor, format_check=None):
        command = self.producer.get(project_id, command_id, actor)
        if format_check is not None:
            from .format_receipts import record
            record(self.producer,project_id,command_id,result,format_check,actor)
        if command['kind'] == 'direction':
            from .direction import propose
            return propose(self.producer, project_id, command_id, result, provenance, actor=actor)
        require(command['kind'] in ('plan', 'compose'), 'UNSUPPORTED_COMMAND')
        require(command['state'] in ('working', 'rendering', 'completed'), 'COMMAND_STATE_CONFLICT')
        require(isinstance(provenance, dict) and set(provenance) == {'execution_id','context_policy'}, 'INVALID_PROVENANCE')
        identifier(provenance['execution_id'])
        require(provenance['context_policy'] == 'fresh_hermes_cli', 'INVALID_PROVENANCE')
        require(isinstance(result, dict), 'INVALID_CREATIVE_PROPOSAL')
        # Freeze first response before semantic validation or any fallible render
        # preparation. A retry cannot replace it with a different draft.
        raw_payload = dict(op='creation_result_received', project_id=project_id,
                           command_id=command_id, result=result, provenance=provenance)
        def record(db):
            digest = self.store._blob(db, canonical(raw_payload))
            self.store._event(db, project_id, 'creation_result_received', actor,
                dict(command_id=command_id, result_sha256=digest))
            return digest
        result_sha = self.store._operation(actor, command_id+'_creative_raw', raw_payload, record)
        revision_id = render_id = None
        if command['kind'] == 'plan':
            require(set(result) == {'reply','plan'}, 'INVALID_CREATIVE_PROPOSAL')
            text(result['reply'], 6000)
            plan = result['plan']
            require(isinstance(plan, dict) and set(plan) == {'title','concept','style','structure','lyric_direction'}, 'INVALID_CREATION_PLAN')
            for value in plan.values():
                text(value, 4000)
            plan_content=dict(plan=plan, target_duration=command['target_duration'], seed=command['seed'])
            if command.get('songcraft'): plan_content['songcraft']=command['songcraft']
            if command.get('singing_guidance'):plan_content['singing_guidance']=command['singing_guidance']
            plan_sha = sha(canonical(plan_content))
        else:
            from . import professional_skills
            base_fields=set(result)-({'production_specs'} if professional_skills.required(command) else set())
            require(base_fields in ({'abc','lyrics','summary'},{'abc','lyrics','summary','lyric_map'}), 'INVALID_CREATIVE_PROPOSAL')
            from .score_repair import normalize_meter
            abc, meter_repairs = normalize_meter(result['abc'])
            summary = result['summary']
            text(summary, 1500)
            if meter_repairs:
                summary += '；拍数规范化（原始回复已留档）：' + '、'.join(f"第 {c['ordinal']} 小节 {c['before_beats']}→{c['after_beats']} 拍" for c in meter_repairs)
            brief = dict(style=command['plan']['style'], lyrics=result['lyrics'],
                checkpoint='yue2_3b_int8_convrot.safetensors', seed=command['seed'],
                max_duration=command['target_duration'])
            if 'lyric_map' in result:
                from . import score_lyrics
                score_lyrics.validate(abc,brief['lyrics'],result['lyric_map'],complete=True)
            from .lyric_density import analyze
            density=analyze(abc,brief['lyrics'],result.get('lyric_map',[]))
            specs=professional_skills.validate_specs(command,result,abc,brief['lyrics'])
            revision = self.store.add_revision(project_id, abc, brief, summary[:2000],
                actor=actor, key=command_id+'_original')
            revision_id = revision['revision_id']
            professional_skills.attach(self.store,project_id,revision_id,command,specs)
            if 'lyric_map' in result:
                score_lyrics.attach(self.store,project_id,revision_id,result['lyric_map'],actor=actor,key=command_id+'_lyric_map')
            require(self.producer.bridge is not None, 'HERMES_UNAVAILABLE')
            job = self.producer.bridge.prepare(project_id, revision_id, actor=actor, key=command_id, binding=command.get('production_binding'))
            render_id = job['render_id']
        def save(db):
            current = self.producer._get(db, project_id, command_id, actor)
            require(current['state'] == 'working', 'COMMAND_STATE_CONFLICT')
            current.update(provenance=provenance, result_sha256=result_sha, issue=None)
            if current['kind'] == 'plan':
                current.update(state='completed', result=result, plan_sha256=plan_sha)
            else:
                current.update(state='rendering', result_revision_id=revision_id, render_id=render_id, meter_repairs=meter_repairs)
                current['lyric_density_review']=density
            return self.producer._save(db, current, actor)
        return self.store._operation(actor, command_id+'_creative_accept', raw_payload, save)
