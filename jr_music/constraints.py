"""Deterministic musical invariants, independent of the model's summary."""
from .score import parse_abc
from fractions import Fraction
from .store import require, canonical, sha

FIELDS = ('lyrics','melody','rhythm','chords','structure','tempo','key','style')


def validate_locks(value):
    require(isinstance(value,list) and len(set(value))==len(value) and all(v in FIELDS for v in value),'INVALID_PRESERVE_FIELDS')
    return sorted(value)


def signatures(abc,brief):
    score=parse_abc(abc.encode())
    require(all(d['code']=='BAR_DURATION_MISMATCH' for d in score['parse_diagnostics']),'LOCK_REQUIRES_SUPPORTED_SCORE')
    chords=[]
    for token in (t for t in score['tokens'] if t['kind']=='chord_symbol'):
        anchor=next((e for e in score['events'] if e['byte_start']>=token['byte_end']),None)
        beat=sum((Fraction(**e['duration']) for e in score['events'] if anchor and e['bar_id']==anchor['bar_id'] and e['byte_start']<anchor['byte_start']),Fraction())
        chords.append((anchor['bar_id'] if anchor else None,str(beat),token['text']))
    return dict(lyrics=brief['lyrics'],style=brief['style'],tempo=score['headers'].get('Q'),key=score['headers'].get('K'),
        melody=[(e['voice_id'],e['midi_pitch']) for e in score['events'] if e['kind']=='note'],
        rhythm=dict(meter=score['headers'].get('M'),unit=score['headers'].get('L'),
            events=[(e['voice_id'],e['bar_id'],e['kind'],e['duration'],e.get('tie_in'),e.get('tie_out')) for e in score['events']]),
        chords=chords,
        structure=[(s['suggested_label'],s['bars_per_voice']) for s in score['sections']])


def enforce(source,abc,brief,locks):
    validate_locks(locks)
    from .score_lyrics import validate_abc_lyrics
    validate_abc_lyrics(abc,brief['lyrics'])
    if not locks:return dict(checked=[],passed=True)
    # Text locks work even on a source whose musical notation is not yet supported.
    for field in ('lyrics','style'):
        if field in locks:
            require(brief[field].strip()==source['brief'][field].strip(),field.upper()+'_LOCK_VIOLATION')
            brief[field]=source['brief'][field]
    musical=[f for f in locks if f not in ('lyrics','style')]
    before=signatures(source['abc'],source['brief']) if musical else source['brief']
    after=signatures(abc,brief) if musical else brief
    for field in locks:
        require(before[field]==after[field],field.upper()+'_LOCK_VIOLATION')
    return dict(checked=locks,passed=True,source_checks_sha256=sha(canonical({f:before[f] for f in locks})),
        result_checks_sha256=sha(canonical({f:after[f] for f in locks})))
