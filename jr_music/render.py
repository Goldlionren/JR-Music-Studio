"""Durable render ledger, independent of MCP's optional local job files.

Only a trusted executor may bind graphs, claim dispatch, reconcile evidence and
collect files. Agents can prepare/read/cancel local jobs; cannot assert success.
There is deliberately no generic set-state API and no automatic resubmission.
"""
from contextlib import ExitStack
import io
import json
from pathlib import Path
import sqlite3
import uuid

from .audio import AudioVerifier
from .store import StoreError, canonical, identifier, new_id, now, require, sha, text
from . import render_template as tpl

SERVER = dict(server_id='local_3060', url='http://127.0.0.1:8188', hardware='RTX 3060',
    transport='official-comfy-cli-local-dev', production_transport='hermes-comfy-mcp', targeted_cancel=False)
TERMINAL = {'succeeded', 'failed', 'cancelled', 'preflight_failed'}


class RenderService:
    def __init__(self, store, verifier=None):
        self.store = store
        self.verifier = verifier or AudioVerifier()

    @staticmethod
    def _job(db, project_id, render_id):
        row = db.execute('SELECT document FROM render_jobs WHERE project_id=? AND id=?',
                         (identifier(project_id), identifier(render_id))).fetchone()
        require(row is not None, 'RENDER_NOT_FOUND')
        return json.loads(row['document'])

    def get(self, project_id, render_id):
        with self.store.connect() as db:
            return self._job(db, project_id, render_id)

    def list(self, project_id, revision_id):
        with self.store.connect() as db:
            self.store._revision(db, project_id, revision_id)
            return [json.loads(row[0]) for row in db.execute('SELECT document FROM render_jobs WHERE project_id=? AND revision_id=? ORDER BY rowid',
                                                           (project_id, revision_id))]

    def _save(self, db, job, actor, event, evidence=None):
        job['generation'] += 1
        job['updated_at'] = now()
        data = dict(render_id=job['render_id'], state=job['state'], generation=job['generation'])
        if evidence is not None:
            digest = self.store._blob(db, canonical(evidence))
            data['evidence_sha256'] = digest
            job['evidence'].append(dict(event=event, sha256=digest))
        try:
            db.execute('UPDATE render_jobs SET prompt_id=?,document=? WHERE id=?',
                       (job['prompt_id'], canonical(job), job['render_id']))
        except sqlite3.IntegrityError as exc:
            raise StoreError('REMOTE_PROMPT_ALREADY_BOUND') from exc
        self.store._event(db, job['project_id'], event, actor, data)
        return job

    def _write(self, project_id, render_id, actor, action):
        identifier(actor)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                result = action(db, self._job(db, project_id, render_id))
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    def prepare(self, project_id, revision_id, *, actor, key, server_id='local_3060', environment_required=False, production_binding=None, server=None):
        require(server is None or production_binding is not None and production_binding.get('schema_version')=='hermes-binding/2', 'SERVER_NOT_APPROVED')
        server = server or SERVER
        require(server_id == server['server_id'], 'SERVER_NOT_APPROVED')
        require(type(environment_required) is bool,'INVALID_ENVIRONMENT_POLICY')
        payload = dict(op='prepare_render', project_id=project_id, revision_id=revision_id,
                       server_id=server_id, template_id=tpl.TEMPLATE_ID)
        if environment_required:
            payload['environment_required']=True
        if production_binding is not None:
            from .hermes import validate_binding
            validate_binding(production_binding, actor)
            require(environment_required, 'ENVIRONMENT_REQUIRED')
            payload['production_binding'] = production_binding
        def action(db):
            revision = self.store._revision(db, project_id, revision_id)
            snapshot = self.store._snapshot(db, revision)
            template_id=tpl.TEXT_TEMPLATE_ID if snapshot.get('generation',{}).get('abc_planning') else tpl.TEMPLATE_ID
            rid = new_id('render')
            params = tpl.parameters(snapshot, 'audio/JRMusic/' + rid)
            fragments = {name: json.loads((tpl.ROOT / 'fragments' / (name + '.json')).read_text(encoding='utf-8'))
                         for name in ('yue2_model', 'yue2_text_render' if template_id==tpl.TEXT_TEMPLATE_ID else 'yue2_render')}
            job = dict(schema_version='render/1', render_request_id=new_id('render_request'), render_id=rid,
                project_id=project_id, revision_id=revision_id, snapshot_sha256=revision['snapshot_sha256'],
                abc_sha256=revision['abc_sha256'], brief_sha256=sha(canonical(snapshot['brief'])),
                lyrics_sha256=sha(snapshot['brief']['lyrics'].encode('utf-8')), seed_decimal=str(params['seed']),
                master_blend=None, template_id=template_id, template_sha256=sha(canonical(tpl.template(template_id))),
                fragments_sha256={n: sha(canonical(f)) for n, f in fragments.items()},
                source_bundle_sha256=self.store._blob(db, canonical(dict(blueprint=tpl.blueprint(params,template_id), fragments=fragments))),
                parameters=params, server=server, checkpoint=dict(name=params['checkpoint'], sha256=None, verification='filename_only'),
                state='created', generation=0, prompt_id=None, workflow_sha256=None, claim_id=None,
                created_at=now(), updated_at=now(), created_by=actor, evidence=[], assets=[],
                last_issue=None, cancel_requested=False, remote_execution_possible=False)
            if environment_required:
                job.update(schema_version='render/2',environment_sha256=None)
            if production_binding is not None:
                job.update(schema_version='render/3', production_binding=production_binding,
                           server={**server, 'transport': 'hermes-comfy-mcp'})
                if production_binding['schema_version']=='hermes-binding/2':job.update(schema_version='render/4',routing_key=key)
            db.execute('INSERT INTO render_jobs VALUES(?,?,?,?,?,?)', (rid, project_id, revision_id, server_id, None, canonical(job)))
            self.store._event(db, project_id, 'render_prepared', actor, dict(render_id=rid, revision_id=revision_id))
            return job
        return self.store._operation(actor, key, payload, action)

    def bind_environment(self,project_id,render_id,environment,*,actor):
        from .render_environment import validate_environment
        def action(db,job):
            require(job['schema_version'] in ('render/2', 'render/3', 'render/4') and job['state'] in ('created','prepared'),'RENDER_STATE_CONFLICT')
            validate_environment(environment,job)
            digest=sha(canonical(environment))
            require(job['environment_sha256'] in (None,digest),'ENVIRONMENT_IMMUTABLE')
            if job['environment_sha256']==digest: return job
            job['environment_sha256']=self.store._blob(db,canonical(environment))
            ck=environment['checkpoint']
            job['checkpoint']={k:ck[k] for k in ('name','size','sha256','verification')}
            return self._save(db,job,actor,'render_environment_bound',environment)
        return self._write(project_id,render_id,actor,action)

    def bundle(self, project_id, render_id):
        job = self.get(project_id, render_id)
        with self.store.connect() as db:
            bundle = db.execute('SELECT content FROM blobs WHERE hash=?', (job['source_bundle_sha256'],)).fetchone()[0]
            require(sha(bundle) == job['source_bundle_sha256'], 'CORRUPT_ASSET')
            return json.loads(bundle)

    def bind_workflow(self, project_id, render_id, workflow, preflight, *, actor):
        def action(db, job):
            require(job['state'] in ('created', 'prepared'), 'RENDER_STATE_CONFLICT')
            require(isinstance(preflight, dict) and preflight.get('valid') is True and preflight.get('server_id') == job['server']['server_id'], 'PREFLIGHT_REQUIRED')
            graph = tpl.validate(workflow, job['parameters'], job['template_sha256'],job['template_id'])
            digest = sha(canonical(graph))
            require(preflight.get('workflow_sha256') == digest, 'PREFLIGHT_WORKFLOW_MISMATCH')
            require(job['workflow_sha256'] in (None, digest), 'WORKFLOW_ALREADY_BOUND')
            job['workflow_sha256'] = self.store._blob(db, canonical(graph))
            job['state'] = 'prepared'
            return self._save(db, job, actor, 'render_workflow_bound', preflight)
        return self._write(project_id, render_id, actor, action)

    def workflow(self, project_id, render_id):
        job = self.get(project_id, render_id)
        require(job['workflow_sha256'] is not None, 'WORKFLOW_NOT_BOUND')
        with self.store.connect() as db:
            content = db.execute('SELECT content FROM blobs WHERE hash=?', (job['workflow_sha256'],)).fetchone()[0]
            require(sha(content) == job['workflow_sha256'], 'CORRUPT_ASSET')
            return json.loads(content)

    def claim(self, project_id, render_id, *, actor):
        """One-shot dispatch permission. Replay is DENIED, never reissued.

        A crash after this commit requires reconciliation, even if before send.
        Exactly-once remote submission cannot be guaranteed by this local lock.
        """
        def action(db, job):
            require(job['state'] == 'prepared' and job['claim_id'] is None, 'DISPATCH_ALREADY_CLAIMED')
            if job['schema_version'] in ('render/2', 'render/3', 'render/4'):
                require(job.get('environment_sha256') is not None,'ENVIRONMENT_REQUIRED')
            job.update(state='submitting', claim_id=new_id('claim'), remote_execution_possible=True)
            return self._save(db, job, actor, 'render_dispatch_claimed')
        return self._write(project_id, render_id, actor, action)

    def issue(self, project_id, render_id, code, *, actor):
        require(code in ('SUBMISSION_UNKNOWN', 'CONNECTION_LOST', 'WAIT_TIMEOUT', 'DOWNLOAD_FAILED',
            'AUDIO_DECODE_FAILED', 'AUDIO_TOOL_UNAVAILABLE', 'ARTIFACT_MISSING', 'HISTORY_MISSING', 'PREFLIGHT_FAILED'), 'INVALID_ISSUE')
        def action(db, job):
            if job['state'] in TERMINAL:
                return job
            if code == 'PREFLIGHT_FAILED':
                require(job['state'] == 'created', 'RENDER_STATE_CONFLICT')
                job['state'] = 'preflight_failed'
            elif job['state'] == 'submitting':
                job['state'] = 'submission_unknown'
            elif code == 'HISTORY_MISSING':
                require(job['state'] not in ('created', 'prepared'), 'RENDER_STATE_CONFLICT')
                job['state'] = 'lost'  # Evidence missing, NOT proof the GPU never ran.
            job['last_issue'] = code
            return self._save(db, job, actor, 'render_issue', dict(code=code))
        return self._write(project_id, render_id, actor, action)

    def receipt(self, project_id, render_id, server_id, prompt_id, *, actor):
        require(isinstance(prompt_id, str), 'INVALID_PROMPT_ID')
        try:
            require(str(uuid.UUID(prompt_id)) == prompt_id, 'INVALID_PROMPT_ID')
        except (ValueError, AttributeError) as exc:
            raise StoreError('INVALID_PROMPT_ID') from exc
        def action(db, job):
            require(server_id == job['server']['server_id'], 'SERVER_MISMATCH')
            require(job['prompt_id'] in (None, prompt_id), 'REMOTE_PROMPT_MISMATCH')
            if job['prompt_id'] == prompt_id:
                return job
            require(job['state'] in ('submitting', 'submission_unknown', 'lost'), 'RENDER_STATE_CONFLICT')
            job.update(prompt_id=prompt_id, state='queued', last_issue=None)
            return self._save(db, job, actor, 'render_receipt', dict(server_id=server_id, prompt_id=prompt_id))
        return self._write(project_id, render_id, actor, action)

    def _match_prompt(self, job, prompt):
        require(isinstance(prompt, list) and len(prompt) >= 3, 'INVALID_REMOTE_EVIDENCE')
        require(prompt[1] == job['prompt_id'], 'REMOTE_PROMPT_MISMATCH')
        return tpl.remote_match(self.workflow(job['project_id'],job['render_id']),prompt[2],allow_float_coercion=job['schema_version'] in ('render/2', 'render/3', 'render/4'))

    def observe(self, project_id, render_id, server_id, *, history=None, queue=None, actor):
        require((history is None) != (queue is None), 'INVALID_REMOTE_EVIDENCE')
        def action(db, job):
            require(server_id == job['server']['server_id'], 'SERVER_MISMATCH')
            require(job['prompt_id'] is not None, 'REMOTE_PROMPT_REQUIRED')
            if job['state'] in TERMINAL:
                return job
            if history is not None:
                require(isinstance(history, dict), 'INVALID_REMOTE_EVIDENCE')
                matching=self._match_prompt(job, history.get('prompt'))
                if job['schema_version'] in ('render/2', 'render/3', 'render/4'):job['workflow_match']=matching
                status = history.get('status', {})
                if status.get('status_str') == 'error':
                    messages = status.get('messages', [])
                    interrupted = any(isinstance(m, list) and m and m[0] == 'execution_interrupted' for m in messages)
                    job['state'] = 'cancelled' if interrupted else 'failed'
                    job['last_issue'] = 'REMOTE_INTERRUPTED' if interrupted else 'REMOTE_EXECUTION_FAILED'
                elif status.get('completed') is True and status.get('status_str') == 'success':
                    outputs = history.get('outputs', {})
                    if job['template_id']==tpl.TEXT_TEMPLATE_ID:
                        planned=outputs.get('155',{}).get('text')
                        require(isinstance(planned,list) and len(planned)==1 and isinstance(planned[0],str)
                            and 0<len(planned[0].encode('utf-8'))<=1_000_000,'GENERATED_SCORE_MISSING')
                    else:require(outputs.get('155', {}).get('text') == [job['parameters']['abc']], 'ECHOED_ABC_MISMATCH')
                    audio = outputs.get('152', {}).get('audio', [])
                    require(isinstance(audio, list) and len(audio) == 1, 'ARTIFACT_MISSING')
                    desc = audio[0]
                    require(isinstance(desc, dict) and set(desc) == {'filename', 'subfolder', 'type'} and desc['type'] == 'output', 'INVALID_OUTPUT_DESCRIPTOR')
                    require(isinstance(desc['filename'], str) and '/' not in desc['filename'] and '\\' not in desc['filename']
                            and desc['filename'].endswith('.flac') and desc['filename'] not in ('.', '..'), 'INVALID_OUTPUT_DESCRIPTOR')
                    require(isinstance(desc['subfolder'], str) and not desc['subfolder'].startswith(('/', '\\'))
                            and '..' not in desc['subfolder'].replace('\\', '/').split('/') and ':' not in desc['subfolder'], 'INVALID_OUTPUT_DESCRIPTOR')
                    folder, basename = job['parameters']['filename_prefix'].rsplit('/', 1)
                    require(desc['subfolder'].replace('\\', '/') == folder and desc['filename'].startswith(basename + '_'), 'OUTPUT_PREFIX_MISMATCH')
                    job.update(state='collecting', output_descriptor=desc, last_issue=None,
                               history_sha256=self.store._blob(db, canonical(history)))
                else:
                    raise StoreError('INCOMPLETE_REMOTE_HISTORY')
                return self._save(db, job, actor, 'render_history_observed', history)
            require(isinstance(queue, dict), 'INVALID_REMOTE_EVIDENCE')
            hits = [(state, p) for field, state in [('queue_running', 'running'), ('queue_pending', 'queued')]
                    for p in queue.get(field, []) if isinstance(p, list) and len(p) > 1 and p[1] == job['prompt_id']]
            require(len(hits) == 1, 'REMOTE_PROMPT_NOT_IN_QUEUE')
            state, prompt = hits[0]
            matching=self._match_prompt(job, prompt)
            if job['schema_version'] in ('render/2', 'render/3', 'render/4'):job['workflow_match']=matching
            if job['state'] == 'collecting' or (job['state'] == 'running' and state == 'queued'):
                return job
            job.update(state=state, last_issue=None)
            return self._save(db, job, actor, 'render_queue_observed', dict(state=state, prompt=prompt))
        return self._write(project_id, render_id, actor, action)

    def cancel(self, project_id, render_id, *, actor):
        def action(db, job):
            if job['state'] in TERMINAL:
                return job
            job['cancel_requested'] = True
            if job['state'] in ('created', 'prepared'):
                job['state'] = 'cancelled'
            else:
                # No global /interrupt or queue clear. Until tested, MCP targeted
                # cancellation is unavailable; completion can still win the race.
                job['last_issue'] = 'TARGETED_CANCEL_UNAVAILABLE'
            return self._save(db, job, actor, 'render_cancel_requested')
        return self._write(project_id, render_id, actor, action)

    def collect(self, project_id, render_id, stream, *, actor, length=None, expected_sha256=None):
        job = self.get(project_id, render_id)
        if job['state'] == 'succeeded':
            return job
        require(job['state'] == 'collecting' and job.get('history_sha256'), 'RENDER_STATE_CONFLICT')
        with ExitStack() as stack:
            source = stack.enter_context(self.store.cas.stage(stream, length=length))
            if expected_sha256 is not None:
                require(source['sha256'] == expected_sha256, 'TRANSFER_HASH_MISMATCH')
            wav_path = source['path'].with_suffix('.wav')
            stack.callback(wav_path.unlink, missing_ok=True)
            verification = self.verifier.verify(source['path'], wav_path, job['parameters']['max_duration'])
            with wav_path.open('rb') as handle:
                wav = stack.enter_context(self.store.cas.stage(handle))
            def action(db, current):
                if current['state'] == 'succeeded':
                    return current
                require(current['state'] == 'collecting' and current.get('history_sha256') == job['history_sha256'], 'RENDER_STATE_CONFLICT')
                assets = [self.store.file_asset(db, project_id, job['revision_id'], staged, name, media, 'decoded_pcm_verified')
                          for staged, name, media in [(source, 'native.flac', 'audio/flac'), (wav, 'delivery.wav', 'audio/wav')]]
                history = json.loads(db.execute('SELECT content FROM blobs WHERE hash=?', (job['history_sha256'],)).fetchone()[0])
                if job['template_id']==tpl.TEXT_TEMPLATE_ID:
                    planned=history.get('outputs',{}).get('155',{}).get('text',[])
                    if isinstance(planned,list) and len(planned)==1 and isinstance(planned[0],str) and 0<len(planned[0].encode())<=1_000_000:
                        with self.store.cas.stage(io.BytesIO(planned[0].encode('utf-8'))) as staged:
                            assets.append(self.store.file_asset(db,project_id,job['revision_id'],staged,'generated-score.abc','text/plain','server_generated'))
                manifest = dict(schema_version='render-manifest/1', render_id=render_id,
                    render_request_id=job['render_request_id'], revision_id=job['revision_id'], project_id=project_id,
                    snapshot_sha256=job['snapshot_sha256'], abc_sha256=job['abc_sha256'], brief_sha256=job['brief_sha256'],
                    lyrics_sha256=job['lyrics_sha256'], seed_decimal=job['seed_decimal'], master_blend=job['master_blend'],
                    template_id=job['template_id'], template_sha256=job['template_sha256'], fragments_sha256=job['fragments_sha256'],
                    source_bundle_sha256=job['source_bundle_sha256'], workflow_sha256=job['workflow_sha256'],
                    workflow=json.loads(db.execute('SELECT content FROM blobs WHERE hash=?', (job['workflow_sha256'],)).fetchone()[0]),
                    server=job['server'], checkpoint=job['checkpoint'], prompt_id=job['prompt_id'],
                    output_descriptor=job['output_descriptor'], history=history, history_sha256=job['history_sha256'],
                    execution_evidence=[dict(event=e['event'], sha256=e['sha256'], data=json.loads(db.execute('SELECT content FROM blobs WHERE hash=?', (e['sha256'],)).fetchone()[0])) for e in job['evidence']],
                    audio_verification=verification, assets=assets, collected_at=now(),
                    limitations=['Audio adherence to ABC is not verified.', 'No subjective listening performed.',
                                  'Remote evidence is trusted executor evidence, not cryptographic server attestation.'])
                if job['schema_version'] in ('render/2', 'render/3', 'render/4'):
                    manifest.update(schema_version='render-manifest/2',environment_sha256=job['environment_sha256'],
                        environment=json.loads(db.execute('SELECT content FROM blobs WHERE hash=?',(job['environment_sha256'],)).fetchone()[0]),
                        workflow_match=job['workflow_match'])
                if job['schema_version'] in ('render/3','render/4'):
                    manifest.update(schema_version='render-manifest/3', production_binding=job['production_binding'])
                    if job['schema_version']=='render/4':manifest['schema_version']='render-manifest/4'
                    manifest['limitations'].append('Hermes/MCP routing is configured by the operator; history verifies the submitted graph, not which process invoked it.')
                with self.store.cas.stage(io.BytesIO(canonical(manifest))) as staged:
                    assets.append(self.store.file_asset(db, project_id, job['revision_id'], staged, 'render-manifest.json',
                                                       'application/json', 'server_generated'))
                current.update(state='succeeded', assets=assets, verification=verification, last_issue=None,
                               remote_execution_possible=False)
                return self._save(db, current, actor, 'render_collected', dict(asset_ids=[a['asset_id'] for a in assets]))
            return self._write(project_id, render_id, actor, action)
