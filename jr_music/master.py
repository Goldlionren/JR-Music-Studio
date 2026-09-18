"""Pinned, feature-oriented knowledge bundles; no agent identity or authority."""
import json
from pathlib import Path
from .music_contracts import ROOT, validate
from .store import canonical, sha, require


class MasterLibrary:
    def __init__(self, root=None):
        self.root = Path(root or ROOT / 'masters').resolve()

    def load(self, master_id, version):
        registry = json.loads((self.root / 'registry.json').read_text(encoding='utf-8'))
        matches = [r for r in registry if r['master_id'] == master_id and r['version'] == version]
        require(len(matches) == 1, 'MASTER_VERSION_NOT_FOUND')
        pin = matches[0]
        require(set(pin['hashes']) in ({'profile', 'provenance', 'critic-rubric'},
            {'profile', 'provenance', 'critic-rubric', 'refinement'}), 'INVALID_MASTER_PARTS')
        folder = (self.root / pin['path']).resolve()
        require(folder.is_relative_to(self.root), 'INVALID_MASTER_PATH')
        bundle = {}
        for name, family in [('profile', 'music-master'), ('provenance', 'music-master'), ('critic-rubric', 'music-critic')]:
            value = json.loads((folder / (name + '.json')).read_text(encoding='utf-8'))
            validate(family, name, value)
            require(sha(canonical(value)) == pin['hashes'][name], 'MASTER_HASH_MISMATCH')
            bundle[name] = value
        require(bundle['profile']['master_id'] == master_id and bundle['profile']['version'] == version, 'MASTER_ID_MISMATCH')
        sources = {s['source_id'] for s in bundle['provenance']['sources']}
        for features in bundle['profile']['dimensions'].values():
            for feature in features.values():
                require(set(feature['source_ids']) <= sources, 'UNKNOWN_EVIDENCE_SOURCE')
                if feature['evidence_level'] in ('observed', 'statistically_observed', 'source_attributed'):
                    require(bool(feature['source_ids']) and bool(feature['evidence_note']), 'EVIDENCE_REQUIRED')
                if feature['status'] == 'insufficient_evidence':
                    require(feature['guidance'] is None, 'UNSUPPORTED_GUIDANCE')
        bundle['fingerprint'] = sha(canonical(bundle))
        if 'refinement' in pin['hashes']:
            value = json.loads((folder / 'refinement.json').read_text(encoding='utf-8'))
            validate('master-refinement', 'refinement', value)
            require(sha(canonical(value)) == pin['hashes']['refinement'], 'MASTER_HASH_MISMATCH')
            require(value['master_id'] == master_id and value['version'] == version, 'MASTER_ID_MISMATCH')
            ids = {s['source_id'] for s in value['evidence']}
            require(len(ids) == len(value['evidence']), 'DUPLICATE_EVIDENCE_SOURCE')
            for control in value['controls']:
                require(set(control['source_ids']) <= ids, 'UNKNOWN_EVIDENCE_SOURCE')
                if control['origin'] == 'producer_preference':
                    require(bool(control['source_ids']) and all(next(s for s in value['evidence'] if s['source_id'] == i)['kind'] == 'producer_statement' for i in control['source_ids']), 'EVIDENCE_REQUIRED')
            del bundle['fingerprint']
            bundle['refinement'] = value
            bundle['fingerprint'] = sha(canonical(bundle))
        return bundle


def guidance(brief, bundle=None, strength=0):
    require(type(strength) in (int, float) and 0 <= strength <= 1, 'INVALID_MASTER_STRENGTH')
    require(bundle is not None or strength == 0, 'MASTER_REQUIRED')
    instructions = []
    if bundle and strength:
        for dimension, features in sorted(bundle['profile']['dimensions'].items()):
            for name, feature in sorted(features.items()):
                if feature['status'] == 'available' and feature['guidance']:
                    instructions.append(dict(feature=dimension + '.' + name, instruction=feature['guidance'],
                        evidence_level=feature['evidence_level'], confidence=feature['confidence']))
    result = dict(schema_version='composer-guidance/1', brief=brief, brief_sha256=sha(canonical(brief)),
        master=None if bundle is None else dict(master_id=bundle['profile']['master_id'], version=bundle['profile']['version'],
                                               fingerprint=bundle['fingerprint']),
        strength=strength, priority='none' if not strength else 'advisory' if strength < .5 else 'preferred',
        strength_semantics='heuristic instruction priority; not a calibrated style percentage',
        instructions=instructions, constraints=['Follow the creative brief before Master preferences.',
            'Create original lyrics and melody; do not reproduce source songs.',
            'Master is advice, not authorization to edit protected regions or select HEAD.'])
    return validate('composer-guidance', 'guidance', result)


def channel_guidance(brief, bundle=None, *, strengths):
    """Independent instruction priority by domain; never calibrated quality scores."""
    require(isinstance(strengths, dict) and set(strengths) == {'lyrics', 'melody', 'other'}, 'INVALID_CHANNEL_STRENGTHS')
    require(all(type(s) in (int, float) and 0 <= s <= 1 for s in strengths.values()), 'INVALID_CHANNEL_STRENGTHS')
    require(bundle is not None or not any(strengths.values()), 'MASTER_REQUIRED')
    result = guidance(brief, bundle, max(strengths.values()))
    result['schema_version'] = 'composer-guidance/2'
    result['channel_strengths'] = strengths
    result['instructions'] = [dict(i, channel=(i['feature'].split('.')[0] if i['feature'].split('.')[0] in ('lyrics', 'melody') else 'other')) for i in result['instructions']]
    result['instructions'] = [i for i in result['instructions'] if strengths[i['channel']] > 0]
    result['refinement_controls'] = []
    if bundle and 'refinement' in bundle:
        for c in bundle['refinement']['controls']:
            if strengths[c['channel']] > 0:
                result['refinement_controls'].append({k: c[k] for k in ('control_id', 'channel', 'origin', 'guidance')})
    result['strength_semantics'] = 'Per-channel heuristic priority; 0 disables that channel. Global strength is the maximum, not a blend. No calibrated style or quality score.'
    return validate('composer-guidance', 'guidance', result, 2)
