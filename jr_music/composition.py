"""Frozen single-candidate packets for external Hermes composition.

This module does not invoke a model. Fresh-context declarations are not attestations.
"""
from copy import deepcopy
import json
from .assessments import AssessmentService
from .music import MusicService, contract_version, variants
from .music_contracts import ROOT, validate
from .store import canonical, sha, require


class CompositionService:
    def __init__(self, store):
        self.store = store
        self.music = MusicService(store)
        self.assessments = AssessmentService(store)

    def prepare(self, project_id, experiment_id, variant, *, actor, key):
        def action(db):
            exp = self.music._experiment(db, project_id, experiment_id)
            require(contract_version(exp) == 2 and variant in variants(exp), 'THREE_ARM_EXPERIMENT_REQUIRED')
            # A second key cannot accidentally create different instructions for this arm.
            for row in db.execute("SELECT blob_hash FROM music_assessments WHERE experiment_id=? AND kind='composer_request'", (experiment_id,)):
                record = self.music._blob(db, row[0])
                if record['payload']['variant'] == variant: return record
            guide = deepcopy(exp['guidance'][variant])
            guide.pop('master')  # Guidance identity/version is not needed to compose.
            request = dict(schema_version='composer-request/1', guidance=guide,
                guidance_sha256=exp['guidance_sha256'][variant], render_controls=exp['spec']['controls'],
                task='Compose one original Mandarin lyric and ABC melody for the supplied brief. Use only this packet. Do not inspect other drafts, prior reviews, project files, previous conversation or memory. Do not ask another agent. Do not render or use ComfyUI. Return a single JSON object conforming to output_schema, without Markdown fences.',
                score_contract=dict(format='ABC within JR yue2-subset/1.0.0', structure='8 bars: 4 verse + 4 chorus; one Vocal voice; each bar exactly 4 quarter beats; lyrics 4 verse lines + 4 chorus lines with [verse] and [chorus] markers.',
                    headers='X:1\nT:' + exp['spec']['brief']['title'] + '\nM:4/4\nL:1/8\nQ:1/4=80\nK:C\nV:Vocal',
                    syntax='Use C major natural notes C D E F G A B and octave marks comma/apostrophe, z rests, integer durations; optional quoted chord symbols. Use % verse and % chorus comments before the corresponding four bars. No w: lyrics, accidentals, tuplets, repeats, inline keys, grace notes or ornaments. C2 D2 E2 G2 is one duration example, not a tune to reuse.'),
                execution_policy='Use a fresh Hermes conversation for this request, one first complete draft. Report your actual available model identity; if unavailable say unknown. context_id must identify this new conversation; if no session ID is exposed, create a unique local label and say it is operator-declared in limitations. Do not claim independently attested context isolation.',
                output_schema=json.loads((ROOT/'schemas/composer-output/1/result.schema.json').read_text(encoding='utf-8')))
            return self.assessments.append(db, project_id, experiment_id, 'composer_request',
                dict(variant=variant, request=request, request_sha256=sha(canonical(request)),
                    executor='yinyue_hermes_fresh_conversation', context_attested=False), actor)
        return self.store._operation(actor, key, dict(op='composer_request', project_id=project_id, experiment_id=experiment_id, variant=variant), action)

    def accept(self, project_id, experiment_id, result, *, actor, key):
        validate('composer-output', 'result', result)
        with self.store.connect() as db:
            self.music._experiment(db, project_id, experiment_id)
            requests = [self.music._blob(db, r[0]) for r in db.execute("SELECT blob_hash FROM music_assessments WHERE experiment_id=? AND kind='composer_request'", (experiment_id,))]
        matches = [r for r in requests if r['payload']['request_sha256'] == result['request_sha256']]
        require(len(matches) == 1, 'COMPOSER_INPUT_MISMATCH')
        packet = matches[0]
        proposal = {k: result[k] for k in ('abc', 'lyrics', 'invocation_id', 'guidance_sha256', 'notes')}
        proposal.update(variant=packet['payload']['variant'], generation={k: result[k] for k in ('context_id', 'model', 'context_policy', 'limitations')})
        proposal['generation'].update(input_assessment_id=packet['assessment_id'], input_sha256=result['request_sha256'])
        return self.music.candidate(project_id, experiment_id, proposal, actor=actor, key=key)
