"""Fetch pinned text-only skill resources, verifying hashes before installation."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tempfile
import shutil
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1] / 'integrations/music-skills'


def main():
    manifest = json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
    for lib in manifest['libraries']:
        name, commit = lib['repository_name'], lib['commit']
        url = lib['repository'].replace('https://github.com/', 'https://codeload.github.com/') + '/zip/' + commit
        with urlopen(url, timeout=60) as response:
            archive = zipfile.ZipFile(io.BytesIO(response.read()))
        prefix = name + '-' + commit + '/'
        skill_prefix = 'plugins/' + lib['plugin'] + '/skills/'
        wanted = {skill_prefix + relative: digest for relative, digest in lib['files'].items()}
        wanted.update({'LICENSE': None, 'NOTICE': None})
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)/name
            for relative, digest in wanted.items():
                relative_path = PurePosixPath(relative)
                if relative_path.is_absolute() or '..' in relative_path.parts or chr(92) in relative:
                    raise ValueError('Unsafe manifest path')
                raw = archive.read(prefix + relative)
                if digest and hashlib.sha256(raw).hexdigest() != digest:
                    # The original reviewed lock was produced by a Windows Git
                    # checkout. Reproduce its CRLF bytes only if that EXACT hash
                    # matches; never accept a different text or update the pin.
                    checkout_bytes = raw.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
                    if hashlib.sha256(checkout_bytes).hexdigest() != digest:
                        raise ValueError('Skill hash mismatch: ' + relative)
                    raw = checkout_bytes
                dest = stage.joinpath(*relative_path.parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
            destination = ROOT/'vendor'/name
            if destination.exists():
                for item in stage.rglob('*'):
                    if item.is_file():
                        existing = destination/item.relative_to(stage)
                        if not existing.is_file() or existing.read_bytes() != item.read_bytes():
                            raise ValueError('Existing installation differs; resolve manually: ' + str(existing))
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(stage, destination)
        print(name + ': verified ' + commit)
    print('Professional music skills ready.')


if __name__ == '__main__':
    main()
