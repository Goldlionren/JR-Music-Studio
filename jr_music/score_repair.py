"""Explicit, lossless-outside-target-bars meter normalization for generated scores."""
from fractions import Fraction
from .score import parse_abc, EXT_NOTE as NOTE
from .store import require


def normalize_meter(abc):
    raw = abc.encode('utf-8')
    score = parse_abc(raw)
    require(all(d['code'] == 'BAR_DURATION_MISMATCH' for d in score['parse_diagnostics']), 'SCORE_REQUIRES_NOTATION_REPAIR')
    if score['parse_diagnostics']:
        require(not any(t['kind'] in ('tuplet','broken_rhythm') for t in score['tokens']),'SCORE_REQUIRES_NOTATION_REPAIR')
    meter = Fraction(score['headers']['M'])
    unit = Fraction(score['headers']['L'])
    changes, patches = [], []
    for bar in score['bars']:
        duration = Fraction(**bar['duration'])
        if duration == meter:
            continue
        require(duration > 0 and not bar['shared_rest_group'], 'SCORE_REQUIRES_NOTATION_REPAIR')
        factor = meter / duration
        changes.append(dict(bar_id=bar['bar_id'], voice_id=bar['voice_id'], ordinal=bar['ordinal'],
            before_beats=str(duration * 4), after_beats=str(meter * 4), duration_scale=str(factor)))
        for event in (e for e in score['events'] if e['bar_id'] == bar['bar_id']):
            old = raw[event['byte_start']:event['byte_end']].decode()
            match = NOTE.fullmatch(old)
            require(match is not None, 'SCORE_REQUIRES_NOTATION_REPAIR')
            letter, octave, _, tie = match.groups()
            length = Fraction(**event['duration']) * factor / unit
            suffix = '' if length == 1 else str(length)
            patches.append((event['byte_start'], event['byte_end'], (letter + octave + suffix + tie).encode()))
    for start, end, replacement in sorted(patches, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    require(parse_abc(raw)['status'] == 'supported', 'SCORE_REQUIRES_NOTATION_REPAIR')
    return raw.decode(), changes


def repair_preview(abc):
    try:
        _, changes = normalize_meter(abc)
        return dict(available=bool(changes), changes=changes)
    except ValueError:
        return dict(available=False, changes=[])


def repair_revision(store, project_id, revision_id, expected_snapshot_sha256, *, actor, key):
    source = store.get_revision(project_id, revision_id)
    require(source['revision']['snapshot_sha256'] == expected_snapshot_sha256, 'STALE_BASE')
    abc, changes = normalize_meter(source['snapshot']['abc'])
    require(changes, 'NO_MUSICAL_CHANGE')
    summary = '拍数修正：' + '；'.join(f"{c['voice_id']} 第 {c['ordinal']} 小节 {c['before_beats']}→{c['after_beats']} 拍，时值等比例乘 {c['duration_scale']}" for c in changes)
    summary += '。音高与歌词保留；仅修改谱面，原音频不绑定到此新版本。'
    result = store.add_revision(project_id, abc, source['snapshot']['brief'], summary[:2000],
        parent_revision_id=revision_id, expected_parent_snapshot_sha256=expected_snapshot_sha256, actor=actor, key=key)
    def record(db):
        store._event(db, project_id, 'score_meter_repair', actor, dict(source_revision_id=revision_id,
            result_revision_id=result['revision_id'], changes=changes, source_snapshot_sha256=expected_snapshot_sha256))
        return result
    return store._operation(actor, key+'_repair_record', dict(op='score_meter_repair', project_id=project_id, revision_id=result['revision_id']), record)
