"""Verify native FLAC and produce a lossless, sample-identical WAV copy."""
import hashlib
import json
from pathlib import Path
import subprocess

from .store import StoreError, require


class AudioVerifier:
    version = 'flac-wav/1'

    def __init__(self, ffmpeg='ffmpeg', ffprobe='ffprobe'):
        self.ffmpeg, self.ffprobe = str(ffmpeg), str(ffprobe)

    def run(self, args):
        try:
            result = subprocess.run(args, capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StoreError('AUDIO_TOOL_UNAVAILABLE') from exc
        require(result.returncode == 0, 'AUDIO_DECODE_FAILED')
        return result.stdout

    def verify(self, source, wav, max_duration):
        with open(source, 'rb') as handle:
            require(handle.read(4) == b'fLaC', 'NATIVE_FLAC_REQUIRED')
        info = json.loads(self.run([self.ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-show_streams', '-show_format', '-of', 'json', str(source)]))
        streams = info.get('streams', [])
        require(len(streams) == 1 and streams[0].get('codec_name') == 'flac', 'INVALID_AUDIO_STREAMS')
        stream = streams[0]
        rate, channels = int(stream['sample_rate']), int(stream['channels'])
        bits = int(stream.get('bits_per_raw_sample', 0))
        duration = float(info['format']['duration'])
        require(rate in (44100, 48000) and channels in (1, 2) and bits in (16, 24), 'UNSUPPORTED_AUDIO_FORMAT')
        require(0 < duration <= float(max_duration) + 0.25, 'INVALID_AUDIO_DURATION')
        codec = 'pcm_s16le' if bits == 16 else 'pcm_s24le'
        pcm_format = 's16le' if bits == 16 else 's24le'
        base = [self.ffmpeg, '-v', 'error', '-xerror', '-err_detect', 'explode', '-protocol_whitelist', 'file,pipe']
        # Do not trust only a file header's declared duration to bound memory or
        # disk. Decode one second beyond budget; accepted files must match their
        # smaller declared sample count, so this cap never truncates accepted audio.
        decode_cap = str(float(max_duration) + 1)
        conversion = base + ['-i', str(source), '-t', decode_cap, '-map', '0:a:0', '-map_metadata', '-1', '-c:a', codec, '-f', 'wav', '-n', str(wav)]
        self.run(conversion)
        # Full decoding of both files. The 300s/rate/channel limits bound PCM RAM.
        original = self.run(base + ['-i', str(source), '-t', decode_cap, '-map', '0:a:0', '-c:a', codec, '-f', pcm_format, 'pipe:1'])
        delivery = self.run(base + ['-i', str(wav), '-t', decode_cap, '-map', '0:a:0', '-c:a', codec, '-f', pcm_format, 'pipe:1'])
        require(original and original == delivery, 'PCM_CONVERSION_MISMATCH')
        frames = len(original) // (channels * (bits // 8))
        require(abs(frames / rate - duration) <= 1 / rate, 'AUDIO_FRAME_COUNT_MISMATCH')
        version = self.run([self.ffmpeg, '-version']).decode('utf-8', errors='replace').splitlines()[0]
        return dict(verifier_version=self.version, ffmpeg_version=version, decoded=True, pcm_equal=True,
            pcm_sha256=hashlib.sha256(original).hexdigest(), frames=frames, sample_rate=rate, channels=channels,
            bits_per_sample=bits, duration_seconds=frames / rate, listened=False,
            conversion=dict(codec=codec, resampled=False, metadata_removed=True,
                arguments=['-t', decode_cap, '-map', '0:a:0', '-map_metadata', '-1', '-c:a', codec, '-f', 'wav']))
