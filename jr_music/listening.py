"""Frozen listening selections, including explicitly recorded technical derivatives.

Uses existing assessments and feedback tables. Never replaces Composer candidates.
"""
import json
from .assessments import AssessmentService
from .music import MusicService, contract_version, variants
from .music_contracts import validate
from .store import canonical, sha, require, identifier


class ListeningService:
    def __init__(self, store):
        self.store=store;self.music=MusicService(store);self.assessments=AssessmentService(store)

    def create(self,project_id,experiment_id,render_ids,correction_ids,*,actor,key):
        def action(db):
            exp=self.music._experiment(db,project_id,experiment_id)
            require(contract_version(exp)==2,'THREE_ARM_EXPERIMENT_REQUIRED')
            arms=variants(exp)
            require(isinstance(render_ids,dict) and isinstance(correction_ids,dict) and set(render_ids)==set(correction_ids)==set(arms),'INVALID_LISTENING_SELECTION')
            members={};environments=set()
            for variant in arms:
                row=db.execute('SELECT revision_id FROM music_candidates WHERE experiment_id=? AND variant=?',(experiment_id,variant)).fetchone()
                require(row is not None,'EXPERIMENT_INCOMPLETE')
                original=self.store._revision(db,project_id,row[0]);selected=original
                correction_id=correction_ids[variant]
                if correction_id is not None:
                    identifier(correction_id)
                    row=db.execute("SELECT blob_hash FROM music_assessments WHERE id=? AND experiment_id=? AND kind='technical_correction'",(correction_id,experiment_id)).fetchone()
                    require(row is not None,'TECHNICAL_CORRECTION_NOT_FOUND')
                    correction=self.music._blob(db,row[0])['payload']
                    require(correction['source_revision']==original,'CORRECTION_SOURCE_MISMATCH')
                    selected=self.store._revision(db,project_id,correction['result_revision']['revision_id'])
                    require(selected==correction['result_revision'] and selected['parent_revision_id']==original['revision_id'],'CORRECTION_PARENT_MISMATCH')
                    before=self.store._snapshot(db,original);after=self.store._snapshot(db,selected)
                    proof=correction['verification'];offset=proof['byte_offset']
                    require(proof['verifier']=='one-byte-technical-repair/1' and type(offset) is int and offset>=0,'INVALID_TECHNICAL_CORRECTION')
                    b,a=before['abc'].encode(),after['abc'].encode()
                    require(sha(b)==proof['source_abc_sha256'] and sha(a)==proof['result_abc_sha256'] and
                        b[offset:offset+1]==b'2' and a==b[:offset]+b[offset+1:] and before['brief']==after['brief'],'CORRECTION_BYTES_MISMATCH')
                    from .score import parse_abc
                    require(parse_abc(a)['status']=='supported','CORRECTION_SCORE_INVALID')
                rid=identifier(render_ids[variant])
                row=db.execute('SELECT document FROM render_jobs WHERE id=? AND project_id=? AND revision_id=?',(rid,project_id,selected['revision_id'])).fetchone()
                require(row is not None,'LISTENING_RENDER_REVISION_MISMATCH')
                job=json.loads(row[0]);require(job['state']=='succeeded' and job['schema_version']=='render/2','LISTENING_AUDIO_NOT_READY')
                environments.add(job['environment_sha256'])
                members[variant]=dict(source_candidate_revision_id=original['revision_id'],listened_revision_id=selected['revision_id'],
                    render_id=rid,technical_correction_id=correction_id,lyrics_sha256=job['lyrics_sha256'],snapshot_sha256=job['snapshot_sha256'])
            require(len(environments)==1,'LISTENING_ENVIRONMENT_MISMATCH')
            return self.assessments.append(db,project_id,experiment_id,'listening_session',dict(schema_version='listening-session/1',
                mode='open_label',members=members,environment_sha256=next(iter(environments)),head_changed=False,
                limitations=['A derivative is labeled explicitly; first-draft technical failure remains part of the experiment.',
                    'Render success and byte preservation do not establish audio quality or Master effectiveness.']),actor)
        return self.store._operation(actor,key,dict(op='listening_session',project_id=project_id,experiment_id=experiment_id,render_ids=render_ids,correction_ids=correction_ids),action)

    def feedback(self,project_id,experiment_id,session_id,feedback,*,actor,key):
        validate('music-assessment','producer-feedback',feedback,2)
        require(feedback['evaluation_mode']=='open_label_listening','LISTENING_FEEDBACK_REQUIRED')
        def action(db):
            self.music._experiment(db,project_id,experiment_id)
            row=db.execute("SELECT blob_hash FROM music_assessments WHERE id=? AND experiment_id=? AND kind='listening_session'",(identifier(session_id),experiment_id)).fetchone()
            require(row is not None,'LISTENING_SESSION_NOT_FOUND')
            session=self.music._blob(db,row[0])['payload']
            require(feedback['render_ids']=={k:v['render_id'] for k,v in session['members'].items()},'LISTENING_SESSION_MISMATCH')
            preference={'no_preference':'undecided','both_unsuitable':'neither'}.get(feedback['preference'],feedback['preference'])
            legacy=self.music.append_feedback(db,project_id,experiment_id,dict(preference=preference,
                reason='['+feedback['scope']+'; open_label_listening; '+session_id+'; explicit technical derivatives in session] '+feedback['free_text']),actor)
            return self.assessments.append(db,project_id,experiment_id,'producer_feedback',dict(feedback=feedback,
                listening_session_id=session_id,legacy_feedback_id=legacy['feedback_id']),actor)
        return self.store._operation(actor,key,dict(op='listening_feedback',project_id=project_id,experiment_id=experiment_id,session_id=session_id,feedback=feedback),action)
