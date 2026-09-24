"""Verify the real worker's CLI response boundary without SSH or model calls."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from jr_music import client,creative_format

spec = importlib.util.spec_from_file_location('creation_worker_fixture',
    Path(__file__).resolve().parents[1]/'integrations/hermes/producer_worker.py')
worker = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'fcntl':types.ModuleType('fcntl'), 'yaml':types.ModuleType('yaml'), 'client':client,'creative_format':creative_format}):
    spec.loader.exec_module(worker)


class WorkerResponseTests(unittest.TestCase):
    def test_file_guardrail_is_retryable_without_format_recovery(self):
        from jr_music.command_status import can_retry,can_recover_format
        raw='I stopped retrying read_file because it hit the tool-call guardrail (same_tool_failure_halt) after 6 repeated non-progressing attempts.'
        (self.directory/'hermes.stdout.txt').write_text(raw,encoding='utf-8')
        (self.directory/'model-started.json').write_text('{}')
        with patch.object(worker.subprocess,'run') as invoke:
            with self.assertRaisesRegex(ValueError,'^HERMES_FILE_READ_HALTED$'):
                worker.create_original(self.packet,self.directory)
            invoke.assert_not_called()
        command=dict(kind='compose',state='needs_attention',issue='HERMES_FILE_READ_HALTED')
        self.assertTrue(can_retry(command));self.assertFalse(can_recover_format(command))
        self.assertEqual((self.directory/'hermes.stdout.txt').read_text(),raw)
        result={**self.result,'reply':raw}
        (self.directory/'hermes.stdout.txt').write_text(json.dumps(result))
        self.assertEqual(worker.read_creative_response(self.packet,self.directory),result)

    def test_skill_manifest_paths_resolve_for_both_creation_routes(self):
        bundle=dict(name='fixture',version='1',selection='professional',adapter='fixture adapter',
            files={'mc-workflow/SKILL.md':'composition','lw-workflow/SKILL.md':'lyrics','lw-mandarin/SKILL.md':'Chinese lyric craft'})
        frozen={k:bundle[k] for k in ('name','version','selection')}
        frozen['sha256']=creative_format.sha(creative_format.canonical(bundle))
        for mode in ('style_lyrics','score'):
            directory=self.directory/mode;directory.mkdir()
            prompt=worker.songcraft_prompt(dict(command=dict(creation_mode=mode,songcraft=frozen),songcraft_materials=bundle),directory)
            for name,content in bundle['files'].items():
                path=directory.resolve()/'frozen-skills'/name
                self.assertIn(str(path),prompt)
                self.assertEqual(path.read_text(encoding='utf-8'),content)
            self.assertIn('Read one relevant file',prompt)
            self.assertNotIn('Use exact relative paths',prompt)

    def test_compaction_runtime_status_does_not_corrupt_final_json(self):
        (self.directory/'hermes.stdout.txt').write_text('  ⟳ compacting context…\n'+json.dumps(self.result),encoding='utf-8')
        self.assertEqual(worker.read_creative_response(self.packet,self.directory),self.result)

    def test_provider_timeout_is_not_reported_as_invalid_music_json(self):
        (self.directory/'hermes.stdout.txt').write_text('API call failed after 3 retries: Non-streaming API call timed out after 90s')
        with self.assertRaisesRegex(ValueError,'^HERMES_PROVIDER_TIMEOUT$'):
            worker.read_creative_response(self.packet,self.directory)

    def test_creative_wait_budget_is_process_local_and_respects_override(self):
        with patch.dict(worker.os.environ,{},clear=True):
            self.assertEqual(worker.creative_environment()['HERMES_API_CALL_STALE_TIMEOUT'],'360')
            self.assertNotIn('HERMES_API_CALL_STALE_TIMEOUT',worker.os.environ)
        with patch.dict(worker.os.environ,{'HERMES_API_CALL_STALE_TIMEOUT':'240'}):
            self.assertEqual(worker.creative_environment()['HERMES_API_CALL_STALE_TIMEOUT'],'240')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.packet={'command':{'kind':'plan'}}
        self.result={'reply':'方案供确认','plan':{'title':'test'}}

    def test_stdout_answer_is_saved_and_not_invoked_twice(self):
        def run(*args,**kw): kw['stdout'].write(json.dumps(self.result).encode())
        with patch.object(worker.subprocess,'run',side_effect=run) as invoke:
            self.assertEqual(worker.create_original(self.packet,self.directory),self.result)
            self.assertEqual(worker.create_original(self.packet,self.directory),self.result)
            invoke.assert_called_once()
            self.assertIn('HERMES_API_CALL_STALE_TIMEOUT',invoke.call_args.kwargs['env'])
            self.assertEqual(invoke.call_args.kwargs['timeout'],480)
        self.assertTrue((self.directory/'hermes.stdout.txt').exists())

    def test_repair_prompt_includes_frozen_result_and_diagnostics(self):
        self.packet['repair_context']={'original_result':{'lyrics':'original lyrics'},
            'diagnostics':{'issues':[{'code':'SPEC_SCORE_MISMATCH','message':'84 vs 76'}]},'round':1,'max_rounds':2}
        def run(*args,**kw):kw['stdout'].write(json.dumps(self.result).encode())
        with patch.object(worker.subprocess,'run',side_effect=run):
            worker.create_original(self.packet,self.directory)
        query=(self.directory/'query.txt').read_text(encoding='utf-8')
        self.assertIn('bounded REPAIR',query)
        self.assertIn('original lyrics',query)
        self.assertIn('84 vs 76',query)
        self.assertIn('Keep its lyrics EXACTLY',query)

    def test_complete_stdout_recovers_after_interruption_without_model(self):
        (self.directory/'model-started.json').write_text('{}')
        (self.directory/'hermes.stdout.txt').write_text(json.dumps(self.result),encoding='utf-8')
        with patch.object(worker.subprocess,'run') as invoke:
            self.assertEqual(worker.create_original(self.packet,self.directory),self.result)
            invoke.assert_not_called()

    def test_partial_response_never_reinvokes_model(self):
        (self.directory/'model-started.json').write_text('{}')
        (self.directory/'hermes.stdout.txt').write_text('{"reply":')
        with patch.object(worker.subprocess,'run') as invoke:
            with self.assertRaisesRegex(ValueError,'MODEL_INTERRUPTED_REVIEW_REQUIRED'):
                worker.create_original(self.packet,self.directory)
            invoke.assert_not_called()

    def test_extra_fields_and_non_json_are_rejected_preserving_stdout(self):
        for raw in ('Here is your plan', json.dumps({**self.result,'proposal_path':'/elsewhere'})):
            (self.directory/'hermes.stdout.txt').write_text(raw)
            with self.assertRaises(ValueError):worker.read_creative_response(self.packet,self.directory)
            self.assertEqual((self.directory/'hermes.stdout.txt').read_text(),raw)

    def test_direction_uses_fresh_cli_and_scope_specific_response(self):
        for mode,result in [('song',dict(abc='score',lyrics='words',style='folk',summary='test')),
                            ('section',dict(section_abc='score',section_lyrics='words',summary='test'))]:
            directory=self.directory/mode;directory.mkdir()
            packet={'command':{'kind':'direction','scope':{'mode':mode}}}
            def run(*args,**kw): kw['stdout'].write(json.dumps(result).encode())
            with patch.object(worker.subprocess,'run',side_effect=run) as invoke:
                self.assertEqual(worker.create_original(packet,directory),result)
                self.assertIn('--oneshot',invoke.call_args[0][0])
            query=(directory/'query.txt').read_text()
            self.assertIn('Revise the frozen source',query)
