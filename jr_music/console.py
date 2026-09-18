"""Loopback-only Producer Console. Browser receives a short-lived session, never an agent/API token."""
import argparse
import json
import mimetypes
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from .store import Store, StoreError, canonical, require
from .render import RenderService
from .producer import ProducerService
from .creation import CreationService
from .audio import AudioVerifier
from .score import parse_abc
from .lyric_navigation import lyric_navigation
from .score_audition import audition
from .score_review import read_review
from .score_repair import repair_preview, repair_revision
from .direction import scopes
from . import manual_edit
from . import score_export

UI = Path(__file__).parent / 'ui'


def make_console(libraries, port=8767, agent_status=None):
    """libraries maps an operator-controlled name to (label, Store, HermesBridge)."""
    sessions = {}
    agent_status = agent_status if agent_status is not None else {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def headers_out(self, status, media_type, size, extra=()):
            self.send_response(status)
            for name, value in [('Content-Type', media_type), ('Content-Length', str(size)),
                ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                ('Referrer-Policy', 'no-referrer'), ('X-Frame-Options', 'DENY'),
                ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")]+list(extra):
                self.send_header(name, value)
            self.end_headers()

        def json(self, status, data, extra=()):
            raw = canonical(data)
            self.headers_out(status, 'application/json; charset=utf-8', len(raw), extra)
            self.wfile.write(raw)

        def access(self, mutation=False):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            require(self.headers.get('Host') == origin[7:], 'INVALID_HOST')
            require(self.headers.get('Sec-Fetch-Site') not in ('cross-site', 'same-site'), 'CROSS_ORIGIN_DENIED')
            require(not self.headers.get('Origin') or self.headers['Origin'] == origin, 'CROSS_ORIGIN_DENIED')
            cookie = SimpleCookie()
            cookie.load(self.headers.get('Cookie', ''))
            sid = cookie.get('jr_producer')
            session = sessions.get(sid.value if sid else '')
            if mutation:
                require(self.headers.get('Origin') == origin and session and session['expires'] > time.monotonic()
                    and secrets.compare_digest(self.headers.get('X-JR-CSRF', ''), session['csrf']), 'CSRF_DENIED')
            return session if session and session['expires'] > time.monotonic() else None

        def body(self, required, optional=()):
            size = self.headers.get('Content-Length', '')
            require(size.isdigit() and 0 < int(size) <= 2*1024*1024, 'INVALID_BODY_SIZE')
            require(self.headers.get('Content-Type', '').split(';')[0] == 'application/json', 'JSON_REQUIRED')
            value = json.loads(self.rfile.read(int(size)))
            require(isinstance(value, dict) and set(required) <= value.keys() <= set(required)|set(optional), 'INVALID_FIELDS')
            return value

        def media(self, store, pid, aid):
            asset, stream = store.open_asset(pid, aid)
            with stream:
                require(asset['media_type'] in ('audio/wav', 'audio/flac') and asset['verification'] == 'decoded_pcm_verified', 'VERIFIED_AUDIO_REQUIRED')
                size = asset['size']
                start, end, status = 0, size-1, 200
                requested = self.headers.get('Range')
                if requested:
                    match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
                    if not match or not any(match.groups()):
                        self.headers_out(416, 'text/plain', 0, [('Content-Range', f'bytes */{size}')]); return
                    left, right = match.groups()
                    if left:
                        start, end = int(left), min(int(right), size-1) if right else size-1
                    else:
                        start, end = max(0, size-int(right)), size-1
                    if start > end or start >= size:
                        self.headers_out(416, 'text/plain', 0, [('Content-Range', f'bytes */{size}')]); return
                    status = 206
                extra = [('Accept-Ranges', 'bytes')]
                if status == 206:
                    extra += [('Content-Range', f'bytes {start}-{end}/{size}')]
                self.headers_out(status, asset['media_type'], end-start+1, extra)
                stream.seek(start)
                remaining = end-start+1
                while remaining:
                    chunk = stream.read(min(remaining, 256*1024))
                    if not chunk: break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def dispatch(self, method):
            try:
                session = self.access(method == 'POST')
                parsed = urlsplit(self.path)
                require(not parsed.query and not parsed.fragment, 'INVALID_ROUTE')
                path = parsed.path
                if path == '/api/session' and method == 'GET':
                    if not session:
                        for sid in list(sessions):
                            if sessions[sid]['expires'] < time.monotonic(): del sessions[sid]
                        sid = secrets.token_urlsafe(32)
                        session = dict(csrf=secrets.token_urlsafe(32), expires=time.monotonic()+12*3600)
                        sessions[sid] = session
                        extra = [('Set-Cookie', f'jr_producer={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200')]
                    else: extra = []
                    self.json(200, dict(ok=True, data=dict(csrf=session['csrf'])), extra); return
                if path.startswith('/api/'):
                    require(session is not None, 'SESSION_REQUIRED')
                    parts = path.strip('/').split('/')[1:]
                    if parts == ['libraries'] and method == 'GET':
                        result = [dict(id=k, label=v[0], projects=v[1].list_projects()) for k,v in libraries.items()]
                    elif parts == ['agents'] and method == 'GET':
                        result = {actor: dict(online=time.time()-value['seen_at']<45, state=value['state']) for actor,value in list(agent_status.items())}
                    elif parts == ['songcraft'] and method == 'GET':
                        from .songcraft import catalog
                        result = catalog()
                    elif len(parts) in (2,3) and parts[0]=='mcp-servers':
                        bridge=libraries['studio'][2]
                        require(bridge and bridge.routing,'MCP_ROUTING_UNAVAILABLE')
                        target=parts[1]
                        if len(parts)==2 and method=='GET':result=bridge.routing.public(target)
                        elif parts[-1]=='refresh' and method=='POST':
                            self.body((),('idempotency_key',));result=bridge.routing.refresh(target)
                        elif parts[-1]=='default' and method=='POST':
                            value=self.body(('mcp_alias',),('idempotency_key',));result=bridge.routing.set_default(target,value['mcp_alias'])
                        else:raise StoreError('INVALID_ROUTE')
                    else:
                        require(len(parts) >= 2 and parts[0] in libraries, 'INVALID_ROUTE')
                        _, store, bridge = libraries[parts[0]]
                        producer = ProducerService(store, bridge)
                        require(parts[1] == 'projects', 'INVALID_ROUTE')
                        if len(parts) == 2 and method == 'POST':
                            value = self.body(('title', 'idempotency_key'))
                            result = store.create_project(value['title'], actor='producer', key=value['idempotency_key'])
                        else:
                            require(len(parts) >= 3, 'INVALID_ROUTE')
                            pid = parts[2]
                            if len(parts) == 3 and method == 'GET':
                                revisions = []
                                for revision in store.list_revisions(pid):
                                    rid = revision['revision_id']
                                    value = store.get_revision(pid, rid)
                                    value['renders'] = producer.renders.list(pid, rid)
                                    value['score'] = parse_abc(value['snapshot']['abc'].encode('utf-8'))
                                    value['lyric_navigation'] = lyric_navigation(value['score'], value['snapshot']['brief'].get('lyrics', ''))
                                    from . import score_lyrics
                                    value['score_lyric_map']=score_lyrics.read(store,pid,rid)
                                    value['score_export']=score_export.availability(value,value['score_lyric_map'])
                                    from .lyric_density import analyze
                                    value['lyric_density']=analyze(value['snapshot']['abc'],value['snapshot']['brief']['lyrics'],value['score_lyric_map']['rows'])
                                    from .professional_skills import read as read_specs
                                    value['production_specs']=read_specs(store,pid,rid)
                                    value['score_audition'] = audition(value['score'])
                                    value['score_repair'] = repair_preview(value['snapshot']['abc'])
                                    value['direction_scopes'] = scopes(value['snapshot'])
                                    value['manual_editor'] = manual_edit.layout(value['snapshot'])
                                    value['protected_edits'] = [store.get_edit_verification(pid,rid,v['verification_id'])['manifest']['submitted_edits']
                                        for v in store.list_edit_verifications(pid,rid)]
                                    revisions.append(value)
                                with store.connect() as db:
                                    decisions = [json.loads(r[0]) for r in db.execute('SELECT document FROM decisions WHERE project_id=? ORDER BY rowid', (pid,))]
                                    historical_feedback = [json.loads(r[0]) for r in db.execute('''SELECT b.content FROM music_assessments a
                                        JOIN music_experiments e ON e.id=a.experiment_id JOIN blobs b ON b.hash=a.blob_hash
                                        WHERE e.project_id=? AND a.kind='producer_feedback' ORDER BY a.rowid''',(pid,))]
                                from .command_status import can_retry,can_recover_format
                                events=store.events(pid)
                                formats={e['command_id']:e for e in events if e['type']=='creative_format_checked'}
                                received={e['command_id'] for e in events if e['type'] in ('creation_result_received','direction_result_received')}
                                commands=[dict(c,can_retry=can_retry(c),can_recover_format=can_recover_format(c) and c['command_id'] not in received,
                                    format_check=formats.get(c['command_id'])) for c in producer.list(pid)]
                                result = dict(project=store.get_project(pid), revisions=revisions, commands=commands,
                                    decisions=decisions, historical_feedback=historical_feedback, events=store.events(pid), agents=list(bridge.bindings) if bridge else [])
                            elif len(parts)==6 and parts[3]=='revisions' and parts[5].startswith('score.') and method=='GET':
                                raw,media,disposition=score_export.download(store,pid,parts[4],parts[5][6:])
                                self.headers_out(200,media,len(raw),[('Content-Disposition',disposition)])
                                self.wfile.write(raw);return
                            elif len(parts) == 5 and parts[3] == 'audio' and method == 'GET':
                                self.media(store, pid, parts[4]); return
                            elif len(parts) == 6 and parts[3] == 'renders' and parts[5]=='song-check':
                                from . import song_check
                                if method=='GET':result=song_check.read(producer,pid,parts[4])
                                else:
                                    value=self.body(('expected_report_sha256','decision','note','idempotency_key'))
                                    result=song_check.save(producer,pid,parts[4],actor='producer',key=value.pop('idempotency_key'),**value)
                            elif len(parts) == 6 and parts[3] == 'renders' and parts[5] in ('analysis','analysis-resume','lyric-timeline'):
                                from . import audio_analysis, lyric_timeline
                                if parts[5]=='analysis':
                                    if method=='GET': result=audio_analysis.read(producer,pid,parts[4])
                                    else:
                                        value=self.body(('assigned_to','idempotency_key'))
                                        result=audio_analysis.start(producer,pid,parts[4],value['assigned_to'],actor='producer',key=value['idempotency_key'])
                                elif parts[5]=='analysis-resume' and method=='POST':
                                    self.body(('idempotency_key',))
                                    c=audio_analysis.latest(producer,pid,parts[4]);require(c is not None,'ANALYSIS_NOT_FOUND')
                                    result=audio_analysis.resume(producer,pid,c['command_id'])
                                elif parts[5]=='lyric-timeline' and method=='GET':result=lyric_timeline.read(producer,pid,parts[4])
                                elif parts[5]=='lyric-timeline' and method=='POST':
                                    value=self.body(('lines','expected_generation','idempotency_key'))
                                    result=lyric_timeline.save(producer,pid,parts[4],actor='producer',key=value.pop('idempotency_key'),**value)
                                else:raise StoreError('INVALID_ROUTE')
                            elif len(parts) == 6 and parts[3] == 'renders' and parts[5] == 'score-review' and method == 'GET':
                                from . import audio_analysis
                                analysis=audio_analysis.read(producer,pid,parts[4])
                                result = analysis.get('transcription') or read_review(store, pid, parts[4])
                            elif len(parts) == 4 and parts[3] == 'creation-discuss' and method == 'POST':
                                value = self.body(('assigned_to','instruction','target_duration','parent_command_id','idempotency_key'),('songcraft_selection','mcp_alias'))
                                result = CreationService(producer).discuss(pid, actor='producer', key=value.pop('idempotency_key'), **value)
                            elif len(parts) == 4 and parts[3] == 'creation-confirm' and method == 'POST':
                                value = self.body(('plan_command_id','expected_plan_sha256','idempotency_key'))
                                result = CreationService(producer).confirm(pid, actor='producer', key=value.pop('idempotency_key'), **value)
                            elif len(parts) == 4 and parts[3] == 'commands' and method == 'POST':
                                value = self.body(('revision_id','expected_snapshot_sha256','assigned_to','kind','instruction','allowed_bar_ids','idempotency_key'), ('target_duration','scope','preserve','songcraft_selection','density_limits','mcp_alias'))
                                result = producer.create(pid, actor='producer', key=value.pop('idempotency_key'), **value)
                            elif len(parts) == 4 and parts[3] == 'score-repair' and method == 'POST':
                                value = self.body(('revision_id','expected_snapshot_sha256','idempotency_key'))
                                result = repair_revision(store, pid, actor='producer', key=value.pop('idempotency_key'), **value)
                            elif len(parts)==4 and parts[3]=='score-lyric-map' and method=='POST':
                                from . import score_lyrics
                                value=self.body(('revision_id','expected_snapshot_sha256','expected_generation','rows','idempotency_key'))
                                rid=value.pop('revision_id')
                                result=score_lyrics.save(store,pid,rid,actor='producer',key=value.pop('idempotency_key'),**value)
                            elif len(parts) == 4 and parts[3] in ('manual-preview','manual-edit') and method == 'POST':
                                value = self.body(('revision_id','expected_snapshot_sha256','operation','idempotency_key'))
                                rid=value.pop('revision_id');key=value.pop('idempotency_key')
                                result = manual_edit.preview(store,pid,rid,**value) if parts[3]=='manual-preview' else manual_edit.save(store,pid,rid,actor='producer',key=key,**value)
                            elif len(parts) == 6 and parts[3] == 'commands' and method == 'POST':
                                value = self.body(('idempotency_key',))
                                require(parts[5] in ('cancel','refresh','retry','format-recover'), 'INVALID_ROUTE')
                                command = producer.get(pid, parts[4])
                                if parts[5] == 'refresh' and command['render_id'] and bridge:
                                    bridge.reconcile(pid, command['render_id'], actor=command['assigned_to'])
                                if parts[5] == 'format-recover':
                                    from .command_status import recover_format
                                    result = recover_format(producer,pid,parts[4],actor='producer',key=value['idempotency_key'])
                                elif parts[5] == 'retry':
                                    from .command_status import retry
                                    result = retry(producer, pid, parts[4], actor='producer', key=value['idempotency_key'])
                                else:
                                    result = producer.transition(pid, parts[4], parts[5], actor='producer', key=value['idempotency_key'])
                            elif len(parts) == 4 and parts[3] == 'feedback' and method == 'POST':
                                value = self.body(('revision_id','render_id','content','idempotency_key'))
                                result = producer.feedback(pid, actor='producer', key=value.pop('idempotency_key'), **value)
                            elif len(parts) == 4 and parts[3] == 'decisions' and method == 'POST':
                                value = self.body(('target_revision_id','action_name','expected_head_revision_id','expected_head_generation','reason','selected_render_id','idempotency_key'))
                                result = store.decide(pid, actor='producer', key=value.pop('idempotency_key'), **value)
                            elif len(parts) == 4 and parts[3] == 'revisions' and method == 'POST':
                                value = self.body(('abc','brief','summary','idempotency_key'))
                                result = store.add_revision(pid, actor='producer', key=value.pop('idempotency_key'), **value)
                            else: raise StoreError('INVALID_ROUTE')
                    self.json(200, dict(ok=True, data=result)); return
                require(method == 'GET', 'INVALID_ROUTE')
                files = {'/': 'index.html', '/app.js':'app.js', '/score-review.js':'score-review.js', '/manual-editor.js':'manual-editor.js', '/style.css':'style.css', '/vendor/abcjs-basic-min.js':'vendor/abcjs-basic-min.js'}
                files['/audio-analysis.js']='audio-analysis.js'
                files['/score-lyrics.js']='score-lyrics.js'
                require(path in files, 'INVALID_ROUTE')
                target = UI / files[path]
                raw = target.read_bytes()
                media = {'.js':'text/javascript', '.css':'text/css', '.html':'text/html'}[target.suffix]
                self.headers_out(200, media+'; charset=utf-8', len(raw))
                self.wfile.write(raw)
            except StoreError as exc:
                status = 403 if exc.code in ('CSRF_DENIED','INVALID_HOST','CROSS_ORIGIN_DENIED','SESSION_REQUIRED') else 409 if 'CONFLICT' in exc.code or exc.code == 'STALE_BASE' else 400
                self.json(status, dict(ok=False,error=dict(code=exc.code)))
            except (ValueError, TypeError, KeyError):
                self.json(400, dict(ok=False,error=dict(code='INVALID_REQUEST')))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (OSError, sqlite3.Error):
                self.json(503, dict(ok=False,error=dict(code='SERVICE_UNAVAILABLE')))

        def do_GET(self): self.dispatch('GET')
        def do_POST(self): self.dispatch('POST')

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--database', type=Path, default=Path('data/studio.sqlite3'))
    parser.add_argument('--experiments-database', type=Path, default=Path('data/m1-05a-demo.sqlite3'))
    parser.add_argument('--hermes-config', type=Path, default=Path('data/hermes-deployment.json'))
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--ffprobe', default='ffprobe')
    args = parser.parse_args()
    from .hermes import load_bridge
    libraries = {}
    for name, label, path in [('studio','创作工程',args.database), ('experiments','试听实验',args.experiments_database)]:
        store = Store(path)
        renders = RenderService(store, AudioVerifier(args.ffmpeg,args.ffprobe))
        bridge = load_bridge(args.hermes_config, renders, args.ffmpeg,args.ffprobe)
        libraries[name] = (label,store,bridge)
    server = make_console(libraries,args.port)
    print(f'Producer Console: http://127.0.0.1:{server.server_port}',flush=True)
    try: server.serve_forever()
    finally: server.server_close()


if __name__ == '__main__': main()
