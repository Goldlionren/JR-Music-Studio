"""Deterministic exports of saved notation, never a transcription of YuE audio.

Runtime uses only the standard library. Both encoders share exact rational
timing; incomplete notation is rejected rather than repaired during download.
"""
from collections import defaultdict
from fractions import Fraction
import hashlib
import json
import math
import re
import struct
from urllib.parse import quote
import xml.etree.ElementTree as ET

from .score import parse_abc, KEY_FIFTHS
from .store import require, StoreError
from . import score_lyrics

VERSION = 'score-export/1'
TYPES = [('maxima',8),('long',4),('breve',2),('whole',1),('half',Fraction(1,2)),
         ('quarter',Fraction(1,4)),('eighth',Fraction(1,8)),('16th',Fraction(1,16)),
         ('32nd',Fraction(1,32)),('64th',Fraction(1,64)),('128th',Fraction(1,128)),
         ('256th',Fraction(1,256)),('512th',Fraction(1,512)),('1024th',Fraction(1,1024))]


def prepare(source, mapping):
    snapshot=source['snapshot'];abc=snapshot['abc'];score=parse_abc(abc.encode())
    require(not re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]',abc+snapshot['brief'].get('lyrics','')),'EXPORT_INVALID_TEXT')
    require(score['status']=='supported','EXPORT_SCORE_UNSUPPORTED')
    tempo=re.fullmatch(r'1/4=([1-9]\d*)',score['headers'].get('Q',''))
    require(tempo is not None,'EXPORT_TEMPO_REQUIRED')
    bpm=int(tempo[1]);micros=round(60_000_000/bpm)
    require(1<=micros<=0xffffff,'EXPORT_TEMPO_RANGE')
    beats,denominator=map(int,score['headers']['M'].split('/'))
    require(1<=beats<=255 and denominator>0 and denominator&(denominator-1)==0 and denominator<=128,'EXPORT_METER_UNSUPPORTED')
    voices=[v for v in score['voices'] if any(b['voice_id']==v['voice_id'] for b in score['bars'])]
    require(0<len(voices)<=15 and len(score['events'])<=10000,'EXPORT_SIZE_LIMIT')
    ppq=480
    for event in score['events']:
        ppq=math.lcm(ppq,(Fraction(**event['duration'])*4).denominator)
        require(ppq<=32767,'EXPORT_TIMING_RESOLUTION')
        if event['kind']=='note':
            require(12<=event['midi_pitch']<=127,'EXPORT_PITCH_RANGE')
            pitch=event['pitch'];letter=re.search('[A-Ga-g]',pitch)[0]
            require(0<=4+int(letter.islower())+pitch.count("'")-pitch.count(',')<=9,'EXPORT_PITCH_RANGE')
    events={e['event_id']:dict(e) for e in score['events']}
    clocks=defaultdict(Fraction);bar_starts={};note_lookup={}
    for bar in score['bars']:
        voice=bar['voice_id'];bar_starts[bar['bar_id']]=clocks[voice];number=0
        for eid in bar['events']:
            event=events[eid];event['start']=clocks[voice]
            clocks[voice]+=Fraction(**event['duration'])*4;event['end']=clocks[voice]
            if event['kind']=='note':
                number+=1;note_lookup[(voice,bar['ordinal'],number)]=event
    require(max(clocks.values())*60/bpm<=3600,'EXPORT_SIZE_LIMIT')
    require(max(clocks.values())*ppq<=0xfffffff,'EXPORT_TIMING_RANGE')
    # Recover written tuplet groups from lossless tokens; durations are already
    # evaluated by the score parser, including broken rhythm and tuplets.
    tuplets={};remaining=0;total=0
    for token in score['tokens']:
        if token['kind']=='tuplet':total=remaining=int(token['text'][1])
        elif remaining and token['kind'] in ('note','rest'):
            tuplets[token['byte_start']]=(total,3 if total==2 else 2 if total==3 else 3,remaining==total,remaining==1)
            remaining-=1
    # Attach chord symbols to their next event, stopping at a bar/voice boundary.
    chords={};pending=[]
    for token in score['tokens']:
        if token['kind']=='chord_symbol':pending.append(token['text'][1:-1])
        elif token['kind'] in ('note','rest','measure_rest'):
            if pending:chords[token['byte_start']]=pending;pending=[]
        elif token['kind'] in ('barline','field'):
            require(not pending,'EXPORT_UNANCHORED_CHORD')
    require(not pending,'EXPORT_UNANCHORED_CHORD')
    lyrics={};warnings=[]
    lines=score_lyrics.lines(snapshot['brief'].get('lyrics',''))
    rows=score_lyrics.validate(abc,snapshot['brief'].get('lyrics',''),mapping.get('rows',[]))
    for row in rows:
        units=row['units'] or [[lines[row['line']-1]['text'],*row['range']]]
        for text,b1,n1,b2,n2 in units:
            start=note_lookup[(row['voice'],b1,n1)];end=note_lookup[(row['voice'],b2,n2)]
            group=[e for e in events.values() if e['voice_id']==row['voice'] and e['kind']=='note' and start['start']<=e['start']<=end['start']]
            attacks=[e for e in group if not e.get('tie_in')]
            chars=list(text.replace(' ',''))
            if all('\u3400'<=c<='\u9fff' for c in chars) and len(chars)==len(attacks):
                for e,char in zip(attacks,chars):lyrics[e['event_id']]=dict(text=char,extend=None)
            else:
                lyrics[start['event_id']]=dict(text=text,extend='start' if len(group)>1 else None)
                for i,e in enumerate(group[1:]):lyrics[e['event_id']]=dict(text=None,extend='stop' if i==len(group)-2 else 'continue')
                if len(group)>1:warnings.append('部分歌词保留字词组／句级跨度，未猜测逐字落音。')
    if len(rows)<len(lines):warnings.append('未配谱歌词保存在文件说明中，不猜测其音符位置。')
    warnings.append('导出创作输入谱；不包含 YuE2 实唱转谱或尚未写成音符的伴奏。MIDI 使用统一参考力度及钢琴音色。')
    metadata=dict(schema_version=VERSION,revision_id=source['revision']['revision_id'],
        snapshot_sha256=source['revision']['snapshot_sha256'],abc_sha256=score['source_abc_sha256'],
        lyric_mapping_id=mapping.get('mapping_id'),lyric_mapping_generation=mapping.get('generation',0),
        lyric_mapping_sha256=hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
        lyrics=snapshot['brief'].get('lyrics',''),warnings=list(dict.fromkeys(warnings)),
        lyric_policy='Saved ranges; Chinese groups split one character per attack only when counts match; otherwise preserve grouped spans.')
    return dict(score=score,events=events,voices=voices,bar_starts=bar_starts,clocks=clocks,
        bpm=bpm,micros=micros,beats=beats,denominator=denominator,ppq=ppq,tuplets=tuplets,chords=chords,
        lyrics=lyrics,metadata=metadata,title=score['headers'].get('T') or 'JR Music',warnings=metadata['warnings'])


def availability(source,mapping):
    try:
        model=prepare(source,mapping)
        return dict(available=True,warnings=model['warnings'],mapping_generation=mapping.get('generation',0),
            duration_seconds=float(max(model['clocks'].values())*60/model['bpm']),voice_count=len(model['voices']),
            bars_per_voice={v['voice_id']:sum(b['voice_id']==v['voice_id'] for b in model['score']['bars']) for v in model['voices']})
    except StoreError as exc:return dict(available=False,reason=str(exc),warnings=[])


def vlq(value):
    require(type(value) is int and 0<=value<=0xfffffff,'EXPORT_TIMING_RANGE')
    data=[value&127];value>>=7
    while value:data.insert(0,(value&127)|128);value>>=7
    return bytes(data)


def meta(kind,value):
    raw=value.encode('utf-8') if isinstance(value,str) else value
    return b'\xff'+bytes([kind])+vlq(len(raw))+raw


def midi(model):
    ppq=model['ppq'];end=int(max(model['clocks'].values())*ppq)
    def track(events):
        result=bytearray();last=0
        for at,priority,data in sorted(events,key=lambda e:(e[0],e[1])):
            result.extend(vlq(at-last));result.extend(data);last=at
        result.extend(vlq(end-last)+meta(0x2f,b''))
        return b'MTrk'+struct.pack('>I',len(result))+result
    key=model['score']['headers']['K'];fifths=KEY_FIFTHS[key]
    control=[(0,0,meta(3,model['title'])),(0,0,meta(1,json.dumps(model['metadata'],ensure_ascii=False))),
        (0,0,meta(0x51,model['micros'].to_bytes(3,'big'))),
        (0,0,meta(0x58,bytes([model['beats'],model['denominator'].bit_length()-1,24,8]))),
        (0,0,meta(0x59,struct.pack('bb',fifths,int(key.endswith('m')))))]
    sections={s['section_id']:s['suggested_label'] for s in model['score']['sections']};seen=set()
    tracks=[]
    for index,voice in enumerate(model['voices']):
        channel=index if index<9 else index+1
        events=[(0,0,meta(3,voice['voice_id'])),(0,0,bytes([0xc0|channel,0]))]
        for bar in (b for b in model['score']['bars'] if b['voice_id']==voice['voice_id']):
            if bar['section_id'] not in seen:
                seen.add(bar['section_id']);control.append((int(model['bar_starts'][bar['bar_id']]*ppq),1,meta(6,sections[bar['section_id']])))
            for eid in bar['events']:
                e=model['events'][eid];start=int(e['start']*ppq);stop=int(e['end']*ppq)
                for chord in model['chords'].get(e['byte_start'],[]):events.append((start,1,meta(1,'Chord: '+chord)))
                lyric=model['lyrics'].get(eid)
                if lyric and lyric['text']:events.append((start,1,meta(5,lyric['text'])))
                if e['kind']=='note':
                    if not e.get('tie_in'):events.append((start,3,bytes([0x90|channel,e['midi_pitch'],80])))
                    if not e.get('tie_out'):events.append((stop,2,bytes([0x80|channel,e['midi_pitch'],0])))
        tracks.append(track(events))
    return b'MThd'+struct.pack('>IHHH',6,1,len(tracks)+1,ppq)+track(control)+b''.join(tracks)


def sub(parent,tag,content=None,**attrs):
    element=ET.SubElement(parent,tag,attrs)
    if content is not None:element.text=str(content)
    return element


def direction(measure,text,tag='words'):
    return sub(sub(sub(measure,'direction',placement='above'),'direction-type'),tag,text)


def harmony(measure,text):
    kinds={'':'major','m':'minor','7':'dominant','maj7':'major-seventh','m7':'minor-seventh',
        'dim':'diminished','dim7':'diminished-seventh','aug':'augmented','sus2':'suspended-second',
        'sus4':'suspended-fourth','6':'major-sixth','m6':'minor-sixth','9':'dominant-ninth','m7b5':'half-diminished'}
    match=re.fullmatch(r'([A-G])([#b]?)(.*?)(?:/([A-G])([#b]?))?',text)
    if not match or match[3] not in kinds:
        direction(measure,text);return
    h=sub(measure,'harmony');root=sub(h,'root');sub(root,'root-step',match[1])
    if match[2]:sub(root,'root-alter',1 if match[2]=='#' else -1)
    sub(h,'kind',kinds[match[3]],text=match[3])
    if match[4]:
        bass=sub(h,'bass');sub(bass,'bass-step',match[4])
        if match[5]:sub(bass,'bass-alter',1 if match[5]=='#' else -1)


def musicxml(model):
    root=ET.Element('score-partwise',version='4.0');sub(sub(root,'work'),'work-title',model['title'])
    identification=sub(root,'identification');sub(sub(identification,'encoding'),'software','JR Music Studio '+VERSION)
    misc=sub(identification,'miscellaneous');sub(misc,'miscellaneous-field',json.dumps(model['metadata'],ensure_ascii=False),name='jr-source-and-lyrics')
    parts=sub(root,'part-list');score=model['score'];sections={s['section_id']:s['suggested_label'] for s in score['sections']}
    for i,voice in enumerate(model['voices'],1):
        part=sub(parts,'score-part',id=f'P{i}');sub(part,'part-name',voice['voice_id'])
        instrument=sub(part,'score-instrument',id=f'I{i}');sub(instrument,'instrument-name','Piano reference')
        mi=sub(part,'midi-instrument',id=f'I{i}');sub(mi,'midi-channel',i if i<=9 else i+1);sub(mi,'midi-program',1)
    for i,voice in enumerate(model['voices'],1):
        part=sub(root,'part',id=f'P{i}');last_section=None
        bars=[b for b in score['bars'] if b['voice_id']==voice['voice_id']]
        for index,bar in enumerate(bars):
            measure=sub(part,'measure',number=str(bar['ordinal']))
            if index==0:
                attributes=sub(measure,'attributes');sub(attributes,'divisions',model['ppq'])
                key=sub(attributes,'key');sub(key,'fifths',KEY_FIFTHS[score['headers']['K']]);sub(key,'mode','minor' if score['headers']['K'].endswith('m') else 'major')
                time=sub(attributes,'time');sub(time,'beats',model['beats']);sub(time,'beat-type',model['denominator'])
                clef=sub(attributes,'clef');sub(clef,'sign','G');sub(clef,'line',2)
                d=sub(measure,'direction');met=sub(sub(d,'direction-type'),'metronome');sub(met,'beat-unit','quarter');sub(met,'per-minute',model['bpm']);sub(d,'sound',tempo=str(model['bpm']))
            if bar['section_id']!=last_section:direction(measure,sections[bar['section_id']],'rehearsal');last_section=bar['section_id']
            for eid in bar['events']:
                e=model['events'][eid]
                for text in model['chords'].get(e['byte_start'],[]):harmony(measure,text)
                note=sub(measure,'note');duration=Fraction(**e['duration']);tuplet=model['tuplets'].get(e['byte_start'])
                if e['kind']=='note':
                    match=re.fullmatch(r'([_^=]*)([A-Ga-g])([,\x27]*)',e['pitch']);letter=match[2];octave=4+int(letter.islower())+match[3].count("'")-match[3].count(',')
                    natural=12*(octave+1)+{'C':0,'D':2,'E':4,'F':5,'G':7,'A':9,'B':11}[letter.upper()]
                    p=sub(note,'pitch');sub(p,'step',letter.upper());sub(p,'alter',e['midi_pitch']-natural);sub(p,'octave',octave)
                else:sub(note,'rest',**({'measure':'yes'} if e['kind']=='measure_rest' else {}))
                sub(note,'duration',int(duration*4*model['ppq']))
                for key,kind in (('tie_in','stop'),('tie_out','start')):
                    if e.get(key):sub(note,'tie',type=kind)
                sub(note,'voice','1')
                written=duration*Fraction(tuplet[0],tuplet[1]) if tuplet else duration
                if e['kind']!='measure_rest':
                    form=next(((name,dots) for name,base in TYPES for dots in range(4) if base*(2-Fraction(1,2**dots))==written),None)
                    if form:
                        sub(note,'type',form[0])
                        for _ in range(form[1]):sub(note,'dot')
                if tuplet:
                    tm=sub(note,'time-modification');sub(tm,'actual-notes',tuplet[0]);sub(tm,'normal-notes',tuplet[1])
                if e.get('tie_in') or e.get('tie_out') or tuplet:
                    notation=sub(note,'notations')
                    for key,kind in (('tie_in','stop'),('tie_out','start')):
                        if e.get(key):sub(notation,'tied',type=kind)
                    if tuplet:
                        if tuplet[2]:sub(notation,'tuplet',type='start',number='1')
                        if tuplet[3]:sub(notation,'tuplet',type='stop',number='1')
                lyric=model['lyrics'].get(eid)
                if lyric:
                    l=sub(note,'lyric',number='1')
                    if lyric['text'] is not None:sub(l,'syllabic','single');sub(l,'text',lyric['text'])
                    if lyric['extend']:sub(l,'extend',type=lyric['extend'])
            if index==len(bars)-1:sub(sub(measure,'barline',location='right'),'bar-style','light-heavy')
    ET.indent(root)
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


def download(store,pid,rid,extension):
    require(extension in ('mid','musicxml'),'INVALID_EXPORT_FORMAT')
    source=store.get_revision(pid,rid);mapping=score_lyrics.read(store,pid,rid);model=prepare(source,mapping)
    data=midi(model) if extension=='mid' else musicxml(model)
    title=re.sub(r'[\x00-\x1f<>:"/\\|?*]','_',model['title']).strip(' .')[:70] or 'JR-Music'
    filename=f'{title}-{rid[-8:]}-map{mapping.get("generation",0)}.{extension}'
    disposition=f'attachment; filename="JR-{rid[-8:]}.{extension}"; filename*=UTF-8\'\'{quote(filename)}'
    return data,('audio/midi' if extension=='mid' else 'application/vnd.recordare.musicxml+xml'),disposition
