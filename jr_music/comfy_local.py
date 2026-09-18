"""Local development adapter: official CLI submission; read-only HTTP recovery.

No direct POST /prompt, no global cancellation, no hidden automatic retry.
Production Hermes uses the same ledger through its configured official MCP.
"""
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPRedirectHandler

from .render import SERVER
from .render_template import graph_only,remote_match
from .store import StoreError, canonical, require, sha


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise StoreError('REMOTE_REDIRECT_DENIED')


class LocalComfy:
    def __init__(self, renders, cli, work_root, *, actor='local_executor', environment_probe=None):
        self.renders, self.cli = renders, str(cli)
        self.server = dict(SERVER)
        self.environment_probe=environment_probe
        self.work_root, self.actor = Path(work_root).resolve(), actor
        self.opener = build_opener(NoRedirect())

    def fetch(self, route):
        with self.opener.open(self.server['url'] + route, timeout=30) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
            require(len(raw) <= 16 * 1024 * 1024, 'REMOTE_RESPONSE_TOO_LARGE')
            return json.loads(raw)

    def cli_call(self, args, timeout=60):
        result = subprocess.run([self.cli, '--where', 'local', '--json', *args],
            cwd=self.work_root, env={**os.environ, 'COMFY_LOCAL_URL': self.server['url'],'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'},
            capture_output=True, timeout=timeout, check=False)
        try:
            value = json.loads(result.stdout.decode('utf-8-sig'))
        except (ValueError, UnicodeError) as exc:
            raise StoreError('COMFY_INVALID_RESPONSE') from exc
        return result.returncode, value

    def build(self, project_id, render_id):
        job = self.renders.get(project_id, render_id)
        require(job['state'] in ('created','prepared'), 'RENDER_STATE_CONFLICT')
        bundle = self.renders.bundle(project_id, render_id)
        folder = self.work_root / render_id
        folder.mkdir(parents=True, exist_ok=True)
        library = folder / 'fragments'
        library.mkdir(exist_ok=True)
        (folder / 'blueprint.yaml').write_bytes(canonical(bundle['blueprint']))
        for name, fragment in bundle['fragments'].items():
            (library / (name + '.json')).write_bytes(canonical(fragment))
        graph_path = folder / 'workflow.api.json'
        rc, compiled = self.cli_call(['workflow', 'compose', str(folder / 'blueprint.yaml'), '--lib', str(library), '-o', str(graph_path)])
        require(rc == 0 and compiled.get('ok') is True, 'COMPILE_FAILED')
        workflow = json.loads(graph_path.read_text(encoding='utf-8'))
        return workflow

    def compile(self, project_id, render_id):
        workflow = self.build(project_id, render_id)
        graph_path = self.work_root / render_id / 'workflow.api.json'
        rc, checked = self.cli_call(['validate', '--workflow', str(graph_path)])
        require(rc == 0 and checked.get('ok') is True and checked.get('data', {}).get('valid') is True, 'PREFLIGHT_FAILED')
        stats = self.fetch('/system_stats')
        devices = stats.get('devices', [])
        require(any('3060' in d.get('name', '') for d in devices), 'SERVER_HARDWARE_MISMATCH')
        return self.renders.bind_workflow(project_id, render_id, workflow,
            dict(valid=True, server_id=self.server['server_id'], workflow_sha256=sha(canonical(graph_only(workflow))),
                 official_validation=checked, environment=stats), actor=self.actor)

    def submit(self, project_id, render_id):
        require(self.renders.get(project_id, render_id)['schema_version'] not in ('render/3','render/4'),
                'HERMES_MCP_SUBMISSION_REQUIRED')
        # Every invocation must acquire a fresh ONE-SHOT claim. Re-running this
        # method after a crash is refused, even with the same caller identity.
        self.check_environment(project_id,render_id)
        graph = self.renders.workflow(project_id, render_id)
        folder = self.work_root / render_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'dispatch.api.json'
        path.write_bytes(canonical(graph))
        self.renders.claim(project_id, render_id, actor=self.actor)
        try:
            rc, result = self.cli_call(['run', '--workflow', str(path), '--host', '127.0.0.1', '--port', '8188',
                                        '--wait', '--no-notify'], timeout=180)
            (folder / 'dispatch-result.json').write_bytes(canonical(result))
            # Never infer success from the CLI response; history must match.
            data = result.get('data') or {}
            prompt_id = data.get('prompt_id')
            if prompt_id:
                self.renders.receipt(project_id, render_id, self.server['server_id'], prompt_id, actor=self.actor)
            else:
                self.renders.issue(project_id, render_id, 'SUBMISSION_UNKNOWN', actor=self.actor)
        except (OSError, ValueError, subprocess.TimeoutExpired, StoreError):
            self.renders.issue(project_id, render_id, 'SUBMISSION_UNKNOWN', actor=self.actor)
        return self.reconcile(project_id, render_id)

    def check_environment(self,project_id,render_id):
        job=self.renders.get(project_id,render_id)
        if job['schema_version'] in ('render/2', 'render/3', 'render/4'):
            require(self.environment_probe is not None,'ENVIRONMENT_PROBE_REQUIRED')
            from .render_template import TEMPLATE_ID
            current=self.environment_probe() if job['template_id']==TEMPLATE_ID or job['schema_version']=='render/4' else self.environment_probe(template_id=job['template_id'])
            require(sha(canonical(current))==job['environment_sha256'],'RENDER_ENVIRONMENT_CHANGED')

    def reconcile(self, project_id, render_id):
        job = self.renders.get(project_id, render_id)
        if job['state'] in ('created', 'prepared', 'succeeded', 'failed', 'cancelled', 'preflight_failed'):
            return job
        try:
            queue = self.fetch('/queue')
            if job['prompt_id'] is None:
                # Unique filename prefix is in the frozen graph. Match the WHOLE
                # graph, not just a filename/title or the latest completed job.
                histories = self.fetch('/history?max_items=200')
                candidates = {}
                prompts = queue.get('queue_running', []) + queue.get('queue_pending', [])
                prompts += [h.get('prompt') for h in histories.values() if isinstance(h, dict)]
                for prompt in prompts:
                    if isinstance(prompt, list) and len(prompt) > 2:
                        try:
                            remote_match(self.renders.workflow(project_id,render_id),prompt[2],allow_float_coercion=job['schema_version'] in ('render/2', 'render/3', 'render/4'))
                            matches=True
                        except StoreError:
                            matches = False
                        if matches:
                            candidates[prompt[1]] = prompt
                if len(candidates) != 1:
                    return self.renders.issue(project_id, render_id, 'SUBMISSION_UNKNOWN', actor=self.actor)
                job = self.renders.receipt(project_id, render_id, self.server['server_id'], next(iter(candidates)), actor=self.actor)
            histories = self.fetch('/history/' + job['prompt_id'])
            if job['prompt_id'] in histories:
                return self.renders.observe(project_id, render_id, self.server['server_id'], history=histories[job['prompt_id']], actor=self.actor)
            if any(isinstance(p, list) and len(p) > 1 and p[1] == job['prompt_id']
                   for p in queue.get('queue_running', []) + queue.get('queue_pending', [])):
                return self.renders.observe(project_id, render_id, self.server['server_id'], queue=queue, actor=self.actor)
            return self.renders.issue(project_id, render_id, 'HISTORY_MISSING', actor=self.actor)
        except (OSError, ValueError):
            return self.renders.issue(project_id, render_id, 'CONNECTION_LOST', actor=self.actor)
        except StoreError as exc:
            if exc.code == 'ARTIFACT_MISSING':
                return self.renders.issue(project_id, render_id, 'ARTIFACT_MISSING', actor=self.actor)
            raise

    def collect(self, project_id, render_id):
        job = self.renders.get(project_id, render_id)
        if job['state'] == 'succeeded':
            return job
        require(job['state'] == 'collecting', 'RENDER_STATE_CONFLICT')
        self.check_environment(project_id,render_id)
        try:
            with self.opener.open(self.server['url'] + '/view?' + urlencode(job['output_descriptor']), timeout=60) as response:
                length = response.headers.get('Content-Length')
                return self.renders.collect(project_id, render_id, response, actor=self.actor, length=int(length) if length else None)
        except (OSError, ValueError):
            return self.renders.issue(project_id, render_id, 'DOWNLOAD_FAILED', actor=self.actor)
        except StoreError as exc:
            if exc.code in ('AUDIO_DECODE_FAILED', 'AUDIO_TOOL_UNAVAILABLE'):
                return self.renders.issue(project_id, render_id, exc.code, actor=self.actor)
            raise
