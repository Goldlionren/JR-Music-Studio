"""Mandarin text load against declared score timing, never rap/audio detection."""
import math
import re
from . import score_lyrics
from .store import require, StoreError

VERSION = 'lyric-density/1'
HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0003134f]')


def guidance():
    return dict(version=VERSION, instructions='''For melodic Mandarin singing, plan lyric load before writing.
Do not fill every note with a new character or require one lyric line per bar.
Use fewer phrases, allow one phrase across multiple bars and one syllable across
multiple notes; leave phrase-end sustain and breathing space. A structurally valid
lyric_map does not prove singability. Do not preserve source line count unless the
producer explicitly locks it. For a spacious ballad, roughly 1–1.8 Han characters
per mapped score second is an exploratory drafting target, NOT a music-theory law
or a guarantee of rendered delivery. Check sustained dense passages, not only total
word count or max_duration (a ceiling, not available singing time). For explicitly
requested rap/fast delivery follow the request. Preserve all locked music/text;
if locks prevent reducing density, explain the conflict instead of violating locks.
If command.density_limits is present, the server enforces it before rendering:
count all sung Han characters, and check EVERY mapped lyric line's Han characters
divided by its actual declared score span. Complete mappings and explicit tempo
are required; do not evade limits with foreign text or moving words into brackets.
When asked to thin lyrics, remove redundant ideas rather than merely changing
line breaks. In summary report lyric load and phrase spacing, without claiming
you heard the result or proved absence of rap.''')


def validate_limits(limits):
    require(isinstance(limits, dict) and set(limits)=={'max_han_characters','max_line_han_per_second'}, 'INVALID_DENSITY_LIMITS')
    require(type(limits['max_han_characters']) is int and 1<=limits['max_han_characters']<=10000,'INVALID_DENSITY_LIMITS')
    rate=limits['max_line_han_per_second']
    require(type(rate) in (float,int) and math.isfinite(rate) and 0<rate<=20,'INVALID_DENSITY_LIMITS')


def analyze(abc,lyrics,rows):
    source=score_lyrics.lines(lyrics)
    result=dict(version=VERSION,basis='declared_score_not_audio',line_count=len(source),
        han_characters=sum(len(HAN.findall(l['text'])) for l in source),
        status='unavailable',lines=[],dense_runs=[],max_line_han_per_second=None,
        limitation='按输入谱与声明配谱估算汉字负荷，不是实唱速度，也不能判定是否说唱。')
    # Do not assign seconds to absent, partial or unsupported declarations.
    try:
        clean=score_lyrics.validate(abc,lyrics,rows,complete=True)
        parsed,notes,lookup=score_lyrics.geometry(abc)
    except (StoreError,ValueError,TypeError,KeyError):
        return result
    if parsed['status']!='supported' or not source:return result
    for row in clean:
        line=source[row['line']-1]
        a=lookup[(row['voice'],*row['range'][:2])];b=lookup[(row['voice'],*row['range'][2:])]
        start,end=a['start'],b['end'];text=line['text']
        chars=len(HAN.findall(text))
        other=any(c.isalnum() and not HAN.fullmatch(c) for c in text)
        rate=chars/(end-start) if start is not None and end is not None and end>start and not other else None
        result['lines'].append(dict(line=line['line'],section=line['section'],voice=row['voice'],text=text,
            han_characters=chars,start=start,end=end,han_per_second=rate))
    usable=[l for l in result['lines'] if l['han_per_second'] is not None]
    if len(usable)!=len(source):return result
    result['max_line_han_per_second']=max(l['han_per_second'] for l in usable)
    run=[]
    def flush():
        if run and run[-1]['end']-run[0]['start']>=12:
            result['dense_runs'].append(dict(section=run[0]['section'],voice=run[0]['voice'],
                first_line=run[0]['line'],last_line=run[-1]['line'],start=run[0]['start'],end=run[-1]['end']))
        run.clear()
    for line in usable:
        if run and (line['voice']!=run[-1]['voice'] or line['section']!=run[-1]['section'] or line['start']-run[-1]['end']>0.4):flush()
        if line['han_per_second']>=2.2:run.append(line)
        else:flush()
    flush()
    result['status']='warning' if result['dense_runs'] else 'measured'
    return result


def enforce(report,limits):
    if limits is None:return
    validate_limits(limits)
    require(report['status']!='unavailable','LYRIC_DENSITY_UNMEASURABLE')
    require(report['han_characters']<=limits['max_han_characters'] and
        report['max_line_han_per_second']<=limits['max_line_han_per_second']+1e-9,'LYRIC_DENSITY_LIMIT')
