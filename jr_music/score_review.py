"""Read verified analysis evidence without promoting transcription to song truth."""
import hashlib
import json
from pathlib import Path
from .render import RenderService
from .score import parse_abc
from .score_audition import audition
from .store import require

ROOT = Path(__file__).resolve().parents[1]/'research'/'score-reviews'


def read_review(store, project_id, render_id, root=ROOT):
    job = RenderService(store).get(project_id, render_id)  # validates IDs/project membership
    path = root/render_id
    if not (path/'report.json').is_file():
        return dict(status='not_analyzed', message='这次音频尚未进行自动转谱，可先人工对照试听。')
    report = json.loads((path/'report.json').read_text(encoding='utf-8'))
    require(report['project_id'] == project_id and report['render_id'] == render_id and
            report['revision_id'] == job['revision_id'] and report['source_abc_sha256'] == job['abc_sha256'], 'REVIEW_BINDING_MISMATCH')
    require(any(a['asset_id'] == report['source_asset_id'] and a['sha256'] == report['source_audio_sha256'] for a in job['assets']), 'REVIEW_AUDIO_MISMATCH')
    require(hashlib.sha256(report['transcribed_abc'].encode()).hexdigest() == report['transcribed_abc_sha256'], 'REVIEW_SCORE_MISMATCH')
    parsed = parse_abc(report['transcribed_abc'].encode())
    report['audition'] = audition(parsed)
    return report
