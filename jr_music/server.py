"""Loopback HTTP adapter with token-bound agent/producer identities."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import shutil
import time
from urllib.parse import urlsplit

from .store import Store, StoreError, canonical, identifier
from .render import RenderService
from .audio import AudioVerifier
from .music import MusicService
from .assessments import AssessmentService
from .composition import CompositionService
from .listening import ListeningService
from .producer import ProducerService
from .creation import CreationService


def make_server(store, credentials, port=8766, renders=None, hermes=None, libraries=None, agent_status=None):
    renders = renders or RenderService(store)
    music = MusicService(store)
    assessments=AssessmentService(store)
    compositions = CompositionService(store)
    listening = ListeningService(store)
    agent_status = agent_status if agent_status is not None else {}
    contexts = {'studio': (store, renders, hermes, music, assessments, compositions, listening, ProducerService(store, hermes))}
    for name, (extra_store, extra_renders, extra_bridge) in (libraries or {}).items():
        identifier(name)
        if name == 'studio':
            raise ValueError('Reserved library name')
        contexts[name] = (extra_store, extra_renders, extra_bridge, MusicService(extra_store),
            AssessmentService(extra_store), CompositionService(extra_store), ListeningService(extra_store),
            ProducerService(extra_store, extra_bridge))
    tokens = [entry['token'] for entry in credentials]
    if not credentials or len(tokens) != len(set(tokens)) or any(not isinstance(t, str) or len(t) < 32 for t in tokens):
        raise ValueError('Use distinct tokens of at least 32 characters')
    for entry in credentials:
        identifier(entry['actor_id'])
        if entry['role'] not in ('agent', 'producer', 'executor'):
            raise ValueError('Unknown role')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'JRMusic/0.1'

        def log_message(self, *_args):
            pass  # Never log tokens or song content.

        def send_json(self, status, result):
            # Windows can reset a connection closed with unread request bytes,
            # hiding an otherwise valid 401/403. Drain only small declared bodies
            # with a short deadline; never ingest an untrusted large upload here.
            length = self.headers.get('Content-Length', '')
            if status >= 400 and not getattr(self, '_body_read', False) and length.isdigit() and 0 < int(length) <= 2 * 1024 * 1024:
                previous_timeout = self.connection.gettimeout()
                try:
                    self.connection.settimeout(1)
                    self.rfile.read(int(length))
                except OSError:
                    pass
                finally:
                    self.connection.settimeout(previous_timeout)
                    self._body_read = True
            content = canonical(result)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(content)

        def principal(self):
            auth = self.headers.get('Authorization', '')
            token = auth[7:] if auth.startswith('Bearer ') else ''
            for entry in credentials:
                if secrets.compare_digest(token.encode('utf-8'), entry['token'].encode('utf-8')):
                    return entry
            raise StoreError('UNAUTHORIZED')

        def body(self, required, optional=()):
            length = self.headers.get('Content-Length', '')
            if not length.isdigit() or not 0 < int(length) <= 2 * 1024 * 1024:
                raise StoreError('INVALID_BODY_SIZE')
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise StoreError('JSON_REQUIRED')
            self._body_read = True
            value = json.loads(self.rfile.read(int(length)))
            if not isinstance(value, dict) or not set(required) <= value.keys() or not value.keys() <= set(required) | set(optional):
                raise StoreError('INVALID_FIELDS')
            return value

        def dispatch(self, method):
            try:
                principal = self.principal()
                actor = principal['actor_id']
                parsed = urlsplit(self.path)
                if parsed.query or parsed.fragment:
                    raise StoreError('INVALID_ROUTE')
                parts = parsed.path.strip('/').split('/')
                library = 'studio'
                if len(parts) >= 3 and parts[0] == 'libraries':
                    library, parts = parts[1], parts[2:]
                if library not in contexts:
                    raise StoreError('LIBRARY_NOT_FOUND')
                store, renders, hermes, music, assessments, compositions, listening, producer = contexts[library]
                if parts == ['health'] and method == 'GET':
                    result = dict(status='ok', protocol='storage/1', render_protocol='render/1', role=principal['role'], actor_id=actor)
                elif parts == ['integration'] and method == 'GET':
                    result = hermes.info(actor) if hermes else dict(protocol='hermes-integration/1', enabled=False)
                elif parts in (['mcp-servers'],['mcp-servers','refresh'],['mcp-servers','default']):
                    if principal['role']!='agent':raise StoreError('AGENT_REQUIRED')
                    if not hermes or not hermes.routing:raise StoreError('MCP_ROUTING_UNAVAILABLE')
                    if parts==['mcp-servers'] and method=='GET':result=hermes.routing.public(actor)
                    elif parts==['mcp-servers','refresh'] and method=='POST':
                        self.body(());result=hermes.routing.refresh(actor)
                    elif parts==['mcp-servers','default'] and method=='POST':
                        value=self.body(('mcp_alias',));result=hermes.routing.set_default(actor,value['mcp_alias'])
                    else:raise StoreError('INVALID_ROUTE')
                elif parts == ['libraries'] and method == 'GET':
                    result = list(contexts)
                elif parts == ['agent-commands'] and method == 'GET':
                    if principal['role'] != 'agent':
                        raise StoreError('AGENT_REQUIRED')
                    result = producer.list(actor=actor)
                elif parts == ['agent-heartbeat'] and method == 'POST':
                    if principal['role'] != 'agent': raise StoreError('AGENT_REQUIRED')
                    value = self.body(('state',))
                    if value['state'] not in ('idle','working'): raise StoreError('INVALID_WORKER_STATE')
                    agent_status[actor] = dict(state=value['state'], seen_at=time.time())
                    from .quality_loop import resume_pending
                    resume_pending(producer)
                    from .draft_repair import resume
                    resume(producer)
                    result = dict(recorded=True)
                elif parts == ['projects'] and method == 'GET':
                    result = store.list_projects()
                elif parts == ['projects'] and method == 'POST':
                    value = self.body(('title', 'idempotency_key'))
                    result = store.create_project(value['title'], actor=actor, key=value['idempotency_key'])
                elif len(parts) >= 2 and parts[0] == 'projects':
                    project_id = identifier(parts[1])
                    if len(parts) == 2 and method == 'GET':
                        result = store.get_project(project_id)
                    elif len(parts) == 3 and parts[2] == 'revisions' and method == 'GET':
                        result = store.list_revisions(project_id)
                    elif len(parts) == 3 and parts[2] == 'events' and method == 'GET':
                        result = store.events(project_id)
                    elif len(parts)==5 and parts[2]=='commands' and parts[4]=='draft-repair' and method=='POST':
                        from .draft_repair import start
                        value=self.body(('idempotency_key',))
                        result=start(producer,project_id,parts[3],actor=actor,key=value['idempotency_key'])
                    elif len(parts)==5 and parts[2]=='renders' and parts[4].startswith('quality'):
                        from .quality_loop import route
                        result=route(producer,project_id,parts[3],parts[4],method,self.body,actor=actor)
                    elif len(parts) >= 4 and parts[2] == 'agent-commands':
                        if principal['role'] != 'agent':
                            raise StoreError('AGENT_REQUIRED')
                        command_id = identifier(parts[3])
                        producer.get(project_id, command_id, actor)
                        if len(parts) == 4 and method == 'GET':
                            result = producer.packet(project_id, command_id, actor=actor)
                        elif len(parts) == 5 and method == 'POST' and parts[4] == 'claim':
                            self.body(())
                            result = producer.claim(project_id, command_id, actor=actor)
                        elif len(parts) == 5 and method == 'POST' and parts[4] == 'proposal':
                            value = self.body(('edits', 'summary', 'provenance'))
                            result = producer.propose(project_id, command_id, actor=actor, **value)
                        elif len(parts) == 5 and method == 'POST' and parts[4] == 'creative-proposal':
                            value = self.body(('result', 'provenance'),('format_check',))
                            result = CreationService(producer).propose(project_id, command_id, actor=actor, **value)
                        elif len(parts) == 5 and method == 'POST' and parts[4] in ('analysis-dispatch','analysis-result'):
                            from . import audio_analysis
                            if parts[4]=='analysis-dispatch':
                                self.body(())
                                result=audio_analysis.packet(producer,project_id,command_id,actor=actor)
                            else:
                                value=self.body(('receipt',))
                                result=audio_analysis.receive(producer,project_id,command_id,value['receipt'],actor=actor)
                        elif len(parts) == 5 and method == 'POST' and parts[4] == 'progress':
                            from .command_status import progress
                            value = self.body(('report',))
                            result = progress(producer, project_id, command_id, value['report'], actor=actor)
                        elif len(parts) == 5 and method == 'POST' and parts[4] in ('issue', 'refresh'):
                            value = self.body(('idempotency_key',), ('issue',))
                            result = producer.transition(project_id, command_id, parts[4], actor=actor,
                                key=value['idempotency_key'], issue=value.get('issue'))
                        else:
                            raise StoreError('INVALID_ROUTE')
                    elif len(parts) == 3 and parts[2] == 'experiments' and method == 'GET':
                        result = music.list(project_id)
                    elif len(parts) == 3 and parts[2] == 'experiments' and method == 'POST':
                        value = self.body(('spec', 'idempotency_key'))
                        result = music.create(project_id, value['spec'], actor=actor, key=value['idempotency_key'])
                    elif len(parts) >= 4 and parts[2] == 'experiments':
                        experiment_id = identifier(parts[3])
                        if len(parts) == 4 and method == 'GET':
                            result = music.get(project_id, experiment_id)
                        elif len(parts)==5 and parts[4]=='assessments' and method=='GET':
                            result=assessments.list(project_id,experiment_id)
                        elif len(parts)==5 and parts[4]=='composer-requests' and method=='POST':
                            value=self.body(('variant','idempotency_key'))
                            result=compositions.prepare(project_id,experiment_id,value['variant'],actor=actor,key=value['idempotency_key'])
                        elif len(parts)==5 and parts[4]=='composer-results' and method=='POST':
                            value=self.body(('result','idempotency_key'))
                            result=compositions.accept(project_id,experiment_id,value['result'],actor=actor,key=value['idempotency_key'])
                        elif len(parts)==5 and parts[4]=='listening-sessions' and method=='POST':
                            value=self.body(('render_ids','correction_ids','idempotency_key'))
                            result=listening.create(project_id,experiment_id,value['render_ids'],value['correction_ids'],actor=actor,key=value['idempotency_key'])
                        elif len(parts)==5 and parts[4]=='listening-feedback' and method=='POST':
                            if principal['role']!='producer':raise StoreError('PRODUCER_REQUIRED')
                            value=self.body(('session_id','feedback','idempotency_key'))
                            result=listening.feedback(project_id,experiment_id,value['session_id'],value['feedback'],actor=actor,key=value['idempotency_key'])
                        elif len(parts)==5 and parts[4]=='assessments' and method=='POST':
                            if principal['role']!='producer':raise StoreError('PRODUCER_REQUIRED')
                            value=self.body(('feedback','idempotency_key'))
                            result=assessments.feedback(project_id,experiment_id,value['feedback'],actor=actor,key=value['idempotency_key'])
                        elif len(parts)==5 and parts[4]=='semantic-requests' and method=='POST':
                            value=self.body(('idempotency_key',))
                            result=assessments.semantic_request(project_id,experiment_id,actor=actor,key=value['idempotency_key'])
                        elif len(parts)==5 and parts[4]=='semantic-results' and method=='POST':
                            if principal['role']!='executor':raise StoreError('EXECUTOR_REQUIRED')
                            value=self.body(('request_id','result','idempotency_key'))
                            result=assessments.semantic_result(project_id,experiment_id,value['request_id'],value['result'],actor=actor,key=value['idempotency_key'])
                        elif len(parts) == 5 and parts[4] == 'candidates' and method == 'POST':
                            value = self.body(('proposal', 'idempotency_key'))
                            result = music.candidate(project_id, experiment_id, value['proposal'], actor=actor, key=value['idempotency_key'])
                        elif len(parts) == 5 and parts[4] == 'reviews' and method == 'POST':
                            value = self.body(('variant', 'idempotency_key'), ('reference_texts', 'render_id'))
                            key = value.pop('idempotency_key')
                            result = music.review(project_id, experiment_id, actor=actor, key=key, **value)
                        elif len(parts) == 5 and parts[4] == 'feedback' and method == 'POST':
                            if principal['role'] != 'producer':
                                raise StoreError('PRODUCER_REQUIRED')
                            value = self.body(('feedback', 'idempotency_key'))
                            result = music.feedback(project_id, experiment_id, value['feedback'], actor=actor, key=value['idempotency_key'])
                        else:
                            raise StoreError('INVALID_ROUTE')
                    elif len(parts) == 4 and parts[2] == 'revisions' and method == 'GET':
                        result = store.get_revision(project_id, parts[3])
                    elif len(parts)==5 and parts[2]=='revisions' and parts[4].startswith('score.') and method=='GET':
                        from .score_export import download
                        raw,media,disposition=download(store,project_id,parts[3],parts[4][6:])
                        self.send_response(200)
                        for name,value in [('Content-Type',media),('Content-Length',str(len(raw))),('Content-Disposition',disposition),
                                           ('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')]:self.send_header(name,value)
                        self.end_headers();self.wfile.write(raw);return
                    elif len(parts) >= 4 and parts[2] == 'renders':
                        render_id = identifier(parts[3])
                        if len(parts) == 4 and method == 'GET':
                            result = renders.get(project_id, render_id)
                        elif len(parts) == 5 and method == 'GET' and parts[4] in ('bundle', 'workflow'):
                            result = getattr(renders, parts[4])(project_id, render_id)
                        elif len(parts) == 5 and method == 'POST':
                            operation = parts[4]
                            if operation in ('dispatch', 'reconcile'):
                                if hermes is None:
                                    raise StoreError('HERMES_NOT_CONFIGURED')
                                self.body(())
                                result = getattr(hermes, operation)(project_id, render_id, actor=actor)
                                self.send_json(200, dict(ok=True, data=result))
                                return
                            if operation != 'cancel' and principal['role'] != 'executor':
                                raise StoreError('EXECUTOR_REQUIRED')
                            if operation == 'cancel' and principal['role'] == 'agent' and renders.get(project_id, render_id)['created_by'] != actor:
                                raise StoreError('RENDER_OWNER_REQUIRED')
                            if operation in ('claim', 'cancel'):
                                self.body(())
                                result = getattr(renders, operation)(project_id, render_id, actor=actor)
                            elif operation == 'bind':
                                value = self.body(('workflow', 'preflight'))
                                result = renders.bind_workflow(project_id, render_id, actor=actor, **value)
                            elif operation=='environment':
                                value=self.body(('environment',))
                                result=renders.bind_environment(project_id,render_id,value['environment'],actor=actor)
                            elif operation == 'receipt':
                                value = self.body(('server_id', 'prompt_id'))
                                result = renders.receipt(project_id, render_id, actor=actor, **value)
                            elif operation == 'observe':
                                value = self.body(('server_id',), ('history', 'queue'))
                                result = renders.observe(project_id, render_id, actor=actor, **value)
                            elif operation == 'issue':
                                value = self.body(('code',))
                                result = renders.issue(project_id, render_id, actor=actor, **value)
                            elif operation == 'collect':
                                length = self.headers.get('Content-Length', '')
                                if self.headers.get('Transfer-Encoding') or not length.isdigit() or not 0 < int(length) <= 1024**3:
                                    raise StoreError('INVALID_BODY_SIZE')
                                if self.headers.get('Content-Type') != 'audio/flac':
                                    raise StoreError('NATIVE_FLAC_REQUIRED')
                                self.connection.settimeout(60)
                                self._body_read = True
                                result = renders.collect(project_id, render_id, self.rfile, actor=actor,
                                    length=int(length), expected_sha256=self.headers.get('X-Content-SHA256'))
                            else:
                                raise StoreError('INVALID_ROUTE')
                        else:
                            raise StoreError('INVALID_ROUTE')
                    elif len(parts) >= 5 and parts[2] == 'revisions':
                        revision_id = identifier(parts[3])
                        if len(parts) == 5 and parts[4] == 'hermes-renders' and method == 'POST':
                            if hermes is None:
                                raise StoreError('HERMES_NOT_CONFIGURED')
                            value = self.body(('idempotency_key',),('mcp_alias',))
                            result = hermes.prepare(project_id, revision_id, actor=actor, key=value['idempotency_key'],mcp_alias=value.get('mcp_alias'))
                        elif len(parts) == 5 and parts[4] == 'renders' and method == 'POST':
                            value = self.body(('idempotency_key',), ('server_id','environment_required'))
                            key = value.pop('idempotency_key')
                            result = renders.prepare(project_id, revision_id, actor=actor, key=key, **value)
                        elif len(parts) == 5 and parts[4] == 'renders' and method == 'GET':
                            result = renders.list(project_id, revision_id)
                        elif len(parts) == 5 and parts[4] == 'scores' and method == 'POST':
                            value = self.body(('idempotency_key',))
                            result = store.derive_score(project_id, revision_id, actor=actor, key=value['idempotency_key'])
                        elif len(parts) == 5 and parts[4] == 'scores' and method == 'GET':
                            result = store.list_scores(project_id, revision_id)
                        elif len(parts) == 6 and parts[4] == 'scores' and method == 'GET':
                            result = store.get_score(project_id, revision_id, parts[5])
                        elif len(parts) == 5 and parts[4] == 'edit-requests' and method == 'GET':
                            result = store.list_edit_requests(project_id, revision_id)
                        elif len(parts) == 5 and parts[4] == 'edit-requests' and method == 'POST':
                            if principal['role'] != 'producer':
                                raise StoreError('PRODUCER_REQUIRED')
                            value = self.body(('representation_id', 'source_abc_sha256', 'allowed_bar_ids', 'instruction', 'idempotency_key'))
                            key = value.pop('idempotency_key')
                            result = store.create_edit_request(project_id, revision_id, actor=actor, key=key, **value)
                        elif len(parts) == 5 and parts[4] == 'edits' and method == 'POST':
                            value = self.body(('request_id', 'edits', 'summary', 'idempotency_key'))
                            key = value.pop('idempotency_key')
                            result = store.apply_edit(project_id, revision_id, actor=actor, key=key, **value)
                        elif len(parts) == 5 and parts[4] == 'edit-verifications' and method == 'GET':
                            result = store.list_edit_verifications(project_id, revision_id)
                        elif len(parts) == 6 and parts[4] == 'edit-verifications' and method == 'GET':
                            result = store.get_edit_verification(project_id, revision_id, parts[5])
                        elif len(parts) == 7 and parts[4] == 'edit-verifications' and parts[6] == 'check' and method == 'GET':
                            result = store.check_edit_verification(project_id, revision_id, parts[5])
                        else:
                            raise StoreError('INVALID_ROUTE')
                    elif len(parts) == 4 and parts[2] == 'assets' and method == 'GET':
                        asset, content = store.open_asset(project_id, parts[3])
                        self.send_response(200)
                        # Serve as a download; an arbitrary archived payload is never executable HTML.
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Disposition', f'attachment; filename="{asset["name"]}"')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.send_header('Content-Length', str(asset['size']))
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        with content:
                            shutil.copyfileobj(content, self.wfile, length=1024 * 1024)
                        return
                    elif len(parts) == 3 and parts[2] == 'revisions' and method == 'POST':
                        value = self.body(('abc', 'brief', 'summary', 'idempotency_key'), ('parent_revision_id', 'expected_parent_snapshot_sha256'))
                        key = value.pop('idempotency_key')
                        result = store.add_revision(project_id, actor=actor, key=key, **value)
                    elif len(parts) == 3 and parts[2] == 'decisions' and method == 'POST':
                        if principal['role'] != 'producer':
                            raise StoreError('PRODUCER_REQUIRED')
                        value = self.body(('target_revision_id', 'action_name', 'expected_head_revision_id', 'expected_head_generation', 'reason', 'idempotency_key'), ('selected_render_id',))
                        key = value.pop('idempotency_key')
                        result = store.decide(project_id, actor=actor, key=key, **value)
                    else:
                        raise StoreError('INVALID_ROUTE')
                else:
                    raise StoreError('INVALID_ROUTE')
                self.send_json(200, dict(ok=True, data=result))
            except StoreError as exc:
                code = exc.code
                status = 401 if code == 'UNAUTHORIZED' else 403 if code in ('PRODUCER_REQUIRED', 'EXECUTOR_REQUIRED', 'RENDER_OWNER_REQUIRED') else 409 if code in ('HEAD_CONFLICT', 'STALE_BASE', 'STALE_SCORE', 'STALE_PITCH', 'IDEMPOTENCY_CONFLICT', 'RENDER_STATE_CONFLICT', 'DISPATCH_ALREADY_CLAIMED', 'REMOTE_PROMPT_ALREADY_BOUND') else 404 if code.endswith('_NOT_FOUND') or code == 'INVALID_ROUTE' else 400
                self.send_json(status, dict(ok=False, error=dict(code=code)))
            except (ValueError, TypeError, UnicodeError):
                self.send_json(400, dict(ok=False, error=dict(code='INVALID_REQUEST')))
            except sqlite3.OperationalError:
                self.send_json(503, dict(ok=False, error=dict(code='STORAGE_UNAVAILABLE')))
            except OSError:
                self.send_json(503, dict(ok=False, error=dict(code='IO_UNAVAILABLE')))

        def do_GET(self):
            self.dispatch('GET')

        def do_POST(self):
            self.dispatch('POST')

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('command', choices=['init-auth', 'serve'])
    parser.add_argument('--database', type=Path, default=Path('data/studio.sqlite3'))
    parser.add_argument('--auth', type=Path, default=Path('data/local-auth.json'))
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--hermes-config', type=Path)
    parser.add_argument('--experiments-database', type=Path)
    parser.add_argument('--console-port', type=int)
    args = parser.parse_args()
    if args.command == 'init-auth':
        args.auth.parent.mkdir(parents=True, exist_ok=True)
        credentials = [dict(actor_id=actor, role=role, token=secrets.token_urlsafe(32))
                       for actor, role in [('xiaowu', 'agent'), ('yinyue', 'agent'), ('producer', 'producer')]]
        fd = os.open(args.auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(canonical(credentials))
        print('Created local credential file. Keep producer credentials out of agent configuration.')
    else:
        credentials = json.loads(args.auth.read_text(encoding='utf-8'))
        store = Store(args.database)
        renders = RenderService(store, AudioVerifier(args.ffmpeg, args.ffprobe))
        hermes = None
        if args.hermes_config:
            from .hermes import load_bridge
            hermes = load_bridge(args.hermes_config, renders, args.ffmpeg, args.ffprobe)
        libraries = {}
        if args.experiments_database:
            extra = Store(args.experiments_database)
            extra_renders = RenderService(extra, AudioVerifier(args.ffmpeg, args.ffprobe))
            extra_bridge = load_bridge(args.hermes_config, extra_renders, args.ffmpeg, args.ffprobe) if args.hermes_config else None
            libraries['experiments'] = (extra, extra_renders, extra_bridge)
        statuses = {}
        server = make_server(store, credentials, args.port, renders, hermes, libraries, statuses)
        console = None
        if args.console_port:
            import threading
            from .console import make_console
            console_libraries = {'studio': ('创作工程',store,hermes)}
            console_libraries.update({k: ('试听实验',v[0],v[2]) for k,v in libraries.items()})
            console = make_console(console_libraries,args.console_port,statuses)
            threading.Thread(target=console.serve_forever,daemon=True).start()
            print(f'Producer Console: http://127.0.0.1:{console.server_port}',flush=True)
        print(f'JR Music local API: http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if console: console.shutdown(); console.server_close()
            server.server_close()


if __name__ == '__main__':
    main()
