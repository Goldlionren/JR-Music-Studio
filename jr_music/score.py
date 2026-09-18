"""Lossless, deliberately bounded YuE2 ABC reader and pitch-only patch validator.

Source bytes remain authoritative. IDs are topology addresses, qualified by source
hash + representation ID at the API boundary. No insertion/deletion is supported.
"""
from collections import defaultdict
from fractions import Fraction
import hashlib
import re

PARSER_VERSION = 'yue2-subset/1.3.0'
ADDRESS_VERSION = 'topology/1'
NOTE = re.compile(r"([A-Ga-gz])([,']*)((?:\d+(?:/\d+|/+)?|/\d+|/+)?)?(-?)")
EXT_NOTE = re.compile(r"((?:\^{1,2}|_{1,2}|=)?[A-Ga-gz])([,']*)((?:\d+(?:/\d+|/+)?|/\d+|/+)?)?(-?)")
PITCH = re.compile(r"(?:\^{1,2}|_{1,2}|=)?[A-Ga-g][,']*")
SECTION = re.compile(r'%\s*(intro|verse|pre[-_ ]?chorus|chorus|bridge|outro)(?:[ _-]*\d+)?\s*', re.I)
SECTION_V13 = re.compile(r'%\s*(intro|verse|pre[-_ ]?chorus|chorus|bridge|outro)(?:[ _-]*\d+)?(?:\s+bars?\s+\d+\s*[-–]\s*\d+)?\s*', re.I)
KEYS = {'C': (), 'Am': (), 'A': ('F', 'C', 'G')}
PARSER_VERSIONS = ('yue2-subset/1.0.0', 'yue2-subset/1.1.0', 'yue2-subset/1.2.0', PARSER_VERSION)
LEGACY_SECTION = re.compile(r'%\s*(intro|verse|pre_chorus|chorus|bridge|outro)\s*', re.I)
KEY_FIFTHS = dict(zip('Cb Gb Db Ab Eb Bb F C G D A E B F# C#'.split(), range(-7,8)))
KEY_FIFTHS.update(zip('Abm Ebm Bbm Fm Cm Gm Dm Am Em Bm F#m C#m G#m D#m A#m'.split(),range(-7,8)))


class ScoreError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def ratio(value):
    return dict(numerator=value.numerator, denominator=value.denominator)


def note_length(raw):
    if not raw:
        return Fraction(1)
    if '/' not in raw:
        return Fraction(int(raw))
    if re.fullmatch(r'\d*/+', raw):
        return Fraction(int(raw.split('/')[0] or 1), 2 ** raw.count('/'))
    numerator, denominator = raw.split('/')
    return Fraction(int(numerator or 1), int(denominator))


def parse_abc(raw, *, version=PARSER_VERSION):
    if version not in PARSER_VERSIONS:
        raise ScoreError('PARSER_VERSION_UNAVAILABLE')
    section_pattern = LEGACY_SECTION if version == 'yue2-subset/1.0.0' else SECTION_V13 if version == PARSER_VERSION else SECTION
    keys = {k:v for k,v in KEYS.items() if k != 'Am'} if version == 'yue2-subset/1.0.0' else KEYS
    extended = version in ('yue2-subset/1.2.0', PARSER_VERSION)
    lyric_fields = version == PARSER_VERSION
    note_pattern = EXT_NOTE if extended else NOTE
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 1_000_000:
        raise ScoreError('INVALID_ABC_BYTES')
    try:
        source = raw.decode('utf-8')
    except UnicodeError as exc:
        raise ScoreError('INVALID_UTF8') from exc
    tokens, diagnostics, sections, bars, events = [], [], [], [], []
    headers, voices, counters, pending = {}, {}, defaultdict(int), defaultdict(list)
    previous_note = {}
    accidentals = defaultdict(dict)
    tuplet_left, tuplet_factor = 0, Fraction(1)
    broken_factor = Fraction(1)
    voice, section, in_body = 'default', None, False
    offset = 0
    meter, unit = Fraction(1), Fraction(1, 8)
    section_counts = defaultdict(int)

    def diagnostic(code, start, end, severity='error'):
        diagnostics.append(dict(code=code, byte_start=start, byte_end=end, severity=severity))

    def token(kind, value, start):
        item = dict(kind=kind, text=value, byte_start=start, byte_end=start + len(value.encode('utf-8')))
        tokens.append(item)
        return item

    def begin_section(label, start):
        nonlocal section
        if section:
            section['byte_end'] = start
        section_counts[label] += 1
        section = dict(section_id=f'sec_{len(sections) + 1:03}', suggested_label=f'{label}_{section_counts[label]}',
                       label_status='suggested', byte_start=start, byte_end=len(raw))
        sections.append(section)

    def pitch_number(letter, octave):
        accidental, letter = letter[:-1],letter[-1]
        base = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}[letter.upper()]
        register=(1 if letter.islower() else 0)+octave.count("'")-octave.count(',')
        alteration = int(letter.upper() in keys.get(headers.get('K', ''), ()))
        if extended:
            fifths=KEY_FIFTHS.get(headers.get('K',''),0)
            alteration=(1 if fifths>0 else -1) if letter.upper() in ('FCGDAEB' if fifths>0 else 'BEADGCF')[:abs(fifths)] else 0
            address=(letter.upper(),register)
            if accidental: accidentals[voice][address]=accidental.count('^')-accidental.count('_')
            alteration=accidentals[voice].get(address,alteration)
        return 60 + base + 12*register + alteration

    def close_bar(end):
        group = pending[voice]
        if not group:
            return
        multi = [e for e in group if e['kind'] == 'measure_rest']
        count = multi[0]['measure_count'] if len(group) == 1 and multi else 1
        if multi and len(group) != 1:
            diagnostic('MIXED_MEASURE_REST', group[0]['byte_start'], end)
        duration = sum((Fraction(e['duration']['numerator'], e['duration']['denominator']) for e in group), Fraction())
        if duration != meter * count:
            diagnostic('BAR_DURATION_MISMATCH', group[0]['byte_start'], end)
        for virtual_index in range(count):
            counters[voice] += 1
            bar_id = f'bar:{voice}:{counters[voice]:06}'
            bar = dict(bar_id=bar_id, voice_id=voice, section_id=section['section_id'],
                       ordinal=counters[voice], byte_start=group[0]['byte_start'], byte_end=end,
                       duration=ratio(meter if count > 1 else duration),
                       shared_rest_group=count > 1, events=[])
            for index, original in enumerate(group, 1):
                event = dict(original, event_id=f'{bar_id}/event:{index:03}', bar_id=bar_id,
                             voice_id=voice, section_id=section['section_id'])
                if count > 1:
                    event['duration'] = ratio(meter)
                    event['virtual_rest_index'] = virtual_index
                bar['events'].append(event['event_id'])
                events.append(event)
                if event['kind'] == 'note':
                    prev = previous_note.get(voice)
                    event['tie_in'] = bool(prev and prev['tie_out'])
                    if event['tie_in'] and prev['midi_pitch'] != event['midi_pitch']:
                        diagnostic('TIE_PITCH_MISMATCH', event['byte_start'], event['byte_end'])
                    if event['tie_in']:
                        event['tie_from'] = prev['event_id']
                    previous_note[voice] = event
                elif event['kind'] in ('rest', 'measure_rest'):
                    prev = previous_note.pop(voice, None)
                    if prev and prev['tie_out']:
                        diagnostic('TIE_TO_REST', event['byte_start'], event['byte_end'])
            bars.append(bar)
        pending[voice] = []
        accidentals[voice].clear()

    for line in source.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        newline = line[len(body):]
        line_end = offset + len(body.encode('utf-8'))
        stripped = body.strip()
        field = re.fullmatch(r'([A-Za-z]):[ \t]*(.*)', body)
        section_match = section_pattern.fullmatch(stripped)
        if not stripped:
            token('whitespace', body, offset)
        elif stripped.startswith('%'):
            token('comment', body, offset)
            if stripped.startswith('%%'):
                diagnostic('UNSUPPORTED_DIRECTIVE', offset, offset + len(body.encode('utf-8')))
            if section_match:
                if any(pending.values()):
                    diagnostic('SECTION_INSIDE_BAR', offset, offset + len(body.encode('utf-8')))
                label = section_match[1].lower()
                begin_section('pre_chorus' if label.startswith('pre') else label, offset)
        elif field:
            name, value = field.groups()
            token('field', body, offset)
            if name == 'V':
                match = re.fullmatch(r'([A-Za-z][A-Za-z0-9_-]*)(.*)', value)
                if not match:
                    diagnostic('UNSUPPORTED_VOICE', offset, line_end)
                else:
                    if pending[voice]:
                        diagnostic('VOICE_SWITCH_INSIDE_BAR', offset, line_end)
                    voice, attrs = match.groups()
                    if in_body and attrs.strip() and not (lyric_fields and voice not in voices):
                        diagnostic('BODY_VOICE_ATTRIBUTES', offset, line_end)
                    # Only observed treble/name/short-name header attributes are understood.
                    remaining = re.sub(r'\s+(?:clef=treble|name="[^"]*"|snm="[^"]*")', '', attrs)
                    if remaining.strip():
                        diagnostic('UNSUPPORTED_VOICE_ATTRIBUTES', offset, line_end)
                    voices.setdefault(voice, dict(voice_id=voice, header=value))
            elif name == 'w' and lyric_fields:
                # Lossless lyric metadata; score/audio timing remains separate.
                if not in_body or not bars:
                    diagnostic('LYRICS_WITHOUT_MUSIC', offset, line_end)
            elif name in ('X', 'T', 'M', 'L', 'Q', 'K'):
                if in_body or name in headers:
                    diagnostic('BODY_OR_DUPLICATE_HEADER', offset, line_end)
                else:
                    headers[name] = value.strip()
                    try:
                        if name == 'X' and not re.fullmatch(r'[1-9]\d*', value.strip()):
                            raise ValueError()
                        if name in ('M', 'L'):
                            if not re.fullmatch(r'\d+/\d+', value.strip()):
                                raise ValueError()
                            parsed = Fraction(value.strip())
                            if parsed <= 0:
                                raise ValueError()
                            if name == 'M':
                                meter = parsed
                            else:
                                unit = parsed
                        if name == 'Q' and not re.fullmatch(r'1/4=[1-9]\d*', value.strip()):
                            raise ValueError()
                        if name == 'K' and value.strip() not in (KEY_FIFTHS if extended else keys):
                            diagnostic('UNSUPPORTED_KEY', offset, line_end)
                    except (ValueError, ZeroDivisionError):
                        diagnostic('UNSUPPORTED_HEADER_VALUE', offset, line_end)
                    if name == 'K':
                        in_body = True
            else:
                diagnostic('UNKNOWN_FIELD', offset, offset + len(body.encode('utf-8')))
        else:
            if not in_body:
                diagnostic('MUSIC_BEFORE_KEY', offset, offset + len(body.encode('utf-8')))
            if section is None:
                begin_section('unlabelled', offset)
            voices.setdefault(voice, dict(voice_id=voice, header=voice))
            cursor = 0
            byte_cursor = offset
            while cursor < len(body):
                remaining = body[cursor:]
                start = byte_cursor
                match = note_pattern.match(remaining)
                if remaining[0].isspace():
                    value = re.match(r'\s+', remaining)[0]
                    token('whitespace', value, start)
                elif remaining.startswith('%'):
                    value = remaining
                    token('comment', value, start)
                    if value.startswith('%%'):
                        diagnostic('UNSUPPORTED_DIRECTIVE', start, start + len(value.encode('utf-8')))
                elif remaining[0] == '"' and re.match(r'"[^"\r\n]*"', remaining):
                    value = re.match(r'"[^"\r\n]*"', remaining)[0]
                    token('chord_symbol', value, start)
                elif extended and re.match(r'\([234](?!\d|:)',remaining):
                    value=remaining[:2]
                    token('tuplet',value,start)
                    if tuplet_left: diagnostic('NESTED_TUPLET',start,start+2)
                    tuplet_left=int(value[1]);tuplet_factor=Fraction(3,2) if tuplet_left==2 else Fraction(2,3) if tuplet_left==3 else Fraction(3,4)
                elif extended and remaining[0] in '<>':
                    value=re.match(r'[<>]+',remaining)[0]
                    token('broken_rhythm',value,start)
                    if not pending[voice] or len(set(value))!=1 or broken_factor!=1:
                        diagnostic('INVALID_BROKEN_RHYTHM',start,start+len(value))
                    else:
                        short=Fraction(1,2**len(value));long=2-short
                        prev=pending[voice][-1]
                        prev['duration']=ratio(Fraction(**prev['duration'])*(long if value[0]=='>' else short))
                        broken_factor=short if value[0]=='>' else long
                elif remaining[0] == '|' or (extended and remaining.startswith('[|')):
                    value = re.match(r'\|\]|\|\||\[\||\|',remaining)[0] if extended else '|'
                    token('barline', value, start)
                    close_bar(start + len(value))
                    if tuplet_left or broken_factor!=1: diagnostic('UNFINISHED_RHYTHM',start,start+len(value))
                elif remaining.startswith('Z'):
                    value = re.match(r'Z\d*', remaining)[0]
                    item = token('measure_rest', value, start)
                    count = int(value[1:] or 1)
                    if not 1 <= count <= 1024:
                        diagnostic('INVALID_MEASURE_REST', start, item['byte_end'])
                        count = 1
                    pending[voice].append(dict(kind='measure_rest', byte_start=start, byte_end=item['byte_end'],
                                               measure_count=count, duration=ratio(meter * count)))
                elif match:
                    value = match[0]
                    letter, octave, length, tie = match.groups()
                    item = token('rest' if letter[-1] == 'z' else 'note', value, start)
                    try:
                        duration = note_length(length) * unit
                        if extended:
                            duration *= broken_factor
                            broken_factor=Fraction(1)
                            if tuplet_left:
                                duration *= tuplet_factor;tuplet_left-=1
                        if duration <= 0:
                            raise ValueError()
                    except (ValueError, ZeroDivisionError):
                        diagnostic('INVALID_NOTE_LENGTH', start, item['byte_end'])
                        duration = Fraction(0)
                    event = dict(kind=item['kind'], byte_start=start, byte_end=item['byte_end'], duration=ratio(duration))
                    if letter[-1] != 'z':
                        if "'" in octave and ',' in octave:
                            diagnostic('MIXED_OCTAVE_MARKS', start, item['byte_end'])
                        event.update(pitch=letter + octave, pitch_byte_start=start, pitch_byte_end=start + len(letter + octave),
                                     midi_pitch=pitch_number(letter, octave), tie_out=bool(tie))
                    elif octave or tie or letter!='z':
                        diagnostic('INVALID_REST_SUFFIX', start, item['byte_end'])
                    pending[voice].append(event)
                else:
                    value = remaining[0]
                    token('UNKNOWN', value, start)
                    diagnostic('UNKNOWN_SYNTAX', start, start + len(value.encode('utf-8')))
                cursor += len(value)
                byte_cursor += len(value.encode('utf-8'))
        if newline:
            token('newline', newline, offset + len(body.encode('utf-8')))
        offset += len(line.encode('utf-8'))
    for current, items in pending.items():
        if items:
            diagnostic('UNTERMINATED_BAR', items[0]['byte_start'], len(raw))
    if tuplet_left or broken_factor!=1: diagnostic('UNFINISHED_RHYTHM',len(raw),len(raw))
    for event in previous_note.values():
        if event['tie_out']:
            diagnostic('DANGLING_TIE', event['byte_start'], event['byte_end'])
    for required in ('X', 'M', 'L', 'K'):
        if required not in headers:
            diagnostic('MISSING_' + required, 0, 0)
    if not bars:
        diagnostic('NO_COMPLETE_BARS', 0, len(raw))
    for part in sections:
        counts = {v: sum(1 for b in bars if b['voice_id'] == v and b['section_id'] == part['section_id']) for v in voices}
        part['bars_per_voice'] = counts
        if len(set(counts.values())) > 1:
            diagnostic('SECTION_VOICE_LENGTH_MISMATCH', part['byte_start'], part['byte_end'])
    reconstructed = ''.join(t['text'] for t in tokens).encode('utf-8')
    if reconstructed != raw:
        raise AssertionError('Lossless token coverage failed')
    return dict(schema_version='score/1', parser_version=version, address_version=ADDRESS_VERSION,
                source_abc_sha256=digest(raw), source_byte_length=len(raw),
                status='supported' if not diagnostics else 'partial',
                headers=headers, voices=list(voices.values()), sections=sections, bars=bars, events=events,
                tokens=tokens, parse_diagnostics=diagnostics, lossless_roundtrip=True,
                semantics='supported notation subset; no audio timing or lyric alignment')


def apply_pitch_edits(raw, representation, allowed_event_ids, edits):
    """Only replace pitch lexemes, preserving every byte outside granted spans."""
    if digest(raw) != representation['source_abc_sha256']:
        raise ScoreError('STALE_SCORE')
    if representation['status'] != 'supported':
        raise ScoreError('UNSUPPORTED_SCORE_FOR_EDIT')
    lookup = {e['event_id']: e for e in representation['events']}
    if not isinstance(edits, list) or not edits or len(edits) > 1000:
        raise ScoreError('INVALID_EDITS')
    allowed = set(allowed_event_ids)
    replacements, seen = [], set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'event_id', 'expected_pitch', 'new_pitch'}:
            raise ScoreError('INVALID_EDIT_FIELDS')
        eid = edit['event_id']
        if not isinstance(eid, str) or eid not in allowed or eid not in lookup:
            raise ScoreError('EDIT_OUTSIDE_ALLOWED_REGION')
        if eid in seen:
            raise ScoreError('DUPLICATE_EDIT')
        seen.add(eid)
        event = lookup[eid]
        if event['kind'] != 'note' or event.get('tie_in') or event.get('tie_out'):
            raise ScoreError('UNSUPPORTED_TIED_OR_NON_NOTE_EDIT')
        if edit['expected_pitch'] != event['pitch']:
            raise ScoreError('STALE_PITCH')
        pitch = edit['new_pitch']
        if not isinstance(pitch, str) or not PITCH.fullmatch(pitch) or len(pitch) > 5 or ("'" in pitch and ',' in pitch):
            raise ScoreError('INVALID_PITCH_REPLACEMENT')
        replacements.append((event['pitch_byte_start'], event['pitch_byte_end'], pitch.encode('ascii')))
    result = raw
    for start, end, value in sorted(replacements, reverse=True):
        result = result[:start] + value + result[end:]
    if result == raw:
        raise ScoreError('NO_MUSICAL_CHANGE')
    parsed = parse_abc(result)
    if parsed['status'] != 'supported':
        raise ScoreError('EDIT_PRODUCED_UNSUPPORTED_SCORE')
    # Defense in depth: topology, durations, ties, header values and unedited notes must match.
    before = representation['events']
    after = parsed['events']
    if len(before) != len(after) or representation['headers'] != parsed['headers']:
        raise ScoreError('INVARIANT_VIOLATION')
    for old, new in zip(before, after):
        for key in ('event_id', 'kind', 'bar_id', 'voice_id', 'section_id', 'duration', 'tie_in', 'tie_out'):
            if old.get(key) != new.get(key):
                raise ScoreError('INVARIANT_VIOLATION')
        if old['event_id'] not in seen and (old.get('pitch') != new.get('pitch') or old.get('midi_pitch') != new.get('midi_pitch')):
            raise ScoreError('INVARIANT_VIOLATION')
    return result, dict(source_abc_sha256=digest(raw), result_abc_sha256=digest(result),
                        changed_event_ids=sorted(seen), protected_bytes_preserved=True,
                        brief_preserved=True, topology_preserved=True, durations_preserved=True,
                        audio_preservation='not_supported', parser_version=PARSER_VERSION)
