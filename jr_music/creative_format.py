"""Shared deterministic response boundary. No model, music rewrite or I/O.

Deployed byte-for-byte (after platform newline normalization) to Hermes workers.
Only EOF object closers and explicit upstream spec container moves are repairable.
"""
import copy
import hashlib
import json
import re

VERSION='creative-format/1'
REPAIR_REVISION='eof-closers-2'


def canonical(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()


def sha(value):return hashlib.sha256(value).hexdigest()


def strict_loads(text):
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise ValueError('CREATIVE_JSON_DUPLICATE_KEY')
            value[key]=item
        return value
    def constant(_):raise ValueError('INVALID_CREATIVE_RESPONSE_JSON')
    return json.loads(text,object_pairs_hook=pairs,parse_constant=constant)


def expected_fields(command,lyric_contract=False):
    kind=command['kind']
    if kind=='compose' and command.get('creation_mode')=='style_lyrics':return {'style','lyrics','summary'}
    fields={'reply','plan'} if kind=='plan' else {'abc','lyrics','summary'}
    if kind=='direction':
        fields={'abc','lyrics','style','summary'} if command['scope']['mode']=='song' else {'section_abc','section_lyrics','summary'}
    if lyric_contract and kind!='plan':fields.add('lyric_map')
    if command.get('songcraft',{}).get('selection')=='professional' and kind!='plan':fields.add('production_specs')
    return fields


def check(raw,command,*,lyric_contract=False,finish_reason=None):
    if not isinstance(raw,str) or len(raw.encode())>1_000_000:raise ValueError('INVALID_CREATIVE_RESPONSE_JSON')
    text=raw.strip();operations=[]
    if text.startswith('API call failed'):
        raise ValueError('HERMES_PROVIDER_TIMEOUT' if 'timed out' in text else 'HERMES_PROCESS_FAILED')
    text=re.sub(r'^\s*⟳ compacting context…\s*\n','',text,flags=re.M).strip()
    if text.startswith('I stopped retrying read_file because it hit the tool-call guardrail (same_tool_failure_halt)'):
        raise ValueError('HERMES_FILE_READ_HALTED')
    fenced=re.fullmatch(r'```(?:json)?\s*\n(.*)\n```',text,re.S)
    if fenced:text=fenced[1]
    if finish_reason in ('length','max_tokens','content_filter'):raise ValueError('CREATIVE_RESPONSE_TRUNCATED')
    try:value=strict_loads(text)
    except json.JSONDecodeError as error:
        # Only discard surplus closers after a complete, strictly parsed object.
        # Prose, another document, duplicate keys and incomplete values stay errors.
        tail=text[error.pos:].strip()
        if finish_reason=='stop' and error.msg=='Extra data' and re.fullmatch(r'}{1,4}',tail):
            value=strict_loads(text[:error.pos])
            if not isinstance(value,dict):raise ValueError('INVALID_CREATIVE_RESPONSE_JSON') from error
            operations.append(dict(op='remove_eof_object_closers',count=len(tail)))
        # Only a complete final scalar/object followed by omitted object closers.
        # Never close strings/arrays, fix a comma, guess a value or remove prose.
        elif finish_reason!='stop' or error.pos!=len(text) or not text.endswith('}'):
            raise ValueError('INVALID_CREATIVE_RESPONSE_JSON') from error
        else:
            for count in range(1,5):
                try:value=strict_loads(text+'}'*count)
                except json.JSONDecodeError:continue
                operations.append(dict(op='append_eof_object_closers',count=count));break
            else:raise ValueError('INVALID_CREATIVE_RESPONSE_JSON') from error
    before=copy.deepcopy(value)
    expected=expected_fields(command,lyric_contract)
    if not isinstance(value,dict) or set(value)!=expected:raise ValueError('INVALID_CREATIVE_RESPONSE_FIELDS')
    if 'production_specs' in expected:
        spec=value['production_specs']
        if not isinstance(spec,dict):raise ValueError('INVALID_PRODUCTION_SPECS')
        arr=spec.get('arr_spec')
        if not isinstance(arr,dict):raise ValueError('INVALID_PRODUCTION_SPECS')
        def move(source,key,target,src_path,dst_path):
            if key not in source:return
            if key in target:raise ValueError('CREATIVE_FORMAT_AMBIGUOUS')
            target[key]=source.pop(key)
            operations.append(dict(op='move_container',source=src_path+'/'+key,target=dst_path+'/'+key))
        move(arr,'lyr_spec',spec,'/production_specs/arr_spec','/production_specs')
        arrangement=arr.get('arrangement')
        if isinstance(arrangement,dict):
            for key in ('vocal','mix_intent','render','checks'):
                move(arrangement,key,arr,'/production_specs/arr_spec/arrangement','/production_specs/arr_spec')
        if set(spec)!={'arr_spec','lyr_spec'}:raise ValueError('INVALID_PRODUCTION_SPECS')
        for obj,keys in ((arr,{'meta','intent','material','form','arrangement','vocal','checks'}),
                         (spec['lyr_spec'],{'meta','intent','prosody','lyrics','checks'})):
            if not isinstance(obj,dict) or not keys<=obj.keys():raise ValueError('INVALID_PRODUCTION_SPECS')
            if not isinstance(obj['checks'],dict) or not isinstance(obj['checks'].get('self_audit'),list):raise ValueError('INVALID_PRODUCTION_SPECS')
    if operations and finish_reason!='stop':raise ValueError('CREATIVE_FORMAT_COMPLETION_UNVERIFIED')
    # All non-spec creative values must be bit-for-bit equivalent as JSON values.
    protected={k:v for k,v in value.items() if k!='production_specs'}
    if protected!={k:v for k,v in before.items() if k!='production_specs'}:raise ValueError('CREATIVE_FORMAT_CONTENT_CHANGED')
    for key in expected-{'production_specs','lyric_map','plan'}:
        if not isinstance(value[key],str) or not value[key].strip():raise ValueError('INVALID_CREATIVE_RESPONSE_FIELDS')
    if 'lyric_map' in expected and not isinstance(value['lyric_map'],list):raise ValueError('INVALID_CREATIVE_RESPONSE_FIELDS')
    if 'plan' in expected and not isinstance(value['plan'],dict):raise ValueError('INVALID_CREATIVE_RESPONSE_FIELDS')
    report=dict(schema_version=VERSION,status='repaired' if operations else 'valid',
        command_id=command.get('command_id'),raw_sha256=sha(raw.encode()),result_sha256=sha(canonical(value)),
        protected_content_sha256=sha(canonical(protected)),finish_reason=finish_reason,
        lyric_contract=bool(lyric_contract),operations=operations,musical_content_changed=False)
    return value,report
