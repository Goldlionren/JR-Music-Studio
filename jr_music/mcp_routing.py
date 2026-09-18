"""Hermes-owned MCP discovery, persistent defaults, immutable task routing."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from .store import canonical, identifier, require, sha, StoreError
from . import render_template as tpl


class McpRouting:
    def __init__(self, path, config, bindings):
        self.path=Path(path); self.config=config; self.bindings=bindings
        self.lock=threading.RLock(); self.refreshing=set(); self.errors={}
        self.state=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else dict(defaults={},catalogs={})

    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        tmp=self.path.with_suffix('.tmp');tmp.write_bytes(canonical(self.state));os.replace(tmp,self.path)

    def call(self, actor, request):
        host=self.config['hosts'][actor]
        args=['ssh','-T','-i',self.config['identity_file'],'-o','BatchMode=yes','-o','ConnectTimeout=6','-o','StrictHostKeyChecking=yes',host]
        try:
            p=subprocess.run(args,input=canonical(request),capture_output=True,timeout=210,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            value=json.loads(p.stdout.decode('utf-8-sig'))
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:raise StoreError('MCP_BROKER_UNAVAILABLE') from exc
        if p.returncode or value.get('ok') is not True:
            code=value.get('error','')
            raise StoreError(code if isinstance(code,str) and code.isupper() and len(code)<100 else 'MCP_TARGET_PROBE_FAILED')
        return value['data']

    @staticmethod
    def base_request():
        return dict(checkpoint='yue2_3b_int8_convrot.safetensors',template_sha256=sha(canonical(tpl.template())),
                    node_classes=sorted({n['class_type'] for n in tpl.template().values()}))

    def refresh(self, actor, *, background=True):
        require(actor in self.bindings and actor in self.config['hosts'],'HERMES_ACTOR_NOT_CONFIGURED')
        with self.lock:
            if actor in self.refreshing:return self.public(actor)
            self.refreshing.add(actor);self.errors.pop(actor,None)
        def update():
            try:
                rows=self.call(actor,dict(self.base_request(),action='catalog'))
                require(isinstance(rows,list) and len(rows)<=100,'INVALID_MCP_CATALOG')
                for row in rows:identifier(row['alias'])
                with self.lock:
                    self.state['catalogs'][actor]=dict(checked_at=time.time(),servers=rows)
                    self.save()
            except Exception as e:
                with self.lock:self.errors[actor]=e.code if isinstance(e,StoreError) else 'MCP_DISCOVERY_FAILED'
            finally:
                with self.lock:self.refreshing.discard(actor)
        if background:threading.Thread(target=update,daemon=True).start()
        else:update()
        return self.public(actor)

    def public(self, actor):
        require(actor in self.bindings,'HERMES_ACTOR_NOT_CONFIGURED')
        with self.lock:
            saved=self.state['catalogs'].get(actor,{})
            default=self.state['defaults'].get(actor,self.bindings[actor]['mcp_alias'])
            rows=[{k:deepcopy(v) for k,v in row.items() if k in ('alias','enabled','available','reason','hardware','server_id','capability')} for row in saved.get('servers',[])]
            return dict(actor_id=actor,default_alias=default,servers=rows,checked_at=saved.get('checked_at'),
                        refreshing=actor in self.refreshing,error=self.errors.get(actor))

    def set_default(self, actor, alias):
        self.resolve(actor,alias)
        with self.lock:
            self.state['defaults'][actor]=alias;self.save()
        return self.public(actor)

    def resolve(self, actor, alias=None):
        require(actor in self.bindings,'HERMES_ACTOR_NOT_CONFIGURED')
        with self.lock:
            alias=alias or self.state['defaults'].get(actor,self.bindings[actor]['mcp_alias'])
            identifier(alias)
            row=next((r for r in self.state['catalogs'].get(actor,{}).get('servers',[]) if r['alias']==alias),None)
            require(row is not None,'MCP_DISCOVERY_REQUIRED')
            require(row.get('enabled') and row.get('available'),'MCP_SERVER_UNAVAILABLE')
            info=row['info']
            # Persist only values needed for the trusted probe, never MCP credentials.
            target_info={k:deepcopy(info[k]) for k in ('python','workspace','target_url')}
            return dict(schema_version='hermes-binding/2',actor_id=actor,mcp_alias=alias,
                        config_sha256=row['config_sha256'],server_id=row['server_id'],url=row['url'],hardware=row['hardware'],
                        info=target_info,evidence_basis='hermes_config_and_live_mcp')

    def target(self, binding, action, **extra):
        return self.call(binding['actor_id'],dict(self.base_request(),action=action,alias=binding['mcp_alias'],
            config_sha256=binding['config_sha256'],server_id=binding['server_id'],info=binding['info'],**extra))


_instances={}
_lock=threading.Lock()


def shared(config_path, config, bindings):
    path=Path(config_path).resolve().parent/'mcp-routing/state.json'
    with _lock:
        key=str(path)
        if key not in _instances:_instances[key]=McpRouting(path,config,bindings)
        return _instances[key]
