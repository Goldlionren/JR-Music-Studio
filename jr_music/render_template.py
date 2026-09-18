"""Allowlisted YuE2 template validation; graphs are built by official composer.

Validation compares every executable node/input to the reviewed quality graph,
except declared blueprint parameters which must match the frozen revision.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
from .store import canonical, sha, require

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ID = 'yue2-fixed-score/1'
REFERENCE = ROOT / 'jr_music/resources/yue2-template.json'
SLOTS = {'checkpoint': ('102', 'ckpt_name'), 'abc': ('157', 'value'), 'style': ('166', 'value'),
         'lyrics': ('167', 'value'), 'seed': ('165', 'seed'), 'max_duration': ('158', 'max_duration'),
         'filename_prefix': ('152', 'filename_prefix')}


def graph_only(workflow):
    require(isinstance(workflow, dict) and 0 < len(workflow) < 100, 'INVALID_WORKFLOW')
    graph = {}
    for key, node in workflow.items():
        if key == '_meta':
            continue
        require(isinstance(key, str) and key.isdigit() and isinstance(node, dict) and
                set(node) <= {'inputs', 'class_type', '_meta'} and 'inputs' in node and 'class_type' in node,
                'INVALID_WORKFLOW')
        graph[key] = dict(class_type=node['class_type'], inputs=node['inputs'])
    return deepcopy(graph)


def template():
    reference = graph_only(json.loads(REFERENCE.read_text(encoding='utf-8')))
    for node, field in SLOTS.values():
        reference[node]['inputs'][field] = '<bound-parameter>'
    return reference


def remote_match(frozen,observed,*,allow_float_coercion=False):
    """Comfy's FLOAT input validation may rewrite int duration to equal float.

    Preserve both graph hashes. No global JSON numeric normalization: booleans,
    sampler fields, seeds and every other executable field remain exact.
    """
    original=graph_only(observed); normalized=deepcopy(original); changes=[]
    frozen_hash=sha(canonical(frozen)); remote_hash=sha(canonical(original))
    if allow_float_coercion and frozen_hash!=remote_hash:
        before=frozen.get('158',{}).get('inputs',{}).get('max_duration')
        after=normalized.get('158',{}).get('inputs',{}).get('max_duration')
        if type(before) is int and type(after) is float and after==before:
            normalized['158']['inputs']['max_duration']=before
            changes=[dict(path='/158/inputs/max_duration',frozen=before,observed=after,conversion='integer_to_equal_float')]
    require(sha(canonical(normalized))==frozen_hash,'REMOTE_WORKFLOW_MISMATCH')
    return dict(verifier='comfy-duration-coercion/1',frozen_workflow_sha256=frozen_hash,
                observed_workflow_sha256=remote_hash,exact_match=frozen_hash==remote_hash,allowed_coercions=changes)


def parameters(snapshot, prefix):
    brief = snapshot['brief']
    require(any('|' in line and not re.match(r'^[ \t]*(%|[A-Za-z]:)', line)
                for line in snapshot['abc'].splitlines()), 'ABC_MUSIC_BODY_REQUIRED')
    require(0 <= brief['seed'] <= 9007199254740991, 'MCP_SEED_PRECISION_LIMIT')
    require(brief['checkpoint'] == 'yue2_3b_int8_convrot.safetensors', 'MODEL_NOT_APPROVED')
    require(re.fullmatch(r'audio/JRMusic/[a-zA-Z0-9_-]+', prefix), 'INVALID_OUTPUT_PREFIX')
    return dict(**brief, abc=snapshot['abc'], filename_prefix=prefix)


def blueprint(params):
    return dict(pipeline=[dict(fragment='yue2_model', alias='model', params={'checkpoint': params['checkpoint']}),
        dict(fragment='yue2_render', alias='render', inputs={'clip': '$model.clip', 'model': '$model.model', 'vae': '$model.vae'},
             params={k: v for k, v in params.items() if k != 'checkpoint'})])


def validate(workflow, params, expected_template_sha256):
    expected = template()
    require(sha(canonical(expected)) == expected_template_sha256, 'TEMPLATE_CHANGED')
    graph = graph_only(workflow)
    masked = deepcopy(graph)
    for key, (node, field) in SLOTS.items():
        require(node in graph and isinstance(graph[node]['inputs'], dict) and
                canonical(graph[node]['inputs'].get(field)) == canonical(params[key]), 'WORKFLOW_INPUT_MISMATCH')
        masked[node]['inputs'][field] = '<bound-parameter>'
    # Python equality equates True with 1 and 32.0 with 32; executable JSON
    # constants must retain their actual serialized types as well as values.
    require(canonical(masked) == canonical(expected), 'WORKFLOW_TEMPLATE_MISMATCH')
    return graph
