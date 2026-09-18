"""Read-only, provisional lyric navigation; never audio or syllable alignment."""
import re
from collections import Counter


ALIASES = {'verse': 'verse', '主歌': 'verse', 'chorus': 'chorus', '副歌': 'chorus',
           'pre_chorus': 'pre_chorus', 'prechorus': 'pre_chorus', '预副歌': 'pre_chorus',
           'bridge': 'bridge', '桥段': 'bridge', 'intro': 'intro', '前奏': 'intro',
           'outro': 'outro', '尾奏': 'outro'}
TITLES = {'verse': '主歌', 'chorus': '副歌', 'pre_chorus': '预副歌',
          'bridge': '桥段', 'intro': '前奏', 'outro': '尾奏'}


def lyric_navigation(score, lyrics):
    """Match section occurrences only when their counts agree, then compare line counts.

    Equal counts suggest one line per measure, but do not prove it. Unequal counts
    expose only the section range. Unknown tags/voices remain explicitly unmapped.
    No inferred mapping is persisted or used to grant edit permissions.
    """
    groups = []
    for line in (lyrics or '').splitlines():
        line = line.strip()
        if not line:
            continue
        tag = re.fullmatch(r'\[([^\]]+)\]', line)
        if tag:
            name = re.sub(r'[\s-]+', '_', tag[1].strip().lower())
            kind = ALIASES.get(name)
            if not kind and groups and groups[-1]['kind']:
                # Singer/delivery annotations remain inside the enclosing section.
                # Their presence forces section navigation rather than a false
                # one-line/one-bar correspondence.
                groups[-1]['lines'].append(line)
                groups[-1]['annotations'] = True
            else:
                groups.append(dict(title=TITLES.get(kind, tag[1]), kind=kind, lines=[]))
        else:
            if not groups:
                groups.append(dict(title='歌词', kind=None, lines=[]))
            groups[-1]['lines'].append(line)
    voices = {b['voice_id'] for b in score['bars']}
    vocal = [v for v in voices if v.lower() in ('vocal', 'vocals', 'voice', 'singing', '人声')]
    voice = vocal[0] if len(vocal) == 1 else None
    parts = []
    for section in score['sections']:
        bars = [b for b in score['bars'] if b['voice_id'] == voice and b['section_id'] == section['section_id']]
        if bars:
            kind = section['suggested_label'].rsplit('_', 1)[0]
            parts.append(dict(kind=kind, bars=bars))
    lyric_counts = Counter(g['kind'] for g in groups)
    score_counts = Counter(p['kind'] for p in parts)
    occurrences = Counter()
    output = []
    for group in groups:
        kind = group['kind']
        index = occurrences[kind]
        occurrences[kind] += 1
        title = group['title'] + (f' {index + 1}' if lyric_counts[kind] > 1 else '')
        bars = []
        if kind and lyric_counts[kind] == score_counts[kind]:
            bars = [p for p in parts if p['kind'] == kind][index]['bars']
        mode = 'line_order_reference' if bars and len(bars) == len(group['lines']) and not group.get('annotations') else 'section_reference' if bars and group['lines'] else 'unmapped'
        if mode == 'unmapped':
            bars = []
        items = [dict(text=line, bar_ids=[bars[i]['bar_id']] if mode == 'line_order_reference' else [])
                 for i, line in enumerate(group['lines'])]
        output.append(dict(title=title, mode=mode, bars=[dict(bar_id=b['bar_id'], voice_id=b['voice_id'], ordinal=b['ordinal']) for b in bars], items=items))
    return dict(groups=output, semantics='provisional section/line-order navigation; no syllable or audio timing alignment')
