"""Deterministic score timing for reference listening, never inferred audio timing."""
from fractions import Fraction
import re


def audition(score):
    result = dict(status='unavailable', reason='', voices=[], source_abc_sha256=score['source_abc_sha256'])
    rhythm_only = bool(score['parse_diagnostics']) and all(d['code'] == 'BAR_DURATION_MISMATCH' for d in score['parse_diagnostics'])
    if score['status'] != 'supported' and not rhythm_only:
        result['reason'] = '谱面含未支持的语法，暂不合成，避免播放错误的旋律。'
        return result
    tempo = re.fullmatch(r'1/4=([1-9]\d*)', score['headers'].get('Q', ''))
    if not tempo:
        result['reason'] = '缺少明确的速度 Q:1/4=…，不猜测播放速度。'
        return result
    bpm = int(tempo[1])
    def seconds(duration):
        return Fraction(duration['numerator'], duration['denominator']) * Fraction(240, bpm)
    for voice in score['voices']:
        time = Fraction(0)
        notes, bars = [], []
        for bar in score['bars']:
            if bar['voice_id'] != voice['voice_id']:
                continue
            start = time
            for event in (e for e in score['events'] if e['bar_id'] == bar['bar_id']):
                duration = seconds(event['duration'])
                if event['kind'] == 'note':
                    pitch = event['midi_pitch']
                    if not 0 <= pitch <= 127:
                        result['reason'] = '音高超出试听范围。'
                        return result
                    if event.get('tie_in') and notes:
                        notes[-1]['end'] = float(time + duration)
                    else:
                        notes.append(dict(start=float(time), end=float(time + duration), midi=pitch, bar_id=bar['bar_id']))
                time += duration
            bars.append(dict(bar_id=bar['bar_id'], ordinal=bar['ordinal'], start=float(start), end=float(time)))
        if time > 600 or len(notes) > 10000:
            result['reason'] = '谱面超过当前试听上限（10 分钟或 10000 个音符）。'
            return result
        if bars:
            result['voices'].append(dict(voice_id=voice['voice_id'], duration=float(time), notes=notes, bars=bars))
    result.update(status='available', bpm=bpm, reason='按输入谱合成的单音旋律，不包含演唱、伴奏或音频对齐。')
    if rhythm_only:
        result['reason'] = '音符已完整识别；部分小节拍数异常，按原文实际时值试听，不自动补拍或截断。'
    return result
