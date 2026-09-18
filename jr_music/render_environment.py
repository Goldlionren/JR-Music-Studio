"""Read-only local environment evidence; cached checkpoint byte identity.

This records disk/configuration evidence, not an attestation of GPU-resident
weights. Stable fingerprints exclude RAM availability, timestamps and cache hits.
"""
import json
import os
from pathlib import Path
import subprocess
import uuid
from urllib.request import urlopen
from .cas import CAS
from .store import canonical, sha, require
from . import render_template as tpl


def file_identity(path):
    st=Path(path).stat()
    return dict(path=str(Path(path).resolve()), device=st.st_dev, inode=st.st_ino,
                size=st.st_size, mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns)


def cached_checkpoint(path, cache_root):
    path=Path(path).resolve()
    before=file_identity(path)
    cache_root=Path(cache_root); cache_root.mkdir(parents=True,exist_ok=True)
    target=cache_root/(sha(canonical(before))+'.json')
    if target.exists():
        try:
            saved=json.loads(target.read_text(encoding='utf-8'))
            require(saved['identity']==before and saved['record_hash']==sha(canonical(saved['checkpoint'])), 'CHECKPOINT_CACHE_INVALID')
            require(file_identity(path)==before,'CHECKPOINT_CHANGED')
            return saved['checkpoint']
        except (ValueError, KeyError):
            pass
    digest,size=CAS.fingerprint(path)
    require(file_identity(path)==before,'CHECKPOINT_CHANGED_DURING_HASH')
    checkpoint=dict(name=path.name,path=str(path),size=size,sha256=digest,verification='file_sha256',file_identity=before)
    saved=dict(identity=before,checkpoint=checkpoint,record_hash=sha(canonical(checkpoint)))
    temporary=target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('xb') as f:
            f.write(canonical(saved)); f.flush(); os.fsync(f.fileno())
        os.replace(temporary,target)
    finally:
        temporary.unlink(missing_ok=True)
    return checkpoint


def command(args,cwd=None):
    result=subprocess.run([str(x) for x in args],cwd=cwd,capture_output=True,timeout=30,check=False)
    require(result.returncode==0,'ENVIRONMENT_PROBE_FAILED')
    return result.stdout.decode('utf-8',errors='replace').strip()


def collect_environment(comfy_root,checkpoint,cache_root,ffmpeg,ffprobe,cli,model_config):
    root=Path(comfy_root).resolve()
    with urlopen('http://127.0.0.1:8188/system_stats',timeout=15) as r: stats=json.load(r)
    require(any('RTX 3060' in d.get('name','') for d in stats['devices']),'SERVER_HARDWARE_MISMATCH')
    with urlopen('http://127.0.0.1:8188/object_info',timeout=20) as r: nodes=json.load(r)
    checkpoint=cached_checkpoint(checkpoint,cache_root)
    config=Path(model_config)
    require(str(config) in stats['system']['argv'],'MODEL_CONFIG_NOT_ACTIVE')
    raw=config.read_bytes()
    # The local operator explicitly supplies the active config; verify its
    # default checkpoints root before pinning a same-named file there.
    import sys
    sys.path.insert(0,str(Path(cli).parents[1]/'Lib/site-packages'))
    import yaml
    parsed=yaml.safe_load(raw)
    roots=[]
    for entry in parsed.values():
        if isinstance(entry,dict) and entry.get('is_default') and 'checkpoints' in entry:
            roots += [(Path(entry['base_path'])/p).resolve() for p in entry['checkpoints'].splitlines() if p]
    require(len(roots)==1 and Path(checkpoint['path']).parent==roots[0], 'AMBIGUOUS_CHECKPOINT_RESOLUTION')
    modules={}
    for node in tpl.template().values():
        cls=node['class_type']; module=nodes[cls].get('python_module')
        require(isinstance(module,str) and (module=='nodes' or module.startswith('comfy_extras.')),'UNVERIFIED_CUSTOM_NODE')
        path=root/(module.replace('.', '/')+'.py')
        modules[cls]=dict(module=module,source_sha256=sha(path.read_bytes()),origin='ComfyUI core repository')
    git=['git','-c','safe.directory='+str(root),'-C',str(root)]
    commit=command(git+['rev-parse','HEAD'])
    dirty=command(git+['status','--porcelain','--untracked-files=no'])
    python=root/'.venv/Scripts/python.exe'
    runtime=json.loads(command([python,'-c','import json,torch,sys; print(json.dumps(dict(python=sys.version,torch=torch.__version__,cuda_build=torch.version.cuda,cudnn=torch.backends.cudnn.version())))']))
    require(runtime['torch']==stats['system']['pytorch_version'] and runtime['python']==stats['system']['python_version'],'RUNTIME_VERSION_MISMATCH')
    runtime['probe_interpreter']=str(python)
    runtime['identity_basis']='installation .venv; Python/Torch versions cross-checked against live server; process executable path unavailable'
    result=dict(schema_version='render-environment/1',server_id='local_3060',
        comfyui=dict(root=str(root),version=stats['system']['comfyui_version'],commit=commit,tracked_dirty=bool(dirty),tracked_status_sha256=sha(dirty.encode())),
        checkpoint=checkpoint,node_implementations=modules,custom_nodes_used=[],
        model_configuration=dict(path=str(config),sha256=sha(raw),checkpoint_resolution='single active default checkpoint root'),
        template_id=tpl.TEMPLATE_ID,template_sha256=sha(canonical(tpl.template())),runtime=runtime,
        ffmpeg_version=command([ffmpeg,'-version']).splitlines()[0],ffprobe_version=command([ffprobe,'-version']).splitlines()[0],
        official_cli=dict(path=str(Path(cli).resolve()),executable_sha256=sha(Path(cli).read_bytes()),
            version=command([Path(cli).parent/'python.exe','-c','import importlib.metadata; print(importlib.metadata.version("comfy-cli"))'])),
        devices=[{k:d.get(k) for k in ('name','type','index','vram_total')} for d in stats['devices']],
        limitations=['Disk and active-configuration evidence; not cryptographic remote/GPU weight attestation.',
                     'CUDA build is the PyTorch build version, not a claim about an independently measured driver runtime.',
                     'Checkpoint cache invalidates on resolved path, file identity, size, mtime and ctime; trusted local cache.',
                     'Per-candidate executable workflow SHA-256 is bound separately in the render job/manifest.'])
    result['fingerprint']=sha(canonical(result))
    return result


def validate_environment(value,job):
    from .music_contracts import validate
    remote=job['schema_version']=='render/4'
    validate('render-environment','environment',value,version=2 if remote else 1)
    require(isinstance(value,dict) and value.get('schema_version')==('render-environment/2' if remote else 'render-environment/1'),'INVALID_ENVIRONMENT')
    body={k:v for k,v in value.items() if k!='fingerprint'}
    require(value.get('fingerprint')==sha(canonical(body)),'ENVIRONMENT_HASH_MISMATCH')
    require(value.get('server_id')==job['server']['server_id'] and value.get('template_sha256')==job['template_sha256'],'ENVIRONMENT_CONTEXT_MISMATCH')
    ck=value.get('checkpoint',{})
    require(ck.get('name')==job['parameters']['checkpoint'] and ck.get('verification')=='file_sha256'
            and type(ck.get('size')) is int and ck['size']>0 and isinstance(ck.get('sha256'),str)
            and len(ck['sha256'])==64 and all(c in '0123456789abcdef' for c in ck['sha256']),'CHECKPOINT_FINGERPRINT_REQUIRED')
