"""Streaming local CAS. Publish and GC must hold the SAME SQLite writer lock.

Staging is deliberately excluded from online GC: a slow upload is not an orphan.
Objects are linked without replacement, so publication never overwrites content.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import time
import uuid


class CAS:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.objects = self.root / 'objects'
        self.staging = self.root / 'staging'
        self.objects.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        if self.objects.stat().st_dev != self.staging.stat().st_dev:
            raise ValueError('CAS staging and objects must be on the same volume')

    def path(self, digest):
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid content hash')
        path = self.objects / digest[:2] / digest
        if not path.resolve().is_relative_to(self.objects.resolve()):
            raise ValueError('CAS path escaped object root')
        return path

    @staticmethod
    def fingerprint(path):
        h, size = hashlib.sha256(), 0
        with open(path, 'rb') as stream:
            while chunk := stream.read(1024 * 1024):
                h.update(chunk)
                size += len(chunk)
        return h.hexdigest(), size

    @contextmanager
    def stage(self, stream, *, length=None, limit=1024 * 1024 * 1024):
        path = self.staging / (uuid.uuid4().hex + '.part')
        try:
            h, size = hashlib.sha256(), 0
            with path.open('xb') as target:
                while length is None or size < length:
                    chunk = stream.read(min(1024 * 1024, length - size) if length is not None else 1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('Asset too large')
                    target.write(chunk)
                    h.update(chunk)
                if not size or (length is not None and size != length):
                    raise ValueError('Empty or truncated asset')
                target.flush()
                os.fsync(target.fileno())
            yield dict(path=path, sha256=h.hexdigest(), size=size)
        finally:
            path.unlink(missing_ok=True)

    def publish(self, db, staged):
        if not db.in_transaction:
            raise RuntimeError('CAS publish requires the database writer transaction')
        if self.fingerprint(staged['path']) != (staged['sha256'], staged['size']):
            raise ValueError('Staging content changed')
        destination = self.path(staged['sha256'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if Path(staged['path']).stat().st_dev != destination.parent.stat().st_dev:
            raise ValueError('CAS publication requires a same-volume hard link')
        try:
            os.link(staged['path'], destination)
        except FileExistsError:
            if self.fingerprint(destination) != (staged['sha256'], staged['size']):
                raise ValueError('Existing CAS object is corrupt')
        # POSIX directory durability. Python has no equivalent directory fsync
        # on Windows; startup/read integrity checks detect missing/corrupt files.
        if os.name != 'nt':
            fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        db.execute('INSERT OR IGNORE INTO cas_objects VALUES(?,?)', (staged['sha256'], staged['size']))

    def open_verified(self, digest, size):
        handle = self.path(digest).open('rb')
        try:
            h, count = hashlib.sha256(), 0
            while chunk := handle.read(1024 * 1024):
                h.update(chunk)
                count += len(chunk)
            if (h.hexdigest(), count) != (digest, size):
                raise ValueError('CAS integrity failure')
            handle.seek(0)
            return handle
        except BaseException:
            handle.close()
            raise

    def gc(self, db, *, grace_seconds=3600):
        """Caller holds BEGIN IMMEDIATE, including throughout unlink and commit."""
        if not db.in_transaction or grace_seconds < 0:
            raise ValueError('GC requires writer transaction and nonnegative grace')
        referenced = {r[0] for r in db.execute('SELECT hash FROM file_assets')}
        removed = []
        for path in self.objects.glob('*/*'):
            if not re.fullmatch('[0-9a-f]{64}', path.name) or path != self.path(path.name):
                continue
            if path.name in referenced or time.time() - path.stat().st_mtime < grace_seconds:
                continue
            path.unlink()
            db.execute('DELETE FROM cas_objects WHERE hash=?', (path.name,))
            removed.append(path.name)
        return removed
