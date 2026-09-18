"""Append-only experimental evidence on the existing project/version store.

Trusted in-process API; human-only feedback authorization belongs to HTTP.
Candidates, reviews and feedback never select a project HEAD.
"""
import json
from .store import canonical, sha, require, identifier, new_id, now
from .music_contracts import validate
from .master import MasterLibrary, guidance, channel_guidance


def contract_version(document):
    version = document.get('schema_version', 'music-experiment/1')
    require(version in ('music-experiment/1', 'music-experiment/2'), 'UNSUPPORTED_EXPERIMENT_VERSION')
    return 2 if version.endswith('/2') else 1


def variants(experiment):
    return ('baseline', 'master', 'master_v11') if contract_version(experiment) == 2 else ('baseline', 'master')
from .critic import critique


class MusicService:
    def __init__(self, store, library=None):
        self.store = store
        self.library = library or MasterLibrary()

    @staticmethod
    def _blob(db, digest):
        row = db.execute('SELECT content FROM blobs WHERE hash=?', (digest,)).fetchone()
        require(row is not None and sha(row[0]) == digest, 'CORRUPT_MUSIC_RECORD')
        return json.loads(row[0])

    def _experiment(self, db, project_id, experiment_id):
        row = db.execute('SELECT blob_hash FROM music_experiments WHERE project_id=? AND id=?',
                         (identifier(project_id), identifier(experiment_id))).fetchone()
        require(row is not None, 'EXPERIMENT_NOT_FOUND')
        return self._blob(db, row[0])

    def create(self, project_id, spec, *, actor, key):
        version = contract_version(spec)
        validate('music-experiment', 'create', spec, version)
        def action(db):
            self.store._project(db, project_id)
            bundle = self.library.load(spec['master_id'], spec['master_version'])
            digest = sha(canonical(bundle))
            prior = db.execute('SELECT blob_hash FROM master_bundles WHERE master_id=? AND version=?',
                               (spec['master_id'], spec['master_version'])).fetchone()
            require(prior is None or prior[0] == digest, 'MASTER_VERSION_IMMUTABLE')
            self.store._blob(db, canonical(bundle))
            db.execute('INSERT OR IGNORE INTO master_bundles VALUES(?,?,?)', (spec['master_id'], spec['master_version'], digest))
            guides = dict(baseline=guidance(spec['brief']), master=guidance(spec['brief'], bundle, spec['strength']))
            document = dict(schema_version='music-experiment/1', experiment_id=new_id('experiment'), project_id=project_id,
                created_at=now(), created_by=actor, spec=spec, master_bundle=bundle, guidance=guides,
                guidance_sha256={k: sha(canonical(v)) for k,v in guides.items()},
                design='one pair; fixed brief and render controls; no automatic winner',
                producer_decision=None)
            if version == 2:
                refined = self.library.load(spec['master_id'], spec['refined_version'])
                require('refinement' in refined and refined['refinement']['parent_fingerprint'] == bundle['fingerprint'], 'MASTER_PARENT_MISMATCH')
                digest = sha(canonical(refined))
                prior = db.execute('SELECT blob_hash FROM master_bundles WHERE master_id=? AND version=?', (spec['master_id'], spec['refined_version'])).fetchone()
                require(prior is None or prior[0] == digest, 'MASTER_VERSION_IMMUTABLE')
                self.store._blob(db, canonical(refined))
                db.execute('INSERT OR IGNORE INTO master_bundles VALUES(?,?,?)', (spec['master_id'], spec['refined_version'], digest))
                # Equal domain priorities isolate guideline changes rather than a strength change.
                require(all(s == spec['strength'] for s in spec['channel_strengths'].values()), 'THREE_ARM_STRENGTH_CONFOUNDED')
                guides = dict(baseline=channel_guidance(spec['brief'], strengths=dict(lyrics=0, melody=0, other=0)),
                    master=channel_guidance(spec['brief'], bundle, strengths=spec['channel_strengths']),
                    master_v11=channel_guidance(spec['brief'], refined, strengths=spec['channel_strengths']))
                document.update(schema_version='music-experiment/2', refined_bundle=refined, guidance=guides,
                    guidance_sha256={k: sha(canonical(v)) for k,v in guides.items()},
                    design='Fresh three-arm composition; fixed brief and render controls; open-label producer listening. No automatic winner or causal efficacy claim.')
            validate('music-experiment','record',document, version)
            digest = self.store._blob(db, canonical(document))
            db.execute('INSERT INTO music_experiments VALUES(?,?,?)', (document['experiment_id'], project_id, digest))
            self.store._event(db, project_id, 'music_experiment_created', actor, dict(experiment_id=document['experiment_id'], sha256=digest))
            return document
        return self.store._operation(actor, key, dict(op='create_music_experiment', project_id=project_id, spec=spec), action)

    def list(self, project_id):
        with self.store.connect() as db:
            self.store._project(db, project_id)
            return [self._blob(db, r[0]) for r in db.execute('SELECT blob_hash FROM music_experiments WHERE project_id=? ORDER BY rowid', (project_id,))]

    def get(self, project_id, experiment_id):
        with self.store.connect() as db:
            result = self._experiment(db, project_id, experiment_id)
            for label, table in [('candidates','music_candidates'), ('reviews','music_reviews'), ('feedback','music_feedback')]:
                result[label] = [self._blob(db,r[0]) for r in db.execute(f'SELECT blob_hash FROM {table} WHERE experiment_id=? ORDER BY rowid', (experiment_id,))]
            return validate('music-experiment','view',result, contract_version(result))

    def candidate(self, project_id, experiment_id, proposal, *, actor, key):
        def action(db):
            experiment = self._experiment(db, project_id, experiment_id)
            version = contract_version(experiment)
            validate('music-experiment', 'candidate', proposal, version)
            variant = proposal['variant']
            require(variant in variants(experiment), 'INVALID_VARIANT')
            require(proposal['guidance_sha256'] == experiment['guidance_sha256'][variant], 'GUIDANCE_MISMATCH')
            prior = db.execute('SELECT variant,blob_hash FROM music_candidates WHERE experiment_id=?', (experiment_id,)).fetchall()
            require(not any(r[0] == variant for r in prior), 'CANDIDATE_ALREADY_RECORDED')
            require(not any(self._blob(db,r[1])['invocation_id'] == proposal['invocation_id'] for r in prior), 'COMPOSER_INVOCATION_REUSED')
            if version == 2:
                generation = proposal['generation']
                row = db.execute("SELECT blob_hash FROM music_assessments WHERE id=? AND experiment_id=? AND kind='composer_request'", (generation['input_assessment_id'], experiment_id)).fetchone()
                require(row is not None, 'COMPOSER_REQUEST_NOT_FOUND')
                packet = self._blob(db, row[0])['payload']
                require(packet['variant'] == variant and packet['request_sha256'] == generation['input_sha256'] and packet['request']['guidance_sha256'] == proposal['guidance_sha256'], 'COMPOSER_INPUT_MISMATCH')
                require(proposal['generation']['context_policy'] == experiment['spec']['composer']['independence'], 'COMPOSER_CONTEXT_MISMATCH')
                if proposal['generation']['context_policy'] == 'separate_contexts':
                    require(not any(self._blob(db,r[1])['generation']['context_id'] == proposal['generation']['context_id'] for r in prior), 'COMPOSER_CONTEXT_REUSED')
            brief = dict(experiment['spec']['controls'], lyrics=proposal['lyrics'])
            summary = ('M1-05B v1.1 ' if version == 2 else 'M1-05A ') + variant + ' candidate; producer decision pending'
            self.store.validate_revision(proposal['abc'], brief, summary)
            revision = self.store._insert_revision(db, project_id, proposal['abc'], brief, summary, None, actor)
            record = dict(schema_version='music-experiment/1', experiment_id=experiment_id, variant=variant, revision=revision,
                invocation_id=proposal['invocation_id'], composer=experiment['spec']['composer'],
                provenance='caller-declared generation identity; not an attested external model call',
                guidance_sha256=proposal['guidance_sha256'], notes=proposal['notes'], created_at=now(), created_by=actor)
            if version == 2:
                record.update(schema_version='music-experiment/2', generation=proposal['generation'])
            validate('music-experiment','candidate-record',record, version)
            digest = self.store._blob(db, canonical(record))
            db.execute('INSERT INTO music_candidates VALUES(?,?,?,?)', (experiment_id, variant, revision['revision_id'], digest))
            self.store._event(db, project_id, 'music_candidate_recorded', actor, dict(experiment_id=experiment_id, variant=variant, revision_id=revision['revision_id'], sha256=digest))
            return record
        return self.store._operation(actor, key, dict(op='music_candidate', project_id=project_id, experiment_id=experiment_id, proposal=proposal), action)

    def review(self, project_id, experiment_id, variant, *, reference_texts=None, render_id=None, actor, key):
        references = [] if reference_texts is None else reference_texts
        def action(db):
            experiment = self._experiment(db, project_id, experiment_id)
            version = contract_version(experiment)
            require(variant in variants(experiment), 'INVALID_VARIANT')
            row = db.execute('SELECT revision_id,blob_hash FROM music_candidates WHERE experiment_id=? AND variant=?', (experiment_id,variant)).fetchone()
            require(row is not None, 'CANDIDATE_NOT_FOUND')
            candidate = self._blob(db, row['blob_hash'])
            revision = self.store._revision(db, project_id, row['revision_id'])
            snapshot = self.store._snapshot(db, revision)
            if render_id is not None:
                render = db.execute('SELECT document FROM render_jobs WHERE id=? AND project_id=? AND revision_id=?',
                                    (identifier(render_id),project_id,revision['revision_id'])).fetchone()
                require(render is not None and json.loads(render[0])['state'] == 'succeeded', 'VERIFIED_RENDER_REQUIRED')
            proofs = [r[0] for r in db.execute('SELECT id FROM edit_verifications WHERE result_revision_id=?', (revision['revision_id'],))]
            bundle = experiment['refined_bundle'] if variant == 'master_v11' else experiment['master_bundle']
            request = dict(schema_version='music-critic/' + str(version), experiment_id=experiment_id, project_id=project_id, variant=variant,
                revision_id=revision['revision_id'], snapshot_sha256=revision['snapshot_sha256'], composer_invocation_id=candidate['invocation_id'],
                brief_sha256=sha(canonical(experiment['spec']['brief'])), master_fingerprint=bundle['fingerprint'],
                render_id=render_id, protected_edit_verification_ids=proofs, reference_texts=references)
            # Critic recomputes from immutable inputs. Caller cannot supply ratings.
            result = critique(request, snapshot, experiment['spec']['brief'], bundle['critic-rubric'])
            record = dict(schema_version='music-critic/' + str(version), review_id=new_id('review'), request=request, result=result, created_by=actor, created_at=now())
            validate('music-critic','review-record',record, version)
            digest = self.store._blob(db, canonical(record))
            db.execute('INSERT INTO music_reviews VALUES(?,?,?,?)', (record['review_id'],experiment_id,variant,digest))
            self.store._event(db,project_id,'music_review_recorded',actor,dict(experiment_id=experiment_id,review_id=record['review_id'],sha256=digest))
            return record
        return self.store._operation(actor,key,dict(op='music_review',project_id=project_id,experiment_id=experiment_id,variant=variant,reference_texts=references,render_id=render_id),action)

    def feedback(self, project_id, experiment_id, feedback, *, actor, key):
        def action(db):
            return self.append_feedback(db,project_id,experiment_id,feedback,actor)
        return self.store._operation(actor,key,dict(op='music_feedback',project_id=project_id,experiment_id=experiment_id,feedback=feedback),action)

    def append_feedback(self,db,project_id,experiment_id,feedback,actor):
        experiment = self._experiment(db,project_id,experiment_id)
        version = contract_version(experiment)
        validate('music-experiment','feedback',feedback, version)
        require(db.execute('SELECT count(*) FROM music_candidates WHERE experiment_id=?',(experiment_id,)).fetchone()[0] == len(variants(experiment)), 'EXPERIMENT_INCOMPLETE')
        record = dict(feedback, schema_version='music-experiment/' + str(version), feedback_id=new_id('feedback'),experiment_id=experiment_id,
                      created_at=now(),created_by=actor,head_changed=False)
        validate('music-experiment','feedback-record',record, version)
        digest = self.store._blob(db,canonical(record))
        db.execute('INSERT INTO music_feedback VALUES(?,?,?)',(record['feedback_id'],experiment_id,digest))
        self.store._event(db,project_id,'music_feedback_recorded',actor,dict(experiment_id=experiment_id,feedback_id=record['feedback_id'],sha256=digest))
        return record
