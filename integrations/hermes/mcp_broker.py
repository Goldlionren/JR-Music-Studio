"""JR-invoked broker: reads Hermes MCP configuration, probes and stages only.

Run under a dedicated forced-command SSH key. No submit/run/stop/update action.
"""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit,urlunsplit
import yaml

ROOT=Path(__file__).resolve().parent


def config():
    return yaml.safe_load((Path.home()/'.hermes/config.yaml').read_text()).get('mcp_servers',{})


def signature(server):
    return hashlib.sha256(json.dumps(server,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def ssh_prefix(server):
    if server.get('command')!='ssh': raise ValueError('MCP_STAGING_TRANSPORT_UNSUPPORTED')
    args=server.get('args',[])
    index=next((i for i,a in enumerate(args) if re.fullmatch(r'[\w.-]+@[\w.-]+',a)),None)
    if index is None:raise ValueError('MCP_STAGING_TRANSPORT_UNSUPPORTED')
    return ['ssh','-o','ConnectTimeout=6',*args[:index+1]],args[index].split('@')[1]


async def info(server):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    async with stdio_client(StdioServerParameters(command=server['command'],args=server.get('args',[]),env=server.get('env'))) as (r,w):
        async with ClientSession(r,w) as session:
            await session.initialize()
            ts=await session.list_tools()
            names={t.name for t in ts.tools}
            if not {'run_workflow','server_info'}<=names:raise ValueError('MCP_WORKFLOW_TOOL_UNAVAILABLE')
            reply=await session.call_tool('server_info',{})
            if reply.is_error:raise ValueError('MCP_PROBE_FAILED')
            return next(json.loads(c.text) for c in reply.content if c.type=='text')


def target(server,packet):
    prefix,_=ssh_prefix(server)
    source=(ROOT/'mcp_target.py').read_text()
    python=packet['info']['python']['executable']
    if not re.fullmatch(r'[A-Za-z]:[\\/][^\r\n"`$]+python.exe',python):raise ValueError('INVALID_TARGET_PYTHON')
    # Keep the SSH command tiny: source + request travel via stdin, not argv.
    boot="import json,sys;p=json.load(sys.stdin);exec(compile(p['source'],'<jr-mcp-probe>','exec'),{'__name__':'jr_probe','__file__':'jr_probe'});"
    boot="import json,sys;p=json.load(sys.stdin);scope={'__name__':'jr_probe'};exec(compile(p['source'],'<jr-mcp-probe>','exec'),scope);print(json.dumps(scope['execute'](p['request']),ensure_ascii=False))"
    ps="& '"+python.replace("'","''")+"' -X utf8 -c '"+boot.replace("'","''")+"'"
    cmd=prefix+['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(ps.encode('utf-16-le')).decode()]
    p=subprocess.run(cmd,input=json.dumps(dict(source=source,request=packet)).encode(),capture_output=True,timeout=180)
    if p.returncode:raise ValueError('TARGET_PROBE_FAILED: '+p.stderr.decode(errors='replace')[-1500:])
    return json.loads(p.stdout.decode('utf-8-sig'))


def describe(alias,server,request):
    row=dict(alias=alias,config_sha256=signature(server),enabled=server.get('enabled',True),available=False)
    if not row['enabled']:return dict(row,reason='MCP_DISABLED')
    policy=server.get('tools') or {}
    if 'run_workflow' in policy.get('exclude',[]) or ('include' in policy and 'run_workflow' not in policy['include']):return dict(row,reason='MCP_WORKFLOW_TOOL_EXCLUDED')
    try:
        prefix,host=ssh_prefix(server)
        # TCP preflight avoids opening a long-lived MCP process for offline hosts.
        import socket
        with socket.create_connection((host,22),timeout=3):pass
        details=asyncio.run(asyncio.wait_for(info(server),45))
        url=details.get('server',{}).get('url')
        target_info=details.get('comfy_target') or {}
        if target_info.get('host'):
            url='http://'+target_info['host']+':'+str(target_info.get('port',8188))
        parsed=urlsplit(url or '')
        if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost',host) or not parsed.port:raise ValueError('MCP_TARGET_TOPOLOGY_UNSUPPORTED')
        details['target_url']=url
        row.update(info=details,url=urlunsplit(('http',host+':'+str(parsed.port),'','','')),
                   server_id='comfy_'+host.replace('.','_')+'_'+str(parsed.port),host=host,
                   hardware=details.get('hardware',{}).get('gpu',{}).get('model','ComfyUI'))
        probe=target(server,dict(request,action='capability',info=details))
        row.update(available=not probe['missing_nodes'] and probe['checkpoint_available'],capability=probe)
        row['reason']='' if row['available'] else ('TARGET_YUE2_NODES_MISSING' if probe['missing_nodes'] else 'TARGET_YUE2_MODEL_MISSING')
    except Exception as e:
        # Never expose transport argv, environment values or credentials.
        row['reason']=str(e) if isinstance(e,ValueError) and re.fullmatch('[A-Z_]+',str(e)) else 'MCP_OFFLINE_OR_PROBE_FAILED'
    return row


def execute(request):
    servers=config()
    if request['action']=='catalog':
        from concurrent.futures import ThreadPoolExecutor
        selected=[(k,v) for k,v in servers.items() if re.fullmatch('[a-z][a-z0-9_-]{0,95}',k)]
        with ThreadPoolExecutor(max_workers=4) as pool:return list(pool.map(lambda x:describe(*x,request),selected))
    if request['action'] not in ('prepare','probe'):raise ValueError('BROKER_ACTION_DENIED')
    server=servers[request['alias']]
    if signature(server)!=request['config_sha256']:raise ValueError('MCP_CONFIGURATION_CHANGED')
    return target(server,request)


if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(2*1024*1024+1)
        if len(raw)>2*1024*1024:raise ValueError('BROKER_REQUEST_TOO_LARGE')
        print(json.dumps(dict(ok=True,data=execute(json.loads(raw))),ensure_ascii=False))
    except Exception as e:
        print(json.dumps(dict(ok=False,error=str(e)[:2000])))
        sys.exit(1)
