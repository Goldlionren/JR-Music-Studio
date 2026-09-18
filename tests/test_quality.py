import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from jr_music import quality as q, quality_loop as loop, audio_analysis as aa, songcraft
from jr_music.store import Store, StoreError, sha
from jr_music.producer import ProducerService
from jr_music.render import RenderService

LYRICS='[verse]\n春风吹过这条漫长街道\n星光照见那座安静小桥\n灯火等着晚归的人回家\n月亮听着风里轻轻呼吸'
ABC='X:1\nT:Example\nM:4/4\nL:1/8\nQ:1/4=80\nK:C\nC2 D2 E2 F2|\n'
BRIEF=dict(lyrics=LYRICS,style='folk',checkpoint='yue2_3b_int8_convrot.safetensors',seed=1,max_duration=300)


def measured(start=0,step=4):
    lines=q.lyric_timeline.lyric_lines(LYRICS)
    for i,line in enumerate(lines):line.update(start=start+i*step,end=start+i*step+4,basis='manual')
    return lines


class MetricsTests(unittest.TestCase):
    def measure(self,asr=None,lines=None,goals=None):
        return q.metrics(LYRICS,asr or {},lines if lines is not None else measured(),goals or q.DEFAULTS,60)

    def test_continuous_dense_windows_are_not_rap_verdicts(self):
        report=self.measure()
        self.assertEqual(report['dense_seconds'],16)
        self.assertEqual(len(report['dense_runs']),1)
        self.assertNotIn('rap_detected',report)

    def test_gaps_break_continuous_runs(self):
        self.assertEqual(self.measure(lines=measured(step=6))['dense_runs'],[])

    def test_unknown_or_overlapping_times_cannot_clear_density(self):
        report=self.measure(lines=measured(step=2))
        self.assertFalse(report['valid_order']);self.assertIsNone(report['max_rate'])
        self.assertIsNone(self.measure(lines=[])['dense_seconds'])

    def test_missing_first_line_cannot_be_replaced_by_later_lyric(self):
        self.assertIsNone(self.measure(lines=measured(start=10)[1:])['first_lyric_seconds'])

    def test_no_energy_does_not_imply_a_safe_ending(self):
        self.assertIsNone(self.measure()['possible_cutoff'])
        self.assertFalse(self.measure(asr={'measurements':{'energy':[0]*600,'sample_period':0.1}})['possible_cutoff'])

    def test_asr_low_confidence_does_not_establish_density(self):
        lines=measured()
        for row in lines:row['basis']='asr_estimate'
        asr={'segments':[dict(start=0,end=16,no_speech_prob=0.9,words=[
            dict(text=row['text'],start=row['start'],end=row['end']) for row in lines])]}
        self.assertEqual(self.measure(asr=asr,lines=lines)['density_lines'],[])

    def test_thresholds_reject_nan_and_reversed_ranges(self):
        for change in ({'intro_min':20,'intro_max':10},{'max_han_per_second':float('nan')},{'dense_run_seconds':False}):
            with self.assertRaisesRegex(StoreError,'INVALID_QUALITY_POLICY'):q.validate_policy(dict(q.DEFAULTS,**change))

    def test_changed_lyrics_or_missing_evidence_are_not_claimed_as_improvement(self):
        before=dict(policy=dict(values=q.DEFAULTS,generation=0),lyrics_sha256='a',measurements=self.measure())
        after=copy.deepcopy(before);after['lyrics_sha256']='b';after['measurements']['text_coverage']=0.99
        before['measurements']['text_coverage']=0.7
        rows=q.compare(before,after)['rows']
        self.assertFalse(next(r for r in rows if r['metric']=='text_coverage')['comparable'])
        self.assertEqual(q.compare(before,after)['verdict'],'producer_review_required')

    def test_theory_sources_are_pinned_not_unattributed_thresholds(self):
        sources=q.sources(['density','intro'])
        self.assertTrue(sources)
        self.assertTrue(all(len(s['sha256'])==64 and len(s['commit'])==40 and s['commit'] in s['url'] for s in sources))


class Bridge:
    bindings={'yinyue':{},'xiaowu':{}}
    def __init__(self,store):self.renders=RenderService(store);self.alias='fixture'
    def selection(self,actor,mcp_alias=None):return dict(mcp_alias=mcp_alias or self.alias)


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'db.sqlite3')
        self.pid=self.store.create_project('Fixture',actor='producer',key='project')['project_id']
        self.revision=self.store.add_revision(self.pid,ABC,BRIEF,'fixture',actor='yinyue',key='revision')
        self.bridge=Bridge(self.store);self.producer=ProducerService(self.store,self.bridge)
        self.report=dict(report_sha256='a'*64,revision_id=self.revision['revision_id'],snapshot_sha256=self.revision['snapshot_sha256'],
            policy=dict(generation=0,values=dict(q.DEFAULTS)),lyrics_sha256='x',measurements=MetricsTests().measure(),
            checks=[dict(code='density',label='密度',evidence='窗口密集',instruction='精简冗余，留出乐句空间',sources=q.sources(['density']))])
        self.patches=[patch.object(q,'read',return_value=self.report),
            patch.object(songcraft,'build',return_value=dict(name='test',version='1',selection='professional'))]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def start(self,key='cycle',**overrides):
        args=dict(expected_report_sha256='a'*64,codes=['density'],assigned_to='yinyue',preserve=['style'],note='请精简')
        args.update(overrides)
        return loop.start(self.producer,self.pid,'render_source',actor='producer',key=key,**args)

    def complete_command(self,cycle):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');command=self.producer._get(db,self.pid,cycle['command_id'])
            command.update(state='completed',render_id='render_result',result_revision_id='rev_result')
            self.producer._save(db,command,'yinyue');db.commit()

    def test_single_revision_is_idempotent_pinned_and_does_not_accept(self):
        first=self.start();self.bridge.alias='another'
        second=self.start()
        self.assertEqual(first['cycle_id'],second['cycle_id'])
        command=self.producer.get(self.pid,first['command_id'])
        self.assertEqual(command['production_binding']['mcp_alias'],'fixture')
        self.assertEqual(command['songcraft']['selection'],'professional')
        self.assertEqual(len(self.producer.list(self.pid)),1)
        self.assertIsNone(self.store.get_project(self.pid)['head_revision_id'])
        with self.assertRaisesRegex(StoreError,'QUALITY_CYCLE_ACTIVE'):self.start(key='another')

    def test_stale_report_and_conflicting_locks_are_rejected(self):
        with self.assertRaisesRegex(StoreError,'CHECK_REPORT_CHANGED'):self.start(expected_report_sha256='b'*64)
        with self.assertRaisesRegex(StoreError,'QUALITY_LOCK_CONFLICT'):self.start(preserve=['lyrics','rhythm'])
        self.assertEqual(self.producer.list(self.pid),[])

    def test_real_report_can_be_frozen_inside_cycle_transaction(self):
        self.patches[0].stop()
        asset=dict(asset_id='asset_fixture',media_type='audio/wav',sha256='c'*64)
        job=dict(state='succeeded',revision_id=self.revision['revision_id'],assets=[asset],verification=dict(duration_seconds=30))
        with patch.object(self.producer.renders,'get',return_value=job),patch.object(aa,'read',return_value=dict(status='not_analyzed')):
            report=q.read(self.producer,self.pid,'render_source')
            cycle=self.start(expected_report_sha256=report['report_sha256'])
            frozen=loop.blob(self.store,cycle['before_sha256'])
            self.assertEqual(frozen['audio_sha256'],asset['sha256'])
            self.assertEqual(frozen['report_sha256'],report['report_sha256'])
            self.assertEqual(cycle['state'],'revising')

    def test_audio_remeasurement_and_human_verdict_complete_the_loop(self):
        cycle=self.start();self.complete_command(cycle)
        after=copy.deepcopy(self.report);after['measurements']['dense_seconds']=0
        with patch.object(aa,'start',return_value=dict(state='completed')) as analysis,patch.object(q,'read',return_value=after):
            result=loop.advance(self.producer,self.pid,cycle['cycle_id'])
            loop.advance(self.producer,self.pid,cycle['cycle_id'])
            self.assertEqual(analysis.call_count,1)
            self.assertEqual(analysis.call_args.kwargs['mode'],'lyrics')
        self.assertEqual(result['state'],'awaiting_review')
        public=loop.list_for_render(self.producer,self.pid,'render_result')[0]
        self.assertEqual(public['comparison']['verdict'],'producer_review_required')
        loop.decide(self.producer,self.pid,cycle['cycle_id'],'improved','听了，有改善',actor='producer',key='decision')
        self.assertEqual(loop.get(self.producer,self.pid,cycle['cycle_id'])['state'],'closed')
        self.assertIsNone(self.store.get_project(self.pid)['head_revision_id'])

    def test_failed_creation_keeps_reservation_for_recovery(self):
        with patch.object(self.producer,'create',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):self.start()
        cycles=loop.records(self.store,self.pid)
        self.assertEqual(len(cycles),1)
        self.bridge.alias='other'
        result=self.start()
        self.assertEqual(result['mcp_alias'],'fixture')
        self.assertEqual(len(self.producer.list(self.pid)),1)

    def test_failure_does_not_generate_another_song(self):
        cycle=self.start()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE');command=self.producer._get(db,self.pid,cycle['command_id'])
            command.update(state='needs_attention',issue='TEST_FAILURE')
            self.producer._save(db,command,'yinyue');db.commit()
        loop.resume_pending(self.producer,self.pid);loop.resume_pending(self.producer,self.pid)
        self.assertEqual(loop.get(self.producer,self.pid,cycle['cycle_id'])['state'],'needs_attention')
        self.assertEqual(len(self.producer.list(self.pid)),1)
        loop.decide(self.producer,self.pid,cycle['cycle_id'],'abandoned','保留证据',actor='producer',key='abandon')

    def test_agent_cannot_record_producer_verdict(self):
        cycle=self.start()
        with self.assertRaisesRegex(StoreError,'PRODUCER_REQUIRED'):
            loop.decide(self.producer,self.pid,cycle['cycle_id'],'improved','fake',actor='yinyue',key='fake')

    def test_local_analysis_completes_without_comfy_or_transcription(self):
        root=Path(self.temp.name);model=root/'data/models/faster-whisper-large-v3-turbo'
        model.mkdir(parents=True);(model/'model.bin').write_bytes(b'fixture')
        audio=b'audio';asset=dict(asset_id='asset_fixture',media_type='audio/wav',sha256=sha(audio))
        job=dict(state='succeeded',revision_id=self.revision['revision_id'],snapshot_sha256=self.revision['snapshot_sha256'],
                 assets=[asset],verification=dict(duration_seconds=30))
        with patch.object(aa,'ROOT',root),patch.object(self.producer.renders,'get',return_value=job),\
             patch.object(self.store,'open_asset',return_value=(asset,io.BytesIO(audio))),\
             patch.object(aa.POOL,'submit') as submit,patch.object(aa,'fetch') as comfy:
            pending_full=dict(kind='analyze',source_render_id='render_fixture',state='working',analysis_mode='full')
            commands=self.producer._commands
            with patch.object(self.producer,'_commands',side_effect=lambda db,pid:dict(commands(db,pid),full=pending_full)):
                c=aa.start(self.producer,self.pid,'render_fixture','yinyue',actor='producer',key='analysis',mode='lyrics')
            try:
                replay=aa.start(self.producer,self.pid,'render_fixture','yinyue',actor='producer',key='analysis',mode='lyrics')
                self.assertEqual(c['command_id'],replay['command_id']);self.assertEqual(submit.call_count,1)
                self.assertEqual(c['state'],'analyzing')
                def process(*args,**kwargs):
                    aa.write(aa.folder(c['command_id'])/'asr.json',dict(source_audio_sha256=asset['sha256'],segments=[],measurements=dict(duration=30)))
                    return SimpleNamespace(returncode=0)
                with patch.object(aa.subprocess,'run',side_effect=process):aa.run(self.producer,self.pid,c['command_id'])
                saved=self.producer.get(self.pid,c['command_id'])
                self.assertEqual(saved['state'],'completed');self.assertIn('asr_sha256',saved)
                self.assertNotIn('transcription_sha256',saved);comfy.assert_not_called()
                with self.store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    pending=dict(saved,command_id='command_pending',state='queued',analysis_mode='full')
                    pending.pop('asr_sha256');self.producer._save(db,pending,'producer');db.commit()
                report=aa.read(self.producer,self.pid,'render_fixture')
                self.assertEqual(report['status'],'queued')
                self.assertEqual(report['asr']['source_audio_sha256'],asset['sha256'])
                self.assertEqual(report['asr_command_id'],saved['command_id'])
            finally:aa.ACTIVE.discard(c['command_id'])

    def test_quality_api_does_not_give_agents_producer_authority(self):
        import threading
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        from jr_music.server import make_server
        token='fixture-not-a-secret-token-123456789'
        server=make_server(self.store,[dict(actor_id='yinyue',role='agent',token=token)],port=0,hermes=self.bridge)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            url=f'http://127.0.0.1:{server.server_port}/projects/{self.pid}/renders/render_source/'
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
            with urlopen(Request(url+'quality',headers=headers),timeout=5) as response:
                self.assertEqual(json.load(response)['data']['report']['report_sha256'],'a'*64)
            with self.assertRaises(HTTPError) as raised:
                urlopen(Request(url+'quality-revise',data=b'{}',headers=headers),timeout=5)
            self.assertEqual(raised.exception.code,403)
        finally:server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main()
