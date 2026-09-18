"""Portable checks for text-first generation and the immutable score handoff."""
import io
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import uuid
import threading
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError
from unittest.mock import patch
from jr_music.store import Store,StoreError,canonical,sha
from jr_music.producer import ProducerService
from jr_music.creation import CreationService
from jr_music.render import RenderService
from jr_music import direct_generation as direct,render_template as tpl,songcraft
from jr_music.creative_format import check
from jr_music.console import make_console

ABC='X:1\nT:Example\nM:4/4\nL:1/8\nQ:1/4=80\nK:C\nV:Vocal\nC2 D2 E2 G2|\n'
PLAN=dict(reply='测试方案',plan=dict(title='灯',concept='归家',style='folk',structure='前奏、主歌、尾奏',lyric_direction='简洁留白'))
PROVENANCE=dict(execution_id='test_context',context_policy='fresh_hermes_cli')

class Bridge:
    bindings={'yinyue':{},'xiaowu':{}}
    def __init__(self,store):self.renders=RenderService(store);self.alias='fixture'
    def selection(self,actor,mcp_alias=None):return dict(mcp_alias=mcp_alias or self.alias)
    def prepare(self,pid,rid,*,actor,key,binding=None):return self.renders.prepare(pid,rid,actor=actor,key=key)

class DirectTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'test.db');self.bridge=Bridge(self.store)
        self.producer=ProducerService(self.store,self.bridge)
        self.pid=self.store.create_project('Fixture',actor='producer',key='p')['project_id']
    def start(self,key='direct',**kw):
        args=dict(style='gentle folk\n',lyrics='[Verse]\n灯还亮着\n',assigned_to='yinyue',target_duration=30,seed=0,abc_planning=True,actor='producer')
        args.update(kw)
        return direct.start(self.producer,self.pid,key=key,**args)
    def render(self,c):
        self.producer.claim(self.pid,c['command_id'],actor='yinyue')
        c=self.producer.propose(self.pid,c['command_id'],[],'直接生成',dict(execution_id='test_render',context_policy='worker_render_only'),actor='yinyue')
        return self.bridge.renders.get(self.pid,c['render_id'])
    def test_direct_exact_input_no_model_and_frozen_routing(self):
        c=self.start();self.bridge.alias='changed';self.assertEqual(c,self.start())
        source=self.store.get_revision(self.pid,c['source_revision_id'])
        self.assertEqual(source['snapshot']['abc'],'');self.assertEqual(source['snapshot']['brief']['lyrics'],'[Verse]\n灯还亮着\n')
        self.assertEqual(c['production_binding']['mcp_alias'],'fixture');self.assertEqual(c['kind'],'render')
        self.assertIsNone(self.producer.packet(self.pid,c['command_id'],actor='yinyue')['score'])
        value=direct.enrich(self.store,source);self.assertFalse(value['score_export']['available']);self.assertEqual(value['score']['status'],'absent')
        job=self.render(c);self.assertEqual(job['template_id'],tpl.TEXT_TEMPLATE_ID)
        self.assertIsNone(self.store.get_project(self.pid)['head_revision_id'])
        with self.assertRaisesRegex(StoreError,'IDEMPOTENCY_CONFLICT'):self.start(style='other')
    def test_validation_does_not_create_half_tasks(self):
        for args in ({'actor':'xiaowu'},{'style':''},{'lyrics':' '},{'seed':True},{'seed':2**53},{'abc_planning':'true'},{'target_duration':301}):
            with self.assertRaises(StoreError):self.start(**args)
        self.assertEqual(self.store.list_revisions(self.pid),[])
        with self.assertRaises(StoreError):self.store.add_revision(self.pid,'',{},'x',actor='producer',key='bad')
    def test_planner_toggle_and_template_mutation_guard(self):
        for enabled in (False,True):
            c=self.start(str(enabled),abc_planning=enabled);job=self.render(c)
            graph=tpl.template(job['template_id'])
            for key,(node,field) in tpl.SLOTS.items():graph[node]['inputs'][field]=job['parameters'][key]
            tpl.validate(graph,job['parameters'],job['template_sha256'],job['template_id'])
            self.assertEqual('168' in graph,enabled)
            if enabled:
                self.assertEqual(graph['155']['inputs']['source'],['168',0])
                graph['168']['inputs']['temperature']=1
                with self.assertRaisesRegex(StoreError,'WORKFLOW_TEMPLATE_MISMATCH'):tpl.validate(graph,job['parameters'],job['template_sha256'],job['template_id'])
    def test_generated_score_copy_has_provenance_and_never_rebinds_audio(self):
        c=self.start();job=self.render(c)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            with self.store.cas.stage(io.BytesIO(ABC.encode())) as staged:
                asset=self.store.file_asset(db,self.pid,job['revision_id'],staged,'generated-score.abc','text/plain','server_generated')
            job.update(state='succeeded',assets=[asset]);db.execute('UPDATE render_jobs SET document=? WHERE id=?',(canonical(job),job['render_id']));db.commit()
        one=direct.use_score(self.producer,self.pid,job['render_id'],actor='producer',key='a')
        two=direct.use_score(self.producer,self.pid,job['render_id'],actor='producer',key='b')
        self.assertEqual(one,two);self.assertEqual(one['parent_revision_id'],job['revision_id'])
        self.assertEqual(self.store.get_revision(self.pid,one['revision_id'])['snapshot']['abc'],ABC)
        self.assertEqual(self.bridge.renders.get(self.pid,job['render_id'])['revision_id'],c['source_revision_id'])
        self.assertEqual(len([e for e in self.store.events(self.pid) if e['type']=='generated_score_adopted']),1)
    def test_history_archives_generated_abc_and_still_checks_graph(self):
        c=self.start();job=self.render(c);rid=job['render_id'];rs=self.bridge.renders
        graph=tpl.template(job['template_id'])
        for key,(node,field) in tpl.SLOTS.items():graph[node]['inputs'][field]=job['parameters'][key]
        rs.bind_workflow(self.pid,rid,graph,dict(valid=True,server_id=job['server']['server_id'],workflow_sha256=sha(canonical(graph))),actor='executor')
        rs.claim(self.pid,rid,actor='executor');prompt=str(uuid.uuid4())
        rs.receipt(self.pid,rid,job['server']['server_id'],prompt,actor='executor')
        folder,base=job['parameters']['filename_prefix'].rsplit('/',1)
        history=dict(prompt=[0,prompt,graph],status=dict(completed=True,status_str='success'),outputs={'155':{'text':[ABC]},'152':{'audio':[dict(filename=base+'_00001.flac',subfolder=folder,type='output')]}})
        bad=deepcopy(history);bad['prompt'][2]['168']['inputs']['top_k']=10
        with self.assertRaisesRegex(StoreError,'REMOTE_WORKFLOW_MISMATCH'):rs.observe(self.pid,rid,job['server']['server_id'],history=bad,actor='executor')
        rs.observe(self.pid,rid,job['server']['server_id'],history=history,actor='executor')
        def verify(src,dst,duration):dst.write_bytes(b'fixture-wav');return dict(duration_seconds=30)
        with patch.object(rs.verifier,'verify',side_effect=verify):finished=rs.collect(self.pid,rid,io.BytesIO(b'fixture-flac'),actor='executor')
        asset=next(a for a in finished['assets'] if a['name']=='generated-score.abc')
        _,stream=self.store.open_asset(self.pid,asset['asset_id'])
        with stream:self.assertEqual(stream.read().decode(),ABC)
        self.assertEqual(self.store.get_revision(self.pid,job['revision_id'])['snapshot']['abc'],'')
    def test_agent_mode_freezes_skills_and_uses_three_field_contract(self):
        creation=CreationService(self.producer)
        bundle=dict(name='fixture',version='1',selection='professional')
        with patch.object(songcraft,'build',return_value=bundle):
            p=creation.discuss(self.pid,'yinyue','留白的短歌',30,None,actor='producer',key='idea',songcraft_selection='professional',creation_mode='style_lyrics')
        self.producer.claim(self.pid,p['command_id'],actor='yinyue')
        p=creation.propose(self.pid,p['command_id'],PLAN,PROVENANCE,actor='yinyue')
        c=creation.confirm(self.pid,p['command_id'],p['plan_sha256'],actor='producer',key='confirm')
        self.assertEqual(c['songcraft'],p['songcraft']);self.assertEqual(c['creation_mode'],'style_lyrics')
        self.producer.claim(self.pid,c['command_id'],actor='yinyue')
        result=dict(style='piano folk, instrumental intro',lyrics='[Intro]\n[Verse]\n灯还亮着',summary='留白')
        checked,_=check(json.dumps(result),c,lyric_contract=True)
        c=creation.propose(self.pid,c['command_id'],checked,PROVENANCE,actor='yinyue')
        self.assertEqual(c['state'],'rendering')
        job=self.bridge.renders.get(self.pid,c['render_id']);self.assertEqual(job['template_id'],tpl.TEXT_TEMPLATE_ID)
        self.assertEqual(self.store.get_revision(self.pid,c['result_revision_id'])['snapshot']['brief']['style'],result['style'])
        with self.assertRaises(ValueError):check(json.dumps(result),dict(c,creation_mode='score'))
    def test_console_requires_csrf_and_reads_direct_candidate_without_parse_failure(self):
        server=make_console({'studio':('Fixture',self.store,self.bridge)},port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}';opener=build_opener(ProxyHandler({}))
        try:
            with opener.open(url+'/api/session') as response:
                cookie=response.headers['Set-Cookie'].split(';')[0];csrf=json.load(response)['data']['csrf']
            path=url+'/api/studio/projects/'+self.pid
            headers={'Cookie':cookie,'Origin':url,'Content-Type':'application/json'}
            body=dict(style='gentle folk',lyrics='[Verse]\n灯还亮着',assigned_to='yinyue',target_duration=30,seed=0,abc_planning=True,idempotency_key='http')
            with self.assertRaises(HTTPError) as error:opener.open(Request(path+'/direct-generate',data=canonical(body),headers=headers))
            self.assertEqual(error.exception.code,403)
            headers['X-JR-CSRF']=csrf
            with opener.open(Request(path+'/direct-generate',data=canonical(body),headers=headers)) as response:c=json.load(response)['data']
            with opener.open(Request(path,headers=headers)) as response:value=json.load(response)['data']
            self.assertEqual(value['revisions'][0]['revision']['revision_id'],c['source_revision_id'])
            self.assertEqual(value['revisions'][0]['score']['status'],'absent')
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
