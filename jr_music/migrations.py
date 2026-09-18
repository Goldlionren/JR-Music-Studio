"""Explicit local SQLite migrations; legacy idempotency results remain replayable."""
from datetime import datetime, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def operation_scope(payload):
    op = payload['op']
    if op == 'create_project':
        return 'projects', op
    scope = 'project:' + payload['project_id']
    if op in ('archive_asset', 'derive_score', 'create_edit_request', 'apply_edit', 'prepare_render'):
        scope += '/revision:' + payload['revision_id']
    if payload.get('render_id') is not None:
        scope += '/render:' + payload['render_id']
    if 'experiment_id' in payload:
        scope += '/experiment:' + payload['experiment_id']
    return scope, op


def legacy_scope(result):
    if result.get('kind') == 'project':
        return 'projects', 'create_project'
    if result.get('kind') == 'opaque_revision':
        return 'project:' + result['project_id'], 'add_revision'
    if 'decision' in result:
        return 'project:' + result['decision']['project_id'], 'decide'
    if 'asset_id' in result:
        return 'project:' + result['project_id'] + '/revision:' + result['revision_id'], 'archive_asset'
    raise ValueError('Cannot safely infer legacy idempotency scope; migration aborted')


def migrate(db, database):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version in (2, 3, 4, 5, 6, 7):
        return
    if version != 0:
        raise ValueError(f'Unsupported database version: {version}')
    # This release requires stopping the old service before migration.
    if db.execute('SELECT count(*) FROM operations').fetchone()[0]:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = Path(str(database) + '.pre-m1-03-' + stamp + '.bak')
        with closing(sqlite3.connect(backup)) as target:
            db.backup(target)
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('PRAGMA user_version').fetchone()[0] == 2:
            db.commit()
            return
        rows = db.execute('SELECT * FROM operations').fetchall()
        db.execute('ALTER TABLE operations RENAME TO operations_legacy')
        db.execute('''CREATE TABLE operations(
            actor TEXT NOT NULL, scope TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
            fingerprint TEXT NOT NULL, result BLOB NOT NULL, PRIMARY KEY(actor,scope,operation,key))''')
        for row in rows:
            scope, operation = legacy_scope(json.loads(row['result']))
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?)',
                       (row['actor'], scope, operation, row['key'], row['fingerprint'], row['result']))
        db.execute('''CREATE TABLE score_representations(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            revision_id TEXT NOT NULL REFERENCES revisions(id), source_hash TEXT NOT NULL,
            parser_version TEXT NOT NULL, document BLOB NOT NULL,
            UNIQUE(revision_id,source_hash,parser_version))''')
        db.execute('''CREATE TABLE edit_requests(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            revision_id TEXT NOT NULL REFERENCES revisions(id), document BLOB NOT NULL)''')
        db.execute('PRAGMA user_version=2')
        db.commit()
    except BaseException:
        db.rollback()
        raise


def migrate_render(db, database):
    if db.execute('PRAGMA user_version').fetchone()[0] in (3, 4, 5, 6, 7):
        return
    # Old assets/results stay byte-identical. Files are migrated explicitly after
    # this schema migration, with per-object hashes and legacy BLOB retention.
    if db.execute('SELECT count(*) FROM revisions').fetchone()[0]:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        with closing(sqlite3.connect(str(database) + '.pre-m1-04-' + stamp + '.bak')) as target:
            db.backup(target)
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('PRAGMA user_version').fetchone()[0] == 3:
            db.commit()
            return
        db.execute('CREATE TABLE cas_objects(hash TEXT PRIMARY KEY, size INTEGER NOT NULL)')
        db.execute('''CREATE TABLE file_assets(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            revision_id TEXT NOT NULL REFERENCES revisions(id), hash TEXT NOT NULL REFERENCES cas_objects(hash), document BLOB NOT NULL)''')
        db.execute('''CREATE TABLE render_jobs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            revision_id TEXT NOT NULL REFERENCES revisions(id), server_id TEXT NOT NULL, prompt_id TEXT,
            document BLOB NOT NULL, UNIQUE(server_id,prompt_id))''')
        db.execute('PRAGMA user_version=3')
        db.commit()
    except BaseException:
        db.rollback()
        raise


def migrate_edit_verification(db, database):
    if db.execute('PRAGMA user_version').fetchone()[0] in (4, 5, 6, 7):
        return
    if db.execute('PRAGMA user_version').fetchone()[0] != 3:
        raise ValueError('Edit verification migration requires database version 3')
    if db.execute('SELECT count(*) FROM revisions').fetchone()[0]:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        with closing(sqlite3.connect(str(database) + '.pre-m1-03b-' + stamp + '.bak')) as target:
            db.backup(target)
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('PRAGMA user_version').fetchone()[0] == 4:
            db.commit()
            return
        db.execute('''CREATE TABLE edit_verifications(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            source_revision_id TEXT NOT NULL REFERENCES revisions(id),
            result_revision_id TEXT NOT NULL UNIQUE REFERENCES revisions(id),
            request_id TEXT NOT NULL REFERENCES edit_requests(id),
            asset_id TEXT NOT NULL UNIQUE REFERENCES assets(id),
            manifest_hash TEXT NOT NULL REFERENCES blobs(hash), document BLOB NOT NULL)''')
        db.execute('PRAGMA user_version=4')
        db.commit()
    except BaseException:
        db.rollback()
        raise


def migrate_music(db, database):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version in (5, 6, 7):
        return
    if version != 4:
        raise ValueError('Music foundation migration requires database version 4')
    if db.execute('SELECT count(*) FROM revisions').fetchone()[0]:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        with closing(sqlite3.connect(str(database) + '.pre-m1-05a-' + stamp + '.bak')) as target:
            db.backup(target)
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('PRAGMA user_version').fetchone()[0] == 5:
            db.commit()
            return
        db.execute('''CREATE TABLE master_bundles(master_id TEXT NOT NULL, version TEXT NOT NULL,
            blob_hash TEXT NOT NULL REFERENCES blobs(hash), PRIMARY KEY(master_id,version))''')
        db.execute('''CREATE TABLE music_experiments(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            blob_hash TEXT NOT NULL REFERENCES blobs(hash))''')
        db.execute('''CREATE TABLE music_candidates(experiment_id TEXT NOT NULL REFERENCES music_experiments(id),
            variant TEXT NOT NULL CHECK(variant IN ('baseline','master')), revision_id TEXT NOT NULL UNIQUE REFERENCES revisions(id),
            blob_hash TEXT NOT NULL REFERENCES blobs(hash), PRIMARY KEY(experiment_id,variant))''')
        db.execute('''CREATE TABLE music_reviews(id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, variant TEXT NOT NULL,
            blob_hash TEXT NOT NULL REFERENCES blobs(hash),
            FOREIGN KEY(experiment_id,variant) REFERENCES music_candidates(experiment_id,variant))''')
        db.execute('''CREATE TABLE music_feedback(id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES music_experiments(id),
            blob_hash TEXT NOT NULL REFERENCES blobs(hash))''')
        db.execute('PRAGMA user_version=5')
        db.commit()
    except BaseException:
        db.rollback()
        raise


def migrate_assessments(db,database):
    version=db.execute('PRAGMA user_version').fetchone()[0]
    if version in (6, 7):return
    if version!=5:raise ValueError('Assessment migration requires version 5')
    if db.execute('SELECT count(*) FROM revisions').fetchone()[0]:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        with closing(sqlite3.connect(str(database)+'.pre-m1-05b-'+stamp+'.bak')) as target:db.backup(target)
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('PRAGMA user_version').fetchone()[0]==6:
            db.commit();return
        db.execute('''CREATE TABLE music_assessments(id TEXT PRIMARY KEY,
            experiment_id TEXT NOT NULL REFERENCES music_experiments(id),
            kind TEXT NOT NULL, blob_hash TEXT NOT NULL REFERENCES blobs(hash))''')
        db.execute('PRAGMA user_version=6');db.commit()
    except BaseException:
        db.rollback();raise


def migrate_three_arm(db, database):
    """SQLite documented table rebuild; preserve rowids, blobs and referring FKs."""
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version == 7: return
    if version != 6: raise ValueError('Three-arm migration requires version 6')
    if db.execute('SELECT count(*) FROM revisions').fetchone()[0]:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        with closing(sqlite3.connect(str(database) + '.pre-m1-05b-v11-' + stamp + '.bak')) as target:
            db.backup(target)
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute('PRAGMA user_version').fetchone()[0] == 7:
            db.commit(); return
        db.execute('''CREATE TABLE music_candidates_new(experiment_id TEXT NOT NULL REFERENCES music_experiments(id),
            variant TEXT NOT NULL CHECK(variant IN ('baseline','master','master_v11')),
            revision_id TEXT NOT NULL UNIQUE REFERENCES revisions(id),
            blob_hash TEXT NOT NULL REFERENCES blobs(hash), PRIMARY KEY(experiment_id,variant))''')
        db.execute('INSERT INTO music_candidates_new(rowid,experiment_id,variant,revision_id,blob_hash) SELECT rowid,* FROM music_candidates')
        db.execute('DROP TABLE music_candidates')
        db.execute('ALTER TABLE music_candidates_new RENAME TO music_candidates')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Foreign key check failed during three-arm migration')
        db.execute('PRAGMA user_version=7')
        db.commit()
    except BaseException:
        db.rollback(); raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')
