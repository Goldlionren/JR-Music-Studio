"""Portable regression checks using synthetic content and no deployment secrets."""
import json
import io
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen, Request, build_opener, ProxyHandler
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

from jr_music.store import Store, StoreError, canonical, sha
from jr_music.score import parse_abc
from jr_music.score_export import prepare, midi, musicxml
from jr_music import render_template, songcraft
from jr_music.console import make_console
from jr_music.hermes import load_bridge
from jr_music.render import RenderService

ABC = 'X:1\nT:Example\nM:4/4\nL:1/8\nQ:1/4=80\nK:C\nV:Vocal\nC2 D2 E2 G2|c2 B2 A2 G2|\n'
BRIEF = dict(style='gentle pop', lyrics='[verse]\nAn example', checkpoint='yue2_3b_int8_convrot.safetensors', seed=1, max_duration=300)
ROOT = Path(__file__).resolve().parents[1]


class PublicReleaseTests(unittest.TestCase):
    def test_archived_render_manifest_download_is_exact_and_project_scoped(self):
        revision=self.add();service=RenderService(self.store)
        job=service.prepare(self.pid,revision['revision_id'],actor='producer',key='manifest')
        # Synthetic archive: exercise download, not GPU execution.
        raw=canonical(dict(seed_decimal='18446744073709551615',render_id=job['render_id'],
            workflow={'165':{'class_type':'SeedNode','inputs':{'seed':18446744073709551615}}}))
        with self.store.cas.stage(io.BytesIO(raw)) as staged:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                asset=self.store.file_asset(db,self.pid,revision['revision_id'],staged,'render-manifest.json','application/json','server_generated')
                job.update(state='succeeded',assets=[asset])
                db.execute('UPDATE render_jobs SET document=? WHERE id=?',(canonical(job),job['render_id']));db.commit()
        other=self.store.create_project('Other',actor='producer',key='other')['project_id']
        before=len(self.store.events(self.pid))
        server=make_console({'studio':('Studio',self.store,None)},port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        opener=build_opener(ProxyHandler({}));base=f'http://127.0.0.1:{server.server_port}'
        path=f'/api/studio/projects/{self.pid}/renders/{job["render_id"]}/manifest'
        try:
            with self.assertRaises(HTTPError) as error:opener.open(base+path)
            self.assertEqual(error.exception.code,403)
            with opener.open(base+'/api/session') as response:cookie=response.headers['Set-Cookie'].split(';')[0]
            with opener.open(Request(base+path,headers={'Cookie':cookie})) as response:
                self.assertEqual(response.read(),raw)
                self.assertIn(job['render_id']+'-manifest.json',response.headers['Content-Disposition'])
                self.assertEqual(response.headers['Content-Type'],'application/json')
            with self.assertRaises(HTTPError):opener.open(Request(base+path.replace(self.pid,other),headers={'Cookie':cookie}))
            with self.store.connect() as db:
                job['assets']=[]
                db.execute('UPDATE render_jobs SET document=? WHERE id=?',(canonical(job),job['render_id']));db.commit()
            with self.assertRaises(HTTPError) as error:opener.open(Request(base+path,headers={'Cookie':cookie}))
            self.assertEqual(json.load(error.exception)['error']['code'],'RENDER_MANIFEST_UNAVAILABLE')
            self.assertEqual(len(self.store.events(self.pid)),before)
        finally:server.shutdown();server.server_close();thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'studio.sqlite3'
        self.store = Store(self.path)
        self.project = self.store.create_project('Example', actor='producer', key='project')
        self.pid = self.project['project_id']

    def add(self, key='revision'):
        return self.store.add_revision(self.pid, ABC, BRIEF, 'Synthetic fixture', actor='yinyue', key=key)

    def test_original_survives_restart_without_automatic_accept(self):
        revision = self.add()
        reopened = Store(self.path)
        self.assertEqual(reopened.get_revision(self.pid, revision['revision_id'])['snapshot']['abc'], ABC)
        self.assertIsNone(reopened.get_project(self.pid)['head_revision_id'])

    def test_idempotency_does_not_create_duplicate_candidate(self):
        self.assertEqual(self.add()['revision_id'], self.add()['revision_id'])
        with self.assertRaisesRegex(StoreError, 'IDEMPOTENCY_CONFLICT'):
            self.store.add_revision(self.pid, ABC+'\n', BRIEF, 'Changed', actor='yinyue', key='revision')

    def test_stale_producer_decision_cannot_replace_head(self):
        one, two = self.add('one'), self.add('two')
        self.store.decide(self.pid, one['revision_id'], 'accept', None, 0, 'Listen', actor='producer', key='accept')
        with self.assertRaisesRegex(StoreError, 'HEAD_CONFLICT'):
            self.store.decide(self.pid, two['revision_id'], 'accept', None, 0, 'Stale', actor='producer', key='stale')
        self.assertEqual(self.store.get_project(self.pid)['head_revision_id'], one['revision_id'])

    def test_midi_and_musicxml_have_notes_and_exact_bar_lengths(self):
        revision = self.add()
        source = self.store.get_revision(self.pid, revision['revision_id'])
        model = prepare(source, dict(rows=[], generation=0))
        output = midi(model)
        self.assertTrue(output.startswith(b'MThd'))
        self.assertGreaterEqual(struct.unpack('>H', output[10:12])[0], 2)
        root = ET.fromstring(musicxml(model))
        self.assertEqual(len(root.findall('.//measure')), 2)
        divisions = int(root.findtext('.//divisions'))
        for measure in root.findall('.//measure'):
            self.assertEqual(sum(int(note.findtext('duration')) for note in measure.findall('note')), divisions*4)

    def test_invalid_bars_are_not_exported_as_valid_music(self):
        source = dict(revision=dict(revision_id='rev_test', snapshot_sha256='a'*64),
                      snapshot=dict(abc=ABC.replace('C2 D2 E2 G2','C2 D2'), brief=BRIEF))
        with self.assertRaisesRegex(StoreError, 'EXPORT_SCORE_UNSUPPORTED'):
            prepare(source, dict(rows=[], generation=0))

    def test_render_resource_contains_only_bound_creative_parameters(self):
        graph = render_template.template()
        self.assertTrue(render_template.REFERENCE.is_file())
        for node, field in render_template.SLOTS.values():
            self.assertEqual(graph[node]['inputs'][field], '<bound-parameter>')
        self.assertEqual(sha(canonical(graph)), sha(canonical(render_template.template())))

    def test_private_styles_are_not_advertised_when_not_installed(self):
        with patch.object(songcraft, 'ROOT', Path(self.temp.name)/'not-installed'):
            self.assertEqual([row['id'] for row in songcraft.catalog()], ['professional','none'])

    def test_console_starts_without_hermes_or_private_assets(self):
        server = make_console({'studio': ('Studio', self.store, None)}, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen('http://127.0.0.1:'+str(server.server_port)+'/', timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read().lower().startswith(b'<!doctype html'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_rejected_post_returns_403_when_body_follows_headers(self):
        from http.client import HTTPConnection
        import time
        server=make_console({'studio':('Studio',self.store,None)},port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for _ in range(3):
                connection=HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                try:
                    body=canonical({'title':'Must not create','idempotency_key':'rejected'})
                    connection.putrequest('POST','/api/studio/projects')
                    connection.putheader('Origin',f'http://127.0.0.1:{server.server_port}')
                    connection.putheader('Content-Type','application/json')
                    connection.putheader('Content-Length',str(len(body)))
                    connection.endheaders()
                    time.sleep(0.02)  # Reproduce headers/body arriving separately.
                    connection.send(body)
                    response=connection.getresponse()
                    self.assertEqual(response.status,403)
                    self.assertEqual(json.loads(response.read())['error']['code'],'CSRF_DENIED')
                finally:connection.close()
            self.assertEqual(len(self.store.list_projects()),1)
        finally:server.shutdown();server.server_close();thread.join()

    def test_configuration_example_can_load_without_contacting_hosts(self):
        config = json.loads((ROOT/'config/hermes-deployment.example.json').read_text(encoding='utf-8'))
        workspace = str(Path(self.temp.name)/'renders')
        config['work_root'] = workspace
        for binding in config['bindings'].values():
            binding['workspace'] = workspace
            binding['mcp_alias'] = 'comfy_example'
        path = Path(self.temp.name)/'hermes-deployment.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        bridge = load_bridge(path, RenderService(self.store), 'ffmpeg', 'ffprobe')
        self.assertEqual(set(bridge.bindings), {'yinyue', 'xiaowu'})


if __name__ == '__main__':
    unittest.main()
