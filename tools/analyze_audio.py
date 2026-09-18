"""Local ASR and waveform measurement. No source lyrics supplied to the recognizer."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time
from importlib.metadata import version
import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
from opencc import OpenCC


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument('--audio',required=True);p.add_argument('--output',required=True);p.add_argument('--model',required=True);a=p.parse_args()
    output=Path(a.output)
    # Cross-process lock survives service restarts through the OS file handle.
    # A resumed collector can wait for the original CPU task without running it twice.
    lock=output.with_suffix('.lock').open('a+b');lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    import msvcrt
    deadline=time.monotonic()+1800
    while True:
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1);break
        except OSError:
            if time.monotonic()>deadline:raise TimeoutError('ASR_LOCK_TIMEOUT')
            time.sleep(1)
    audio_hash=hashlib.sha256(Path(a.audio).read_bytes()).hexdigest()
    if output.exists():
        assert json.loads(output.read_text(encoding='utf-8'))['source_audio_sha256']==audio_hash
        return
    audio=decode_audio(a.audio,sampling_rate=16000)
    blocks=[audio[i:i+1600] for i in range(0,len(audio),1600)]
    waveform=[round(float(np.max(np.abs(x))),5) for x in blocks]
    energy=[round(float(np.sqrt(np.mean(x*x))),6) for x in blocks]
    measurements=dict(sample_period=0.1,duration=len(audio)/16000,waveform=waveform,energy=energy,
        peak=float(np.max(np.abs(audio))) if len(audio) else 0,clipped_fraction=float(np.mean(np.abs(audio)>=0.999)) if len(audio) else 0)
    model=WhisperModel(a.model,device='cpu',compute_type='int8',cpu_threads=6,local_files_only=True)
    segments,info=model.transcribe(audio,word_timestamps=True,vad_filter=True,beam_size=5,
        condition_on_previous_text=False,initial_prompt=None)
    rows=[]
    simplify=OpenCC('t2s')
    for segment in segments:
        rows.append(dict(start=segment.start,end=segment.end,text=simplify.convert(segment.text),raw_text=segment.text,
            avg_logprob=segment.avg_logprob,no_speech_prob=segment.no_speech_prob,
            words=[dict(start=w.start,end=w.end,text=simplify.convert(w.word),raw_text=w.word,probability=w.probability) for w in segment.words or []]))
        progress=output.with_suffix('.progress.json')
        temporary=progress.with_suffix('.tmp');temporary.write_text(json.dumps(dict(through_seconds=segment.end,duration=info.duration)),encoding='utf-8');temporary.replace(progress)
    digest=hashlib.sha256()
    with (Path(a.model)/'model.bin').open('rb') as model_file:
        for block in iter(lambda:model_file.read(8*1024*1024),b''):digest.update(block)
    result=dict(schema_version='local-asr/1',source_audio_sha256=audio_hash,model_sha256=digest.hexdigest(),
        dependencies={name:version(name) for name in ('faster-whisper','ctranslate2','opencc-python-reimplemented')},
        model='faster-whisper-large-v3-turbo',device='cpu',compute_type='int8',language=info.language,
        language_probability=info.language_probability,segments=rows,measurements=measurements,
        semantics='ASR word times and text are estimates; source lyrics were not supplied; not proof of singing accuracy')
    temporary=output.with_suffix('.tmp');temporary.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8');temporary.replace(output)

if __name__=='__main__':main()
