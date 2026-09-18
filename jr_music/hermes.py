"""Hermes dispatch handoff; submission stays in the existing official MCP.

This adapter supports the first, verified topology only: JR and the MCP process
share this Windows host, while Hermes reaches JR through an SSH tunnel.
No agent-supplied history, output paths or success flags are trusted.
"""
import json
import os
from pathlib import Path
import threading

from .comfy_local import LocalComfy
from .render_environment import collect_environment
from .store import canonical, identifier, require, sha, StoreError


def validate_binding(value, actor):
    if isinstance(value,dict) and value.get('schema_version')=='hermes-binding/2':
        from urllib.parse import urlsplit
        import re
        require(set(value)=={'schema_version','actor_id','mcp_alias','config_sha256','server_id','url','hardware','info','evidence_basis'},'INVALID_PRODUCTION_BINDING')
        require(value['actor_id']==actor and value['evidence_basis']=='hermes_config_and_live_mcp','INVALID_PRODUCTION_BINDING')
        identifier(value['mcp_alias']);identifier(value['server_id'])
        require(isinstance(value['config_sha256'],str) and re.fullmatch('[a-f0-9]{64}',value['config_sha256']) is not None,'INVALID_PRODUCTION_BINDING')
        url=urlsplit(value['url'])
        require(url.scheme=='http' and url.hostname and url.port and not url.username and not url.password and url.path in ('','/') and not url.query and not url.fragment,'INVALID_PRODUCTION_BINDING')
        require(isinstance(value['hardware'],str) and len(value['hardware'])<200 and isinstance(value['info'],dict),'INVALID_PRODUCTION_BINDING')
        return
    fields = {'schema_version', 'actor_id', 'mcp_alias', 'hermes_host', 'mcp_host',
              'workspace', 'topology', 'evidence_basis'}
    require(isinstance(value, dict) and set(value) == fields, 'INVALID_PRODUCTION_BINDING')
    require(value['schema_version'] == 'hermes-binding/1' and value['actor_id'] == actor,
            'INVALID_PRODUCTION_BINDING')
    require(value['topology'] == 'jr_and_mcp_same_host' and
            value['evidence_basis'] == 'operator_configuration', 'INVALID_PRODUCTION_BINDING')
    for key in fields - {'schema_version'}:
        require(isinstance(value[key], str) and 0 < len(value[key]) <= 1024, 'INVALID_PRODUCTION_BINDING')
    identifier(value['mcp_alias'])
    require(Path(value['workspace']).is_absolute(), 'MCP_WORKSPACE_MUST_BE_ABSOLUTE')


class HermesBridge:
    def __init__(self, renders, adapter, bindings, routing=None):
        self.renders, self.adapter = renders, adapter
        self.routing=routing
        self.bindings = json.loads(json.dumps(bindings))
        for actor, binding in self.bindings.items():
            identifier(actor)
            validate_binding(binding, actor)
            require(Path(binding['workspace']).resolve() == adapter.work_root,
                    'MCP_WORKSPACE_MISMATCH')
        # Serialize local preparation/collection. The durable ledger claim also
        # protects against a second process and survives restarts.
        self.lock = threading.Lock()

    def info(self, actor):
        result = dict(protocol='hermes-integration/1', enabled=actor in self.bindings,
                    binding=self.bindings.get(actor), submission='existing_official_comfy_mcp',
                    recovery='jr_read_only_comfy_history', targeted_cancel=False)
        if self.routing and actor in self.bindings:result['mcp_servers']=self.routing.public(actor)
        return result

    def selection(self, actor, mcp_alias=None):
        if self.routing:return self.routing.resolve(actor,mcp_alias)
        require(mcp_alias in (None,self.bindings.get(actor,{}).get('mcp_alias')),'MCP_SERVER_UNAVAILABLE')
        return self.bindings[actor]

    def prepare(self, project_id, revision_id, *, actor, key, mcp_alias=None, binding=None):
        require(actor in self.bindings, 'HERMES_ACTOR_NOT_CONFIGURED')
        # Resolve omitted defaults only on the first request. An idempotent retry
        # must keep the target chosen before a later default change.
        if binding is None and mcp_alias is None:
            with self.renders.store.connect() as db:
                for row in db.execute('SELECT document FROM render_jobs WHERE project_id=? AND revision_id=?',(project_id,revision_id)):
                    old=json.loads(row[0])
                    if old.get('routing_key')==key and old['created_by']==actor:binding=old['production_binding'];break
        binding=binding or self.selection(actor,mcp_alias)
        validate_binding(binding,actor)
        if binding['schema_version']=='hermes-binding/2':
            server=dict(server_id=binding['server_id'],url=binding['url'],hardware=binding['hardware'],transport='hermes-comfy-mcp',production_transport='hermes-comfy-mcp',targeted_cancel=False)
            return self.renders.prepare(project_id,revision_id,actor=actor,key=key,server_id=server['server_id'],server=server,environment_required=True,production_binding=binding)
        return self.renders.prepare(project_id, revision_id, actor=actor, key=key,
            environment_required=True, production_binding=binding)

    def owned(self, project_id, render_id, actor):
        job = self.renders.get(project_id, render_id)
        require(job['created_by'] == actor, 'RENDER_OWNER_REQUIRED')
        require(job['schema_version'] in ('render/3','render/4'), 'HERMES_RENDER_REQUIRED')
        if job['schema_version']=='render/3':
            require(job['production_binding'] == self.bindings.get(actor), 'HERMES_BINDING_CHANGED')
        else:require(self.routing is not None,'MCP_ROUTING_UNAVAILABLE')
        return job

    def target_adapter(self, job):
        if job['schema_version']=='render/4':
            from .comfy_remote import RemoteComfy
            return RemoteComfy(self.renders,self.adapter,self.routing,job['production_binding'],job['template_id'])
        return self.adapter

    def dispatch(self, project_id, render_id, *, actor):
        with self.lock:
            job = self.owned(project_id, render_id, actor)
            require(job['state'] in ('created', 'prepared') and job['claim_id'] is None,
                    'DISPATCH_ALREADY_CLAIMED')
            if job['schema_version']=='render/4':
                adapter=self.target_adapter(job)
                path=adapter.stage(project_id,render_id)
                job=self.renders.claim(project_id,render_id,actor=adapter.actor)
                return dict(schema_version='hermes-dispatch/2',project_id=project_id,render_id=render_id,
                    claim_id=job['claim_id'],workflow_sha256=job['workflow_sha256'],
                    mcp_alias=job['production_binding']['mcp_alias'],config_sha256=job['production_binding']['config_sha256'],
                    tool='run_workflow',arguments=dict(workflow_path=path,wait=False,timeout_seconds=110.0,confirm_spend=False),
                    retry_allowed=False,recovery_route=f'/projects/{project_id}/renders/{render_id}/reconcile')
            from .render_template import TEMPLATE_ID
            environment = self.adapter.environment_probe() if job['template_id']==TEMPLATE_ID else self.adapter.environment_probe(template_id=job['template_id'])
            self.renders.bind_environment(project_id, render_id, environment, actor=self.adapter.actor)
            if job['state'] == 'created':
                self.adapter.compile(project_id, render_id)
            self.adapter.check_environment(project_id, render_id)
            graph = self.renders.workflow(project_id, render_id)
            path = self.adapter.work_root / render_id / 'dispatch.api.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            content = canonical(graph)
            if path.exists():
                require(path.read_bytes() == content, 'DISPATCH_FILE_CHANGED')
            else:
                with path.open('xb') as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            # The permission is returned once only. If HTTP delivery is lost,
            # reconcile; do not return the same runnable permission on a retry.
            job = self.renders.claim(project_id, render_id, actor=self.adapter.actor)
            return dict(schema_version='hermes-dispatch/1', project_id=project_id,
                render_id=render_id, claim_id=job['claim_id'], workflow_sha256=sha(content),
                mcp_alias=job['production_binding']['mcp_alias'], tool='run_workflow',
                arguments=dict(workflow_path=str(path), wait=False, timeout_seconds=110.0,
                               confirm_spend=False),
                retry_allowed=False, recovery_route=f'/projects/{project_id}/renders/{render_id}/reconcile')

    def reconcile(self, project_id, render_id, *, actor):
        with self.lock:
            job=self.owned(project_id, render_id, actor)
            adapter=self.target_adapter(job)
            job = adapter.reconcile(project_id, render_id)
            if job['state'] == 'collecting':
                job = adapter.collect(project_id, render_id)
            return job


def load_bridge(path, renders, ffmpeg, ffprobe):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    require(set(config)-{'mcp_routing'} == {'schema_version', 'cli', 'work_root', 'environment', 'bindings'}
            and config['schema_version'] == 'hermes-deployment/1', 'INVALID_HERMES_DEPLOYMENT')
    env = config['environment']
    require(set(env) == {'comfy_root', 'checkpoint', 'cache_root', 'model_config'}, 'INVALID_ENVIRONMENT_CONFIG')
    def probe(**options):
        return collect_environment(**env, ffmpeg=ffmpeg, ffprobe=ffprobe, cli=config['cli'],**options)
    adapter = LocalComfy(renders, config['cli'], config['work_root'],
                         actor='hermes_executor', environment_probe=probe)
    routing=None
    if config.get('mcp_routing'):
        from .mcp_routing import shared
        routing=shared(path,config['mcp_routing'],config['bindings'])
    return HermesBridge(renders, adapter, config['bindings'],routing)
