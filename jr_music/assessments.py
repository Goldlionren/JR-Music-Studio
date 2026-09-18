"""Structured feedback and external semantic review evidence, never HEAD decisions."""
import secrets
from .music import MusicService, contract_version, variants
from .music_contracts import validate
from .store import canonical,sha,require,new_id,now

DIMENSIONS='linguistic_elegance subtlety restraint cliche_density emotional_explicitness image_economy metaphor_economy narrative_economy original_phrasing over_explanation sentimental_overstatement colloquial_naturalness line_quality hook_wording'.split()


class AssessmentService:
    def __init__(self,store):self.store=store;self.music=MusicService(store)

    def append(self,db,project_id,experiment_id,kind,payload,actor):
        record=dict(schema_version='music-assessment/1',assessment_id=new_id('assessment'),experiment_id=experiment_id,
            kind=kind,payload=payload,created_at=now(),created_by=actor)
        digest=self.store._blob(db,canonical(record))
        db.execute('INSERT INTO music_assessments VALUES(?,?,?,?)',(record['assessment_id'],experiment_id,kind,digest))
        self.store._event(db,project_id,'music_assessment_recorded',actor,dict(experiment_id=experiment_id,assessment_id=record['assessment_id'],kind=kind,sha256=digest))
        return record

    def list(self,project_id,experiment_id):
        with self.store.connect() as db:
            self.music._experiment(db,project_id,experiment_id)
            return [self.music._blob(db,r[0]) for r in db.execute('SELECT blob_hash FROM music_assessments WHERE experiment_id=? ORDER BY rowid',(experiment_id,))]

    def feedback(self,project_id,experiment_id,feedback,*,actor,key):
        def action(db):
            experiment = self.music._experiment(db,project_id,experiment_id)
            validate('music-assessment','producer-feedback',feedback, contract_version(experiment))
            require((feedback['scope']=='lyrics') or feedback['evaluation_mode']=='open_label_listening','AUDIO_FEEDBACK_REQUIRES_LISTENING_SCOPE')
            if feedback['evaluation_mode']=='lyrics_only':
                require(all(v is None for v in feedback['render_ids'].values()),'LYRIC_FEEDBACK_HAS_NO_AUDIO_REFERENCE')
            if feedback['evaluation_mode']=='open_label_listening':
                candidates=db.execute('SELECT variant,revision_id FROM music_candidates WHERE experiment_id=?',(experiment_id,)).fetchall()
                require(len(candidates)==len(variants(experiment)),'EXPERIMENT_INCOMPLETE')
                for c in candidates:
                    import json
                    jobs=[json.loads(r[0]) for r in db.execute('SELECT document FROM render_jobs WHERE revision_id=? AND id=?',(c['revision_id'],feedback['render_ids'][c['variant']]))]
                    require(any(j['state']=='succeeded' for j in jobs),'LISTENING_AUDIO_NOT_READY')
            preference={'no_preference':'undecided','both_unsuitable':'neither'}.get(feedback['preference'],feedback['preference'])
            legacy=self.music.append_feedback(db,project_id,experiment_id,
                dict(preference=preference,reason='['+feedback['scope']+'; '+feedback['evaluation_mode']+'] '+feedback['free_text']),actor)
            return self.append(db,project_id,experiment_id,'producer_feedback',dict(feedback=feedback,legacy_feedback_id=legacy['feedback_id']),actor)
        return self.store._operation(actor,key,dict(op='assessment_feedback',project_id=project_id,experiment_id=experiment_id,feedback=feedback),action)

    def semantic_request(self,project_id,experiment_id,*,actor,key):
        def action(db):
            experiment=self.music._experiment(db,project_id,experiment_id)
            version = contract_version(experiment)
            rows=db.execute('SELECT variant,revision_id FROM music_candidates WHERE experiment_id=? ORDER BY variant',(experiment_id,)).fetchall()
            require(len(rows)==len(variants(experiment)),'EXPERIMENT_INCOMPLETE')
            order=list(rows);secrets.SystemRandom().shuffle(order)
            candidates=[];binding={}
            for label,row in zip(('X','Y','Z'),order):
                rev=self.store._revision(db,project_id,row['revision_id']);snapshot=self.store._snapshot(db,rev)
                lines=snapshot['brief']['lyrics'].splitlines()
                candidates.append(dict(label=label,lines=[dict(line_number=i+1,text=line) for i,line in enumerate(lines)]))
                binding[label]=dict(variant=row['variant'],revision_id=rev['revision_id'],snapshot_sha256=rev['snapshot_sha256'],lyrics_sha256=sha(snapshot['brief']['lyrics'].encode()))
            request=dict(schema_version='semantic-lyric/' + str(version),brief=experiment['spec']['brief'],candidates=candidates,dimensions=DIMENSIONS,
                task='Independently assess the supplied original lyrics only. Quote exact numbered lines. Do not browse, use memory, inspect files or obtain prior opinions. Do not infer author or generation method. Do not assess audio.',
                output_contract='semantic-lyric/' + str(version) + '; 14 findings per candidate; finding/evidence/confidence/limitations; no overall score; audio_evaluated=false')
            return self.append(db,project_id,experiment_id,'semantic_request',dict(request=request,request_sha256=sha(canonical(request)),binding=binding,
                executor='yinyue_hermes_fresh_conversation',context_limitations='Fresh conversation requested; Hermes global memory/system context is not technically attested by JR.'),actor)
        return self.store._operation(actor,key,dict(op='semantic_request',project_id=project_id,experiment_id=experiment_id),action)

    def semantic_result(self,project_id,experiment_id,request_id,result,*,actor,key):
        def action(db):
            experiment = self.music._experiment(db,project_id,experiment_id)
            validate('semantic-lyric','result',result, contract_version(experiment))
            row=db.execute("SELECT blob_hash FROM music_assessments WHERE id=? AND experiment_id=? AND kind='semantic_request'",(request_id,experiment_id)).fetchone()
            require(row is not None,'SEMANTIC_REQUEST_NOT_FOUND')
            stored=self.music._blob(db,row[0])['payload']
            require(stored['request_sha256']==result['request_sha256'],'SEMANTIC_INPUT_MISMATCH')
            require(sorted(c['label'] for c in result['candidates'])==sorted(stored['binding']),'DUPLICATE_SEMANTIC_LABEL')
            for c in result['candidates']:
                require(sorted(f['dimension'] for f in c['findings'])==sorted(DIMENSIONS),'SEMANTIC_DIMENSIONS_MISSING')
                inputs=next(x for x in stored['request']['candidates'] if x['label']==c['label'])
                lines={x['line_number']:x['text'] for x in inputs['lines']}
                for f in c['findings']:
                    for e in f['evidence']:require(e['line_number'] in lines and e['quote'] in lines[e['line_number']],'SEMANTIC_QUOTE_MISMATCH')
            return self.append(db,project_id,experiment_id,'semantic_result',dict(request_id=request_id,result=result,
                provenance='External Hermes response supplied by operator; identity and context declarations are not cryptographically attested.'),actor)
        return self.store._operation(actor,key,dict(op='semantic_result',project_id=project_id,experiment_id=experiment_id,request_id=request_id,result=result),action)
