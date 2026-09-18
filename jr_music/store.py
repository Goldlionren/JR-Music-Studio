"""Transactional local storage. Opaque ABC is preserved, not musically parsed."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from .migrations import migrate, migrate_render, migrate_edit_verification, migrate_music, migrate_assessments, migrate_three_arm, operation_scope
from .cas import CAS
import io
from . import score
from .limits import MAX_DURATION_SECONDS


class StoreError(Exception):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


def require(value, code):
    if not value:
        raise StoreError(code)


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def new_id(prefix):
    return prefix + '_' + uuid.uuid4().hex


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,95}', value), 'INVALID_ID')
    return value


def text(value, limit=10000):
    require(isinstance(value, str) and value.strip() and len(value) <= limit, 'INVALID_TEXT')
    return value


class Store:
    """Trusted in-process API. HTTP authorization belongs to the server adapter.

    Snapshots/small legacy BLOBs share a SQLite transaction with metadata.
    New audio uses file CAS; publication and GC share the SQLite writer lock.
    Keep this database on a local disk; never share SQLite over SMB/NFS.
    """
    def __init__(self, database):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.cas = CAS(str(self.database) + '.cas')
        with self.connect() as db:
            require(db.execute('PRAGMA user_version').fetchone()[0] in (0, 2, 3, 4, 5, 6, 7), 'UNSUPPORTED_DATABASE_VERSION')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, document BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS blobs(hash TEXT PRIMARY KEY, content BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    parent_id TEXT REFERENCES revisions(id), snapshot_hash TEXT NOT NULL REFERENCES blobs(hash),
                    document BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS assets(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    revision_id TEXT NOT NULL REFERENCES revisions(id), blob_hash TEXT NOT NULL REFERENCES blobs(hash),
                    document BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions(
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), document BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id), document BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(
                    actor TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, result BLOB NOT NULL,
                    PRIMARY KEY(actor,key));
                CREATE INDEX IF NOT EXISTS revision_project ON revisions(project_id);
                CREATE INDEX IF NOT EXISTS asset_revision ON assets(project_id,revision_id);
                CREATE INDEX IF NOT EXISTS event_project ON events(project_id,sequence);
            ''')
            migrate(db, self.database)
            migrate_render(db, self.database)
            migrate_edit_verification(db, self.database)
            migrate_music(db, self.database)
            migrate_assessments(db, self.database)
            migrate_three_arm(db, self.database)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        finally:
            db.close()

    def _operation(self, actor, key, payload, action):
        identifier(actor)
        text(key, 200)
        fingerprint = sha(canonical(payload))
        scope, operation = operation_scope(payload)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                prior = db.execute('SELECT fingerprint,result FROM operations WHERE actor=? AND scope=? AND operation=? AND key=?',
                                   (actor, scope, operation, key)).fetchone()
                if prior:
                    require(prior['fingerprint'] == fingerprint, 'IDEMPOTENCY_CONFLICT')
                    result = json.loads(prior['result'])
                else:
                    result = action(db)
                    db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)', (actor, scope, operation, key, fingerprint, canonical(result)))
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _project(db, project_id):
        row = db.execute('SELECT document FROM projects WHERE id=?', (identifier(project_id),)).fetchone()
        require(row is not None, 'PROJECT_NOT_FOUND')
        return json.loads(row['document'])

    @staticmethod
    def _revision(db, project_id, revision_id):
        row = db.execute('SELECT document FROM revisions WHERE project_id=? AND id=?', (project_id, identifier(revision_id))).fetchone()
        require(row is not None, 'REVISION_NOT_FOUND')
        return json.loads(row['document'])

    @staticmethod
    def _blob(db, content):
        digest = sha(content)
        db.execute('INSERT OR IGNORE INTO blobs VALUES(?,?)', (digest, content))
        return digest

    @staticmethod
    def _event(db, project_id, event, actor, data):
        value = dict(type=event, actor_id=actor, created_at=now(), **data)
        db.execute('INSERT INTO events(project_id,document) VALUES(?,?)', (project_id, canonical(value)))

    def create_project(self, title, *, actor, key):
        text(title, 200)
        def action(db):
            project = dict(schema_version='0.1.0', kind='project', project_id=new_id('song'), title=title,
                           head_revision_id=None, head_generation=0, created_at=now())
            db.execute('INSERT INTO projects VALUES(?,?)', (project['project_id'], canonical(project)))
            self._event(db, project['project_id'], 'project_created', actor, {})
            return project
        return self._operation(actor, key, dict(op='create_project', title=title), action)

    def _insert_revision(self, db, project_id, abc, brief, summary, parent_revision_id, actor, generation=None):
        snapshot = dict(schema_version='storage/1', kind='opaque_song_snapshot', project_id=project_id,
                        import_status='opaque_import', abc=abc, brief=brief)
        if generation is not None:snapshot['generation']=generation
        snapshot_bytes = canonical(snapshot)
        if parent_revision_id:
            parent = self._revision(db, project_id, parent_revision_id)
            require(parent['snapshot_sha256'] != sha(snapshot_bytes), 'NO_MUSICAL_CHANGE')
        snapshot_hash = self._blob(db, snapshot_bytes)
        abc_hash = self._blob(db, abc.encode('utf-8'))
        revision = dict(schema_version='storage/1', kind='opaque_revision', project_id=project_id,
                        revision_id=new_id('rev'), parent_revision_id=parent_revision_id,
                        snapshot_sha256=snapshot_hash, abc_sha256=abc_hash, author_id=actor, created_at=now(), summary=summary)
        db.execute('INSERT INTO revisions VALUES(?,?,?,?,?)',
                   (revision['revision_id'], project_id, parent_revision_id, snapshot_hash, canonical(revision)))
        self._event(db, project_id, 'revision_created', actor, dict(revision_id=revision['revision_id']))
        from .score_lyrics import inherit_on_insert
        inherit_on_insert(self, db, revision)
        return revision

    @staticmethod
    def _snapshot(db, revision):
        content = db.execute('SELECT content FROM blobs WHERE hash=?', (revision['snapshot_sha256'],)).fetchone()['content']
        require(sha(content) == revision['snapshot_sha256'], 'CORRUPT_ASSET')
        snapshot = json.loads(content)
        require(sha(snapshot['abc'].encode('utf-8')) == revision['abc_sha256'], 'CORRUPT_ASSET')
        return snapshot

    @staticmethod
    def validate_revision(abc, brief, summary, generation=None):
        if generation is None:text(abc, 1_000_000)
        else:
            require(isinstance(generation,dict) and set(generation)=={'mode','abc_planning'} and generation['mode']=='style_lyrics'
                and type(generation['abc_planning']) is bool and abc=='','INVALID_DIRECT_INPUT')
        text(summary, 2000)
        require(isinstance(brief, dict) and set(brief) == {'style', 'lyrics', 'checkpoint', 'seed', 'max_duration'}, 'INVALID_BRIEF')
        for field in ('style', 'lyrics', 'checkpoint'):
            text(brief[field])
        require(type(brief['seed']) is int and 0 <= brief['seed'] < 2**64, 'INVALID_SEED')
        require(type(brief['max_duration']) in (int, float) and 0 < brief['max_duration'] <= MAX_DURATION_SECONDS, 'INVALID_DURATION')
        if generation is None:require(re.search(r'^X:[ \t]*\S+', abc, re.M) and re.search(r'^K:[ \t]*\S+', abc, re.M), 'ABC_HEADERS_MISSING')

    def add_revision(self, project_id, abc, brief, summary, *, parent_revision_id=None,
                     expected_parent_snapshot_sha256=None, actor, key, generation=None):
        identifier(project_id)
        self.validate_revision(abc, brief, summary, generation)
        # Raw import does not claim protected-edit validation; use apply_edit for that.
        require((parent_revision_id is None) == (expected_parent_snapshot_sha256 is None), 'STALE_BASE')
        payload = dict(op='add_revision', project_id=project_id, abc=abc, brief=brief, summary=summary,
                       parent_revision_id=parent_revision_id, expected_parent_snapshot_sha256=expected_parent_snapshot_sha256)
        if generation is not None:payload['generation']=generation
        def action(db):
            self._project(db, project_id)
            if parent_revision_id is not None:
                parent = self._revision(db, project_id, parent_revision_id)
                require(parent['snapshot_sha256'] == expected_parent_snapshot_sha256, 'STALE_BASE')
            return self._insert_revision(db, project_id, abc, brief, summary, parent_revision_id, actor, generation)
        return self._operation(actor, key, payload, action)

    def derive_score(self, project_id, revision_id, *, actor, key):
        payload = dict(op='derive_score', project_id=project_id, revision_id=revision_id, parser_version=score.PARSER_VERSION)
        def action(db):
            revision = self._revision(db, project_id, revision_id)
            prior = db.execute('SELECT document FROM score_representations WHERE revision_id=? AND source_hash=? AND parser_version=?',
                               (revision_id, revision['abc_sha256'], score.PARSER_VERSION)).fetchone()
            if prior:
                return json.loads(prior['document'])
            snapshot = self._snapshot(db, revision)
            parsed = score.parse_abc(snapshot['abc'].encode('utf-8'))
            parsed.update(representation_id=new_id('score'), project_id=project_id, revision_id=revision_id, created_at=now())
            db.execute('INSERT INTO score_representations VALUES(?,?,?,?,?,?)',
                       (parsed['representation_id'], project_id, revision_id, revision['abc_sha256'], score.PARSER_VERSION, canonical(parsed)))
            self._event(db, project_id, 'score_derived', actor, dict(representation_id=parsed['representation_id'], revision_id=revision_id))
            return parsed
        return self._operation(actor, key, payload, action)

    @staticmethod
    def _representation(db, project_id, revision_id, representation_id):
        row = db.execute('SELECT document FROM score_representations WHERE project_id=? AND revision_id=? AND id=?',
                         (project_id, revision_id, identifier(representation_id))).fetchone()
        require(row is not None, 'SCORE_NOT_FOUND')
        return json.loads(row['document'])

    def get_score(self, project_id, revision_id, representation_id):
        with self.connect() as db:
            return self._representation(db, project_id, revision_id, representation_id)

    def list_scores(self, project_id, revision_id):
        with self.connect() as db:
            self._revision(db, project_id, revision_id)
            return [json.loads(row['document']) for row in db.execute(
                'SELECT document FROM score_representations WHERE project_id=? AND revision_id=? ORDER BY rowid', (project_id, revision_id))]

    def create_edit_request(self, project_id, revision_id, representation_id, source_abc_sha256,
                            allowed_bar_ids, instruction, *, actor, key):
        text(instruction, 4000)
        require(isinstance(allowed_bar_ids, list) and 0 < len(allowed_bar_ids) <= 128 and
                all(isinstance(v, str) for v in allowed_bar_ids) and len(set(allowed_bar_ids)) == len(allowed_bar_ids), 'INVALID_EDIT_REGION')
        payload = dict(op='create_edit_request', project_id=project_id, revision_id=revision_id,
                       representation_id=representation_id, source_abc_sha256=source_abc_sha256,
                       allowed_bar_ids=allowed_bar_ids, instruction=instruction)
        def action(db):
            revision = self._revision(db, project_id, revision_id)
            require(source_abc_sha256 == revision['abc_sha256'], 'STALE_SCORE')
            parsed = self._representation(db, project_id, revision_id, representation_id)
            require(parsed['source_abc_sha256'] == source_abc_sha256 and parsed['parser_version'] == score.PARSER_VERSION, 'STALE_SCORE')
            require(parsed['status'] == 'supported', 'UNSUPPORTED_SCORE_FOR_EDIT')
            known = {b['bar_id'] for b in parsed['bars']}
            require(set(allowed_bar_ids) <= known, 'UNKNOWN_BAR')
            allowed = [e['event_id'] for e in parsed['events'] if e['bar_id'] in allowed_bar_ids and e['kind'] == 'note'
                       and not e.get('tie_in') and not e.get('tie_out')]
            require(allowed, 'NO_EDITABLE_NOTES')
            request = dict(schema_version='score/1', request_id=new_id('edit'), project_id=project_id,
                           revision_id=revision_id, representation_id=representation_id,
                           source_abc_sha256=source_abc_sha256, base_snapshot_sha256=revision['snapshot_sha256'],
                           allowed_bar_ids=allowed_bar_ids, allowed_event_ids=allowed, operation='replace_pitch',
                           instruction=instruction, granted_by=actor, created_at=now(),
                           section_meaning='producer chose explicit structural bars; no inferred lyric/audio alignment')
            db.execute('INSERT INTO edit_requests VALUES(?,?,?,?)', (request['request_id'], project_id, revision_id, canonical(request)))
            self._event(db, project_id, 'edit_region_granted', actor, dict(request_id=request['request_id'], revision_id=revision_id))
            return request
        return self._operation(actor, key, payload, action)

    def apply_edit(self, project_id, revision_id, request_id, edits, summary, *, actor, key):
        text(summary, 2000)
        payload = dict(op='apply_edit', project_id=project_id, revision_id=revision_id, request_id=request_id,
                       edits=edits, summary=summary)
        def action(db):
            revision = self._revision(db, project_id, revision_id)
            row = db.execute('SELECT document FROM edit_requests WHERE project_id=? AND revision_id=? AND id=?',
                             (project_id, revision_id, identifier(request_id))).fetchone()
            require(row is not None, 'EDIT_REQUEST_NOT_FOUND')
            request = json.loads(row['document'])
            require(request['base_snapshot_sha256'] == revision['snapshot_sha256'] and request['source_abc_sha256'] == revision['abc_sha256'], 'STALE_BASE')
            parsed = self._representation(db, project_id, revision_id, request['representation_id'])
            require(parsed['parser_version'] == score.PARSER_VERSION, 'STALE_SCORE')
            snapshot = self._snapshot(db, revision)
            raw = snapshot['abc'].encode('utf-8')
            try:
                updated, validation = score.apply_pitch_edits(raw, parsed, request['allowed_event_ids'], edits)
            except score.ScoreError as exc:
                raise StoreError(exc.code) from exc
            child = self._insert_revision(db, project_id, updated.decode('utf-8'), snapshot['brief'], summary, revision_id, actor)
            from . import edit_verification
            manifest = edit_verification.make_manifest(verification_id=new_id('verification'), created_at=now(), actor=actor,
                source_revision=revision, result_revision=child, source_snapshot=snapshot, result_snapshot=self._snapshot(db, child),
                representation=parsed, request=request, edits=edits)
            manifest_bytes = canonical(manifest)
            manifest_hash = self._blob(db, manifest_bytes)
            asset = dict(asset_id=new_id('asset'), project_id=project_id, revision_id=child['revision_id'],
                name='protected-edit-verification.json', media_type='application/json', sha256=manifest_hash,
                size=len(manifest_bytes), created_at=manifest['created_at'], verification='protected_edit_verified')
            db.execute('INSERT INTO assets VALUES(?,?,?,?,?)', (asset['asset_id'], project_id, child['revision_id'], manifest_hash, canonical(asset)))
            record = dict(verification_id=manifest['verification_id'], project_id=project_id, source_revision_id=revision_id,
                result_revision_id=child['revision_id'], request_id=request_id, manifest_sha256=manifest_hash,
                verifier_version=manifest['verifier_version'], created_at=manifest['created_at'], asset=asset)
            db.execute('INSERT INTO edit_verifications VALUES(?,?,?,?,?,?,?,?)',
                (record['verification_id'], project_id, revision_id, child['revision_id'], request_id, asset['asset_id'], manifest_hash, canonical(record)))
            validation['changed_event_ids'] = sorted(c['event_id'] for c in manifest['changed_events'])
            validation.update(request_id=request_id, base_snapshot_sha256=revision['snapshot_sha256'], child_snapshot_sha256=child['snapshot_sha256'])
            validation.update(verification_id=record['verification_id'], verification_manifest_sha256=manifest_hash)
            self._event(db, project_id, 'protected_edit_applied', actor, dict(revision_id=child['revision_id'], validation=validation))
            return dict(revision=child, validation=validation, verification=record)
        return self._operation(actor, key, payload, action)

    def list_edit_requests(self, project_id, revision_id):
        with self.connect() as db:
            self._revision(db, project_id, revision_id)
            return [json.loads(row['document']) for row in db.execute(
                'SELECT document FROM edit_requests WHERE project_id=? AND revision_id=? ORDER BY rowid', (project_id, revision_id))]

    def list_edit_verifications(self, project_id, revision_id):
        with self.connect() as db:
            self._revision(db, project_id, revision_id)
            return [json.loads(row['document']) for row in db.execute(
                'SELECT document FROM edit_verifications WHERE project_id=? AND result_revision_id=? ORDER BY rowid', (project_id, revision_id))]

    @staticmethod
    def _edit_verification(db, project_id, revision_id, verification_id):
        row = db.execute('SELECT document,manifest_hash FROM edit_verifications WHERE project_id=? AND result_revision_id=? AND id=?',
                         (identifier(project_id), identifier(revision_id), identifier(verification_id))).fetchone()
        require(row is not None, 'VERIFICATION_NOT_FOUND')
        record = json.loads(row['document'])
        content = db.execute('SELECT content FROM blobs WHERE hash=?', (row['manifest_hash'],)).fetchone()[0]
        require(sha(content) == row['manifest_hash'] == record['manifest_sha256'] == record['asset']['sha256'], 'CORRUPT_VERIFICATION')
        return record, json.loads(content)

    def get_edit_verification(self, project_id, revision_id, verification_id):
        with self.connect() as db:
            record, manifest = self._edit_verification(db, project_id, revision_id, verification_id)
            return dict(record=record, manifest=manifest)

    def check_edit_verification(self, project_id, revision_id, verification_id):
        """Read-only replay against persisted source/result, grant and representation.

        Doesn't trust preserved=true flags, change logs or the original patcher.
        Doesn't rewrite or issue a new manifest for old parser/checker versions.
        """
        from . import edit_verification
        with self.connect() as db:
            db.execute('BEGIN')
            record, manifest = self._edit_verification(db, project_id, revision_id, verification_id)
            require(manifest['verifier_version'] == edit_verification.VERIFIER_VERSION and
                    manifest['parser_version'] in score.PARSER_VERSIONS and manifest['address_version'] == score.ADDRESS_VERSION,
                    'VERIFIER_VERSION_UNAVAILABLE')
            source = self._revision(db, project_id, record['source_revision_id'])
            result = self._revision(db, project_id, revision_id)
            request_row = db.execute('SELECT document FROM edit_requests WHERE project_id=? AND revision_id=? AND id=?',
                                    (project_id, source['revision_id'], record['request_id'])).fetchone()
            require(request_row is not None, 'EDIT_REQUEST_NOT_FOUND')
            request = json.loads(request_row['document'])
            parsed = self._representation(db, project_id, source['revision_id'], request['representation_id'])
            rebuilt = edit_verification.make_manifest(verification_id=verification_id, created_at=record['created_at'],
                actor=manifest['requested_by'], source_revision=source, result_revision=result,
                source_snapshot=self._snapshot(db, source), result_snapshot=self._snapshot(db, result),
                representation=parsed, request=request, edits=manifest['submitted_edits'])
            require(canonical(rebuilt) == canonical(manifest), 'VERIFICATION_REPLAY_MISMATCH')
            return dict(valid=True, verification_id=verification_id, manifest_sha256=record['manifest_sha256'],
                        verifier_version=edit_verification.VERIFIER_VERSION, source_and_result_rechecked=True,
                        mutation_performed=False, checked_at=now(), audio_evaluated=False, aesthetics_evaluated=False)

    def decide(self, project_id, target_revision_id, action_name, expected_head_revision_id, expected_head_generation,
               reason, *, actor, key, selected_render_id=None):
        require(action_name in ('accept', 'reject', 'rollback'), 'INVALID_DECISION')
        require(type(expected_head_generation) is int and expected_head_generation >= 0, 'INVALID_GENERATION')
        text(reason, 4000)
        payload = dict(op='decide', project_id=project_id, target_revision_id=target_revision_id, action=action_name,
                       expected_head_revision_id=expected_head_revision_id, expected_head_generation=expected_head_generation, reason=reason)
        if selected_render_id is not None:
            payload['selected_render_id'] = selected_render_id
        def action(db):
            project = self._project(db, project_id)
            self._revision(db, project_id, target_revision_id)
            if selected_render_id is not None:
                from .render import RenderService
                job = RenderService._job(db, project_id, selected_render_id)
                require(job['revision_id'] == target_revision_id and job['state'] == 'succeeded', 'AUDIO_BINDING_MISMATCH')
            require(project['head_revision_id'] == expected_head_revision_id and project['head_generation'] == expected_head_generation,
                    'HEAD_CONFLICT')
            if action_name == 'rollback':
                # Rollback selects a version that was previously selected, not any arbitrary candidate.
                prior = [json.loads(row['document']) for row in db.execute('SELECT document FROM decisions WHERE project_id=?', (project_id,))]
                require(any(d['target_revision_id'] == target_revision_id and d['action'] in ('accept', 'rollback') for d in prior), 'ROLLBACK_TARGET_NOT_SELECTED')
            if action_name != 'reject':
                project['head_revision_id'] = target_revision_id
                project['head_generation'] += 1  # Also invalidate stale decisions when re-selecting the same revision.
                db.execute('UPDATE projects SET document=? WHERE id=?', (canonical(project), project_id))
            decision = dict(schema_version='0.1.0', kind='decision', project_id=project_id, decision_id=new_id('decision'),
                            action=action_name, target_revision_id=target_revision_id, selected_render_id=selected_render_id,
                            expected_head_revision_id=expected_head_revision_id, expected_head_generation=expected_head_generation,
                            decided_by=dict(actor_id=actor, runtime='human', display_name='制作人'), created_at=now(), reason=reason)
            db.execute('INSERT INTO decisions VALUES(?,?,?)', (decision['decision_id'], project_id, canonical(decision)))
            self._event(db, project_id, 'decision_recorded', actor, dict(decision_id=decision['decision_id'], action=action_name,
                                                                     target_revision_id=target_revision_id, head_generation=project['head_generation']))
            return dict(project=project, decision=decision)
        return self._operation(actor, key, payload, action)

    def archive_asset(self, project_id, revision_id, content, name, media_type, *, actor, key):
        require(isinstance(content, bytes) and 0 < len(content) <= 32 * 1024 * 1024, 'INVALID_ASSET_SIZE')
        require(isinstance(name, str) and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', name), 'INVALID_ASSET_NAME')
        text(media_type, 100)
        payload = dict(op='archive_asset', project_id=project_id, revision_id=revision_id, sha256=sha(content), name=name, media_type=media_type)
        def action(db):
            self._revision(db, project_id, revision_id)
            asset = dict(asset_id=new_id('asset'), project_id=project_id, revision_id=revision_id, name=name, media_type=media_type,
                         sha256=self._blob(db, content), size=len(content), created_at=now(), verification='archived_bytes_only')
            db.execute('INSERT INTO assets VALUES(?,?,?,?,?)', (asset['asset_id'], project_id, revision_id, asset['sha256'], canonical(asset)))
            self._event(db, project_id, 'asset_archived', actor, dict(asset_id=asset['asset_id'], revision_id=revision_id))
            return asset
        return self._operation(actor, key, payload, action)

    def get_project(self, project_id):
        with self.connect() as db:
            return self._project(db, project_id)

    def list_projects(self):
        with self.connect() as db:
            return [json.loads(row['document']) for row in db.execute('SELECT document FROM projects ORDER BY id')]

    def get_revision(self, project_id, revision_id):
        with self.connect() as db:
            revision = self._revision(db, project_id, revision_id)
            snapshot = self._snapshot(db, revision)
            assets = [json.loads(row['document']) for row in db.execute('''SELECT id,document FROM file_assets WHERE project_id=? AND revision_id=?
                UNION ALL SELECT id,document FROM assets WHERE project_id=? AND revision_id=? AND id NOT IN (SELECT id FROM file_assets) ORDER BY id''',
                (project_id, revision_id, project_id, revision_id))]
            return dict(revision=revision, snapshot=snapshot, assets=assets)

    def get_asset(self, project_id, asset_id):
        asset, stream = self.open_asset(project_id, asset_id)
        with stream:
            return asset, stream.read()

    def open_asset(self, project_id, asset_id):
        with self.connect() as db:
            row = db.execute('SELECT document FROM file_assets WHERE project_id=? AND id=?', (identifier(project_id), identifier(asset_id))).fetchone()
            if row:
                asset = json.loads(row['document'])
                try:
                    return asset, self.cas.open_verified(asset['sha256'], asset['size'])
                except (OSError, ValueError) as exc:
                    raise StoreError('CORRUPT_ASSET') from exc
            row = db.execute('SELECT document,blob_hash FROM assets WHERE project_id=? AND id=?', (identifier(project_id), identifier(asset_id))).fetchone()
            require(row is not None, 'ASSET_NOT_FOUND')
            content = db.execute('SELECT content FROM blobs WHERE hash=?', (row['blob_hash'],)).fetchone()['content']
            require(sha(content) == row['blob_hash'], 'CORRUPT_ASSET')
            return json.loads(row['document']), io.BytesIO(content)

    def file_asset(self, db, project_id, revision_id, staged, name, media_type, verification, asset_id=None, document=None):
        self.cas.publish(db, staged)
        asset = document or dict(asset_id=asset_id or new_id('asset'), project_id=project_id, revision_id=revision_id,
            name=name, media_type=media_type, sha256=staged['sha256'], size=staged['size'], created_at=now(), verification=verification)
        db.execute('INSERT OR IGNORE INTO file_assets VALUES(?,?,?,?,?)',
                   (asset['asset_id'], project_id, revision_id, staged['sha256'], canonical(asset)))
        return asset

    def migrate_assets_to_cas(self):
        with self.connect() as db:
            ids = [(r['project_id'], r['id']) for r in db.execute('SELECT project_id,id FROM assets WHERE id NOT IN (SELECT id FROM file_assets)')]
        for pid, aid in ids:
            asset, stream = self.open_asset(pid, aid)
            with stream, self.cas.stage(stream) as staged, self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    self.file_asset(db, pid, asset['revision_id'], staged, asset['name'], asset['media_type'], asset['verification'], document=asset)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        return len(ids)

    def gc_assets(self, grace_seconds=3600):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                result = self.cas.gc(db, grace_seconds=grace_seconds)
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    def list_revisions(self, project_id):
        with self.connect() as db:
            self._project(db, project_id)
            return [json.loads(row['document']) for row in db.execute('SELECT document FROM revisions WHERE project_id=? ORDER BY rowid', (project_id,))]

    def events(self, project_id):
        with self.connect() as db:
            self._project(db, project_id)
            return [dict(sequence=row['sequence'], **json.loads(row['document']))
                    for row in db.execute('SELECT sequence,document FROM events WHERE project_id=? ORDER BY sequence', (project_id,))]
