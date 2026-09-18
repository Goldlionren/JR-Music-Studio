import json
from pathlib import Path
import tempfile
import unittest
from jr_music.store import Store,StoreError,canonical
from jr_music.producer import ProducerService
from jr_music.creation import CreationService
from jr_music import draft_repair as repair

ABC='X:1\nT:Example\nM:4/4\nL:1/8\nQ:1/4=80\nK:C\nV:Vocal\n[V: Vocal]\n["C"C2 D2 E2 G2|\n'

class DraftRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'db.sqlite3');self.p=ProducerService(self.store)
        self.pid=self.store.create_project('Example',actor='producer',key='project')['project_id']
        c=CreationService(self.p)._command(self.pid,'xiaowu','compose','confirmed draft',300)
        c.update(state='needs_attention',issue='SCORE_REQUIRES_NOTATION_REPAIR',
            plan={'structure':'one bar','style':'gentle folk'},plan_sha256='a'*64,seed=42,
            production_binding={'mcp_alias':'comfy_fixture'},songcraft={'selection':'none'})
        with self.store.connect() as db:self.p._save(db,c,'xiaowu');db.commit()
        self.c=c;self.result=dict(abc=ABC,lyrics='[verse]\n保持原歌词',summary='Original',lyric_map=[])
        self.save_result(c)

    def save_result(self,c):
        with self.store.connect() as db:
            payload=dict(command_id=c['command_id'],project_id=self.pid,result=self.result)
            digest=self.store._blob(db,canonical(payload))
            self.store._event(db,self.pid,'creation_result_received','xiaowu',dict(command_id=c['command_id'],result_sha256=digest));db.commit()
        return digest

    def start(self,c=None,key='repair'):
        return repair.start(self.p,self.pid,(c or self.c)['command_id'],actor='producer',key=key)

    def test_freezes_original_diagnostics_and_target_with_idempotency(self):
        child=self.start();again=self.start(key='double-click')
        self.assertEqual(child['command_id'],again['command_id'])
        self.assertEqual(child['production_binding'],self.c['production_binding'])
        self.assertEqual(child['seed'],42);self.assertEqual(child['plan'],self.c['plan'])
        packet=repair.context(self.store,child)
        self.assertEqual(packet['original_result'],self.result)
        self.assertTrue(any(d.get('line') for d in packet['diagnostics']['issues']))
        self.assertEqual(self.p.get(self.pid,self.c['command_id'])['state'],'needs_attention')
        self.assertIsNone(self.store.get_project(self.pid)['head_revision_id'])

    def test_bounded_failure_resumes_once_after_restart_then_stops(self):
        first=self.start()
        for attempt in (1,2):
            self.assertEqual(first['draft_repair']['round'],attempt)
            self.save_result(first)
            with self.store.connect() as db:
                first.update(state='needs_attention',issue='SPEC_SCORE_MISMATCH')
                self.p._save(db,first,'xiaowu');db.commit()
            repair.resume(self.p);repair.resume(self.p)
            children=[c for c in self.p.list(self.pid) if c.get('retry_of')==first['command_id']]
            if attempt==1:self.assertEqual(len(children),1);first=children[0]
            else:self.assertEqual(children,[])
        self.assertEqual(len(self.p.list(self.pid)),3)

    def test_changed_lyrics_rejected_before_render(self):
        child=self.start()
        repair.verify_lyrics(self.store,child,self.result)
        with self.assertRaisesRegex(StoreError,'REPAIR_LYRICS_CHANGED'):
            repair.verify_lyrics(self.store,child,dict(self.result,lyrics='changed'))
        self.p.claim(self.pid,child['command_id'],actor='xiaowu')
        with self.assertRaisesRegex(StoreError,'REPAIR_LYRICS_CHANGED'):
            CreationService(self.p).propose(self.pid,child['command_id'],dict(self.result,lyrics='changed'),
                {'execution_id':'changed_fixture','context_policy':'fresh_hermes_cli'},actor='xiaowu')
        evidence=repair.received(self.store,self.pid,child['command_id'])
        self.assertEqual(repair.read_blob(self.store,evidence)['result']['lyrics'],'changed')
        self.assertEqual(self.store.list_revisions(self.pid),[])

    def test_valid_repair_saves_new_revision_then_uses_original_render_route(self):
        from unittest.mock import Mock
        child=self.start()
        self.p.claim(self.pid,child['command_id'],actor='xiaowu')
        self.p.bridge=Mock()
        self.p.bridge.prepare.return_value={'render_id':'render_fixture'}
        fixed=dict(abc=ABC.replace('[V: Vocal]\n','').replace('["','"'),
                   lyrics=self.result['lyrics'],summary='Notation repaired')
        accepted=CreationService(self.p).propose(self.pid,child['command_id'],fixed,
            {'execution_id':'repair_fixture','context_policy':'fresh_hermes_cli'},actor='xiaowu')
        self.assertEqual(accepted['state'],'rendering')
        self.assertEqual(accepted['render_id'],'render_fixture')
        self.assertEqual(self.p.bridge.prepare.call_args.kwargs['binding'],self.c['production_binding'])
        revision=self.store.get_revision(self.pid,accepted['result_revision_id'])
        self.assertEqual(revision['snapshot']['brief']['lyrics'],self.result['lyrics'])
        self.assertEqual(repair.context(self.store,child)['original_result']['abc'],ABC)
        self.assertIsNone(self.store.get_project(self.pid)['head_revision_id'])

    def test_permission_missing_source_and_render_uncertainty_fail_closed(self):
        with self.assertRaisesRegex(StoreError,'PRODUCER_REQUIRED'):
            repair.start(self.p,self.pid,self.c['command_id'],actor='xiaowu',key='agent')
        with self.store.connect() as db:
            self.c.update(render_id='render_uncertain');self.p._save(db,self.c,'xiaowu');db.commit()
        with self.assertRaisesRegex(StoreError,'DRAFT_REPAIR_UNAVAILABLE'):self.start()

    def test_diagnostics_do_not_accept_or_rewrite_music(self):
        before=json.dumps(self.result)
        d=repair.diagnose(self.c,self.result)
        self.assertEqual(json.dumps(self.result),before)
        self.assertEqual(d['bar_counts'],{'Vocal':1})
        self.assertTrue(d['notation_preview_only'])

    def test_unparseable_draft_still_has_reviewable_diagnostics(self):
        self.assertEqual(repair.diagnose(self.c,dict(abc=''))['issues'][-1]['code'],'INVALID_ABC_BYTES')

    def test_invalid_lyric_map_becomes_diagnostic_not_request_failure(self):
        from unittest.mock import patch
        with patch('jr_music.score_lyrics.validate',side_effect=StoreError('SCORE_LYRIC_MAP_RANGE')):
            d=repair.diagnose(self.c,dict(self.result,lyric_map=[{'line':12}]))
        self.assertEqual(d['issues'][-1]['code'],'SCORE_LYRIC_MAP_RANGE')
        self.assertEqual(d['issues'][-1]['lyric_line'],12)

    def test_diagnostics_show_actual_section_length_and_note_bounds(self):
        result=dict(abc=ABC.split('[V:')[0]+'% intro\nz8|\n% instrumental\nC2 D2 E2 z2|C8|\n',
            lyrics='[verse]\n原词',lyric_map=[{'line':1,'voice':'Vocal','units':[['原词',2,1,2,4]]}],
            production_specs={'arr_spec':{'form':[{'bars':1},{'bars':1}]}})
        d=repair.diagnose(self.c,result)
        self.assertEqual(d['section_inventory'][-1]['bars'],2)
        self.assertTrue(any(i['code']=='SPEC_SECTION_LENGTH_MISMATCH' for i in d['issues']))
        problem=next(i for i in d['issues'] if i.get('lyric_line')==1)
        self.assertEqual(problem['unit_ranges'][0]['bar_note_counts'],{'2':3})

if __name__=='__main__':unittest.main()
