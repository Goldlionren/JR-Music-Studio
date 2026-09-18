"""Read-only probe and immutable workflow staging on the Comfy Windows host.

Invoked through the operator's existing Hermes SSH transport. Never submits.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import urlopen
import yaml


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def run(args, env=None):
    p = subprocess.run(args, capture_output=True, timeout=90, env=env)
    if p.returncode: raise ValueError('TARGET_COMMAND_FAILED')
    return p.stdout.decode('utf-8-sig').strip()


def execute(packet):
    info = packet['info']
    url = info['target_url']
    with urlopen(url+'/system_stats', timeout=10) as r: stats=json.load(r)
    with urlopen(url+'/object_info', timeout=15) as r: nodes=json.load(r)
    required = packet['node_classes']
    missing = sorted(set(required)-set(nodes))
    choices = nodes.get('CheckpointLoaderSimple',{}).get('input',{}).get('required',{}).get('ckpt_name',[[]])[0]
    if packet['action']=='capability':
        return dict(devices=[d.get('name','') for d in stats.get('devices',[])],
                    missing_nodes=missing, checkpoint_available=packet['checkpoint'] in choices)
    if missing: raise ValueError('TARGET_YUE2_NODES_MISSING')
    if packet['checkpoint'] not in choices: raise ValueError('TARGET_YUE2_MODEL_MISSING')
    root = Path(info['workspace']['path'])
    argv = stats['system']['argv']
    configs=[]
    for i, arg in enumerate(argv):
        if arg=='--extra-model-paths-config':
            for value in argv[i+1:]:
                if value.startswith('--'): break
                configs.append(Path(value))
    defaults=[]
    config_evidence=[]
    for path in configs:
        raw=path.read_bytes(); config_evidence.append(dict(path=str(path),sha256=digest(raw)))
        for entry in (yaml.safe_load(raw) or {}).values():
            if isinstance(entry,dict) and entry.get('is_default') and 'checkpoints' in entry:
                defaults += [(Path(entry['base_path'])/p).resolve() for p in entry['checkpoints'].splitlines() if p]
    roots=defaults or [root/'models/checkpoints']
    candidates=[p/packet['checkpoint'] for p in roots if (p/packet['checkpoint']).is_file()]
    if len(candidates)!=1: raise ValueError('AMBIGUOUS_CHECKPOINT_RESOLUTION')
    ck=candidates[0]; stat=ck.stat()
    identity=dict(path=str(ck.resolve()),size=stat.st_size,mtime_ns=stat.st_mtime_ns,ctime_ns=stat.st_ctime_ns,inode=stat.st_ino)
    home=Path.home()/'.jr-music'; cache=home/'checkpoint-hashes';cache.mkdir(parents=True,exist_ok=True)
    cachefile=cache/(digest(canonical(identity))+'.json')
    if cachefile.exists(): cksha=json.loads(cachefile.read_text())['sha256']
    else:
        h=hashlib.sha256()
        with ck.open('rb') as f:
            while chunk:=f.read(8*1024*1024): h.update(chunk)
        after=ck.stat()
        if (after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_ino)!=(stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino): raise ValueError('CHECKPOINT_CHANGED_DURING_HASH')
        cksha=h.hexdigest();cachefile.write_text(json.dumps(dict(sha256=cksha)))
    implementations={}
    for cls in required:
        module=nodes[cls].get('python_module','')
        if module!='nodes' and not module.startswith('comfy_extras.'): raise ValueError('UNVERIFIED_CUSTOM_NODE')
        implementations[cls]=dict(module=module,source_sha256=digest((root/(module.replace('.','/')+'.py')).read_bytes()))
    cli=str(Path(sys.executable).with_name('comfy.exe'))
    environment=dict(schema_version='render-environment/2',server_id=packet['server_id'],
        template_sha256=packet['template_sha256'], checkpoint=dict(name=ck.name,size=stat.st_size,sha256=cksha,verification='file_sha256',file_identity=identity),
        node_implementations=implementations,model_configuration=config_evidence,
        comfyui=dict(root=str(root),version=stats['system']['comfyui_version']),
        runtime={k:stats['system'].get(k) for k in ('python_version','pytorch_version')},
        devices=[{k:d.get(k) for k in ('name','type','index','vram_total')} for d in stats['devices']],
        official_cli=dict(path=cli,executable_sha256=digest(Path(cli).read_bytes())),
        evidence_basis='jr_invoked_ssh_probe', limitations=['Disk/configuration evidence, not GPU-resident weight attestation.'])
    environment['fingerprint']=digest(canonical(environment))
    result=dict(environment=environment)
    if packet['action']=='prepare':
        if not re.fullmatch('render_[a-f0-9]{32}',packet['render_id']):raise ValueError('INVALID_RENDER_ID')
        folder=home/'renders'/packet['render_id'];folder.mkdir(parents=True,exist_ok=True)
        path=folder/'dispatch.api.json'; raw=canonical(packet['workflow'])
        if path.exists() and path.read_bytes()!=raw:raise ValueError('DISPATCH_FILE_CHANGED')
        if not path.exists():
            with path.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        env={**os.environ,'COMFY_LOCAL_URL':url,'COMFYUI_URL':url}
        checked=json.loads(run([cli,'--where','local','--json','validate','--workflow',str(path)],env))
        if checked.get('ok') is not True or checked.get('data',{}).get('valid') is not True:raise ValueError('PREFLIGHT_FAILED')
        result.update(workflow_path=str(path),workflow_sha256=digest(raw),official_validation=checked)
    return result


if __name__=='__main__':
    print(json.dumps(execute(json.load(sys.stdin)),ensure_ascii=False))
