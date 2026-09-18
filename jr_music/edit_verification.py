"""Independent check of persisted edit inputs/results, without using the patcher.

This is independent of the mutation algorithm, not an independent ABC parser or
an aesthetic critic. Source bytes and the producer's stored grant are authoritative.
"""
from . import score
from .store import canonical, require, sha

VERIFIER_VERSION = 'protected-pitch/1.0.0'


def compare(source, result, source_brief, result_brief, representation, request, edits):
    require(isinstance(source, bytes) and isinstance(result, bytes), 'INVALID_VERIFICATION_INPUT')
    require(sha(source) == request['source_abc_sha256'] == representation['source_abc_sha256'], 'VERIFICATION_SOURCE_MISMATCH')
    require(representation['parser_version'] in score.PARSER_VERSIONS and
            representation['address_version'] == score.ADDRESS_VERSION, 'VERIFIER_VERSION_UNAVAILABLE')
    before = score.parse_abc(source, version=representation['parser_version'])
    after = score.parse_abc(result, version=representation['parser_version'])
    require(before['status'] == after['status'] == 'supported', 'VERIFICATION_UNSUPPORTED_SCORE')
    # Reparse the source; don't trust stale or accidentally modified derived data.
    require(all(canonical(representation.get(k)) == canonical(v) for k, v in before.items()), 'VERIFICATION_REPRESENTATION_MISMATCH')
    require(canonical(source_brief) == canonical(result_brief), 'VERIFICATION_BRIEF_CHANGED')
    require(before['headers'] == after['headers'] and before['voices'] == after['voices'], 'VERIFICATION_HEADERS_CHANGED')
    require(len(before['events']) == len(after['events']) and len(before['bars']) == len(after['bars']) and
            len(before['sections']) == len(after['sections']), 'VERIFICATION_STRUCTURE_CHANGED')
    for old, new in zip(before['bars'], after['bars']):
        require(all(old.get(k) == new.get(k) for k in ('bar_id', 'voice_id', 'section_id', 'ordinal', 'duration', 'shared_rest_group', 'events')),
                'VERIFICATION_STRUCTURE_CHANGED')
    for old, new in zip(before['sections'], after['sections']):
        require(all(old.get(k) == new.get(k) for k in ('section_id', 'suggested_label', 'label_status', 'bars_per_voice')),
                'VERIFICATION_STRUCTURE_CHANGED')
    require(isinstance(edits, list) and 0 < len(edits) <= 1000, 'INVALID_VERIFICATION_EDITS')
    requested = {}
    for edit in edits:
        require(isinstance(edit, dict) and set(edit) == {'event_id', 'expected_pitch', 'new_pitch'} and
                all(isinstance(v, str) for v in edit.values()), 'INVALID_VERIFICATION_EDITS')
        require(edit['event_id'] not in requested, 'INVALID_VERIFICATION_EDITS')
        requested[edit['event_id']] = edit
    before_by_id = {e['event_id']: e for e in before['events']}
    allowed_events, allowed_bars = set(request['allowed_event_ids']), set(request['allowed_bar_ids'])
    require(set(requested) <= allowed_events and set(requested) <= before_by_id.keys(), 'VERIFICATION_OUTSIDE_GRANT')
    for eid, edit in requested.items():
        event = before_by_id[eid]
        require(event['kind'] == 'note' and event['bar_id'] in allowed_bars and not event.get('tie_in') and not event.get('tie_out'),
                'VERIFICATION_OUTSIDE_GRANT')
        require(event['pitch'] == edit['expected_pitch'], 'VERIFICATION_EXPECTED_PITCH_MISMATCH')
        require(score.PITCH.fullmatch(edit['new_pitch']) and len(edit['new_pitch']) <= 5 and
                not ("'" in edit['new_pitch'] and ',' in edit['new_pitch']), 'INVALID_VERIFICATION_EDITS')
    changes = []
    for old, new in zip(before['events'], after['events']):
        require(all(old.get(k) == new.get(k) for k in ('event_id', 'kind', 'bar_id', 'voice_id', 'section_id', 'duration',
                'tie_in', 'tie_out', 'tie_from', 'measure_count', 'virtual_rest_index')), 'VERIFICATION_STRUCTURE_CHANGED')
        eid = old['event_id']
        if eid in requested:
            require(new.get('pitch') == requested[eid]['new_pitch'], 'VERIFICATION_RESULT_PITCH_MISMATCH')
        if old.get('pitch') != new.get('pitch'):
            require(eid in requested and eid in allowed_events and old['bar_id'] in allowed_bars, 'VERIFICATION_OUTSIDE_GRANT')
            require(not old.get('tie_in') and not old.get('tie_out'), 'VERIFICATION_TIED_NOTE_CHANGED')
            changes.append(dict(event_id=eid, bar_id=old['bar_id'], voice_id=old['voice_id'], section_id=old['section_id'],
                before_pitch=old['pitch'], after_pitch=new['pitch'], before_midi_pitch=old['midi_pitch'], after_midi_pitch=new['midi_pitch'],
                midi_pitch_changed=old['midi_pitch'] != new['midi_pitch'],
                source_span=dict(byte_start=old['pitch_byte_start'], byte_end=old['pitch_byte_end']),
                result_span=dict(byte_start=new['pitch_byte_start'], byte_end=new['pitch_byte_end'])))
        elif old.get('midi_pitch') != new.get('midi_pitch'):
            require(False, 'VERIFICATION_UNEXPECTED_PITCH_CHANGE')
    require(changes, 'VERIFICATION_NO_CHANGE')
    # Compare ALL intervening byte segments using independently reparsed offsets.
    # This catches whitespace/comments/chords/rhythm changes even when parse
    # semantics look identical, and handles variable-length pitch spellings.
    changes.sort(key=lambda c: c['source_span']['byte_start'])
    segments, source_cursor, result_cursor = [], 0, 0
    endpoints = [(c['source_span']['byte_start'], c['result_span']['byte_start'],
                  c['source_span']['byte_end'], c['result_span']['byte_end']) for c in changes]
    endpoints.append((len(source), len(result), len(source), len(result)))
    for source_end, result_end, next_source, next_result in endpoints:
        source_chunk, result_chunk = source[source_cursor:source_end], result[result_cursor:result_end]
        require(source_chunk == result_chunk, 'VERIFICATION_PROTECTED_BYTES_CHANGED')
        segments.append(dict(source_span=dict(byte_start=source_cursor, byte_end=source_end),
            result_span=dict(byte_start=result_cursor, byte_end=result_end), byte_length=len(source_chunk), sha256=sha(source_chunk)))
        source_cursor, result_cursor = next_source, next_result
    return dict(requested_event_ids=sorted(requested), changed_events=changes, protected_segments=segments,
        unchanged_requested_event_ids=sorted(set(requested) - {c['event_id'] for c in changes}),
        preserved=dict(headers=True, meter=True, tempo=True, key=True, voices=True, durations=True, structure=True,
                       ties=True, brief=True, outside_changed_pitch_spans=True, outside_allowed_region=True),
        source_brief_sha256=sha(canonical(source_brief)), result_brief_sha256=sha(canonical(result_brief)))


def make_manifest(*, verification_id, created_at, actor, source_revision, result_revision,
                  source_snapshot, result_snapshot, representation, request, edits):
    require(source_revision['project_id'] == result_revision['project_id'] == request['project_id'], 'VERIFICATION_PROJECT_MISMATCH')
    require(result_revision['parent_revision_id'] == source_revision['revision_id'] == request['revision_id'], 'VERIFICATION_BASE_MISMATCH')
    require(request['base_snapshot_sha256'] == source_revision['snapshot_sha256'] and
            request['representation_id'] == representation['representation_id'], 'VERIFICATION_BASE_MISMATCH')
    for revision, snapshot in ((source_revision, source_snapshot), (result_revision, result_snapshot)):
        require(sha(canonical(snapshot)) == revision['snapshot_sha256'] and
                sha(snapshot['abc'].encode('utf-8')) == revision['abc_sha256'], 'VERIFICATION_SNAPSHOT_MISMATCH')
    evidence = compare(source_snapshot['abc'].encode('utf-8'), result_snapshot['abc'].encode('utf-8'),
        source_snapshot['brief'], result_snapshot['brief'], representation, request, edits)
    return dict(schema_version='edit-verification/1', kind='protected_edit_verification',
        verification_id=verification_id, created_at=created_at, requested_by=actor,
        verifier_version=VERIFIER_VERSION, parser_version=representation['parser_version'], address_version=representation['address_version'],
        verifier_scope='independent mutation check using the same bounded ABC parser; not aesthetic or audio review',
        project_id=request['project_id'], source_revision_id=source_revision['revision_id'], result_revision_id=result_revision['revision_id'],
        source_snapshot_sha256=source_revision['snapshot_sha256'], result_snapshot_sha256=result_revision['snapshot_sha256'],
        source_abc_sha256=source_revision['abc_sha256'], result_abc_sha256=result_revision['abc_sha256'],
        source_byte_length=len(source_snapshot['abc'].encode('utf-8')), result_byte_length=len(result_snapshot['abc'].encode('utf-8')),
        representation_id=representation['representation_id'], representation_sha256=sha(canonical(representation)),
        request_id=request['request_id'], request_sha256=sha(canonical(request)), granted_by=request['granted_by'],
        allowed_bar_ids=request['allowed_bar_ids'], allowed_event_ids=request['allowed_event_ids'],
        submitted_edits=edits, submitted_edits_sha256=sha(canonical(edits)),
        audio_preservation='not_supported', audio_evaluated=False, aesthetics_evaluated=False,
        verification_method='source/result reparse plus exhaustive protected-byte segment comparison', **evidence)
