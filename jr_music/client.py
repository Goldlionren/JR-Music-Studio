"""Standalone stdlib JR client for Hermes. No automatic POST retries.

Copy this file to the Hermes host; no repository or Python packages required.
Credentials are read from JR_MUSIC_CONFIG, never command-line token arguments.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


class ClientError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ClientError('REDIRECT_DENIED')


class Client:
    def __init__(self, url, token, *, timeout=180):
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment or
            parsed.path not in ('', '/') or
            (parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', '::1', 'localhost'))):
            raise ClientError('USE_HTTPS_OR_LOOPBACK_SSH_TUNNEL')
        if not isinstance(token, str) or len(token) < 32 or '\n' in token or '\r' in token:
            raise ClientError('INVALID_CREDENTIAL')
        self.url, self.token, self.timeout = url.rstrip('/'), token, timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def open(self, method, path, body=None):
        if not re.fullmatch(r'/[a-zA-Z0-9_/-]+', path) or '//' in path:
            raise ClientError('INVALID_ROUTE')
        data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        request = Request(self.url + path, data=data, method=method,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            return self.opener.open(request, timeout=self.timeout)
        except HTTPError as exc:
            with exc:
                try:
                    code = json.loads(exc.read(65536)).get('error', {}).get('code')
                except (ValueError, AttributeError):
                    code = None
            # Only structured codes, never dump arbitrary upstream responses.
            if not isinstance(code, str) or not re.fullmatch('[A-Z0-9_]{1,96}', code):
                code = 'HTTP_' + str(exc.code)
            raise ClientError(code) from None
        except (OSError, URLError) as exc:
            raise ClientError('CONNECTION_UNCERTAIN_DO_NOT_REPEAT_DISPATCH') from None

    def request(self, method, path, body=None):
        with self.open(method, path, body) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ClientError('RESPONSE_TOO_LARGE')
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get('ok') is not True or 'data' not in result:
            raise ClientError('INVALID_RESPONSE')
        return result['data']

    def download(self, asset, destination):
        for name in ('project_id', 'asset_id'):
            if not re.fullmatch('[a-z][a-z0-9_-]{0,95}', asset.get(name, '')):
                raise ClientError('INVALID_ASSET')
        if not re.fullmatch('[0-9a-f]{64}', asset.get('sha256', '')) or type(asset.get('size')) is not int or not 0 < asset['size'] <= 1024**3:
            raise ClientError('INVALID_ASSET')
        destination = Path(destination)
        if destination.exists():
            raise ClientError('DESTINATION_EXISTS')
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.jr-download-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'wb') as out, self.open('GET', f'/projects/{asset["project_id"]}/assets/{asset["asset_id"]}') as response:
                count, digest = 0, hashlib.sha256()
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > asset['size']:
                        raise ClientError('TRANSFER_SIZE_MISMATCH')
                    digest.update(chunk)
                    out.write(chunk)
                if count != asset['size'] or digest.hexdigest() != asset['sha256']:
                    raise ClientError('TRANSFER_HASH_MISMATCH')
                out.flush()
                os.fsync(out.fileno())
            # Atomic publication without clobbering another download.
            os.link(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return dict(path=str(destination.resolve()), sha256=asset['sha256'], size=asset['size'])


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--config', default=os.environ.get('JR_MUSIC_CONFIG'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    mcp = sub.add_parser('mcp',help='List/refresh Hermes MCP servers or set this agent default')
    mcp.add_argument('action',choices=['list','refresh','default'])
    mcp.add_argument('alias',nargs='?')
    req = sub.add_parser('request')
    req.add_argument('method', choices=['GET', 'POST'])
    req.add_argument('path')
    req.add_argument('--body', type=Path)
    req.add_argument('--output', type=Path)
    dl = sub.add_parser('download')
    dl.add_argument('--asset', type=Path, required=True)
    dl.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if not args.config:
            raise ClientError('JR_MUSIC_CONFIG_REQUIRED')
        config = json.loads(Path(args.config).read_text(encoding='utf-8'))
        client = Client(config['url'], config['token'])
        # Explicit identity check protects against accidentally installing a
        # producer credential in an agent's config.
        health = client.request('GET', '/health')
        if health.get('role') != 'agent' or health.get('actor_id') != config['actor_id']:
            raise ClientError('AGENT_IDENTITY_MISMATCH')
        if args.command == 'doctor':
            result = dict(health=health, integration=client.request('GET', '/integration'))
        elif args.command == 'mcp':
            if args.action=='default':
                if not args.alias:raise ClientError('MCP_ALIAS_REQUIRED')
                result=client.request('POST','/mcp-servers/default',dict(mcp_alias=args.alias))
            elif args.alias:raise ClientError('UNEXPECTED_MCP_ALIAS')
            elif args.action=='refresh':result=client.request('POST','/mcp-servers/refresh',{})
            else:result=client.request('GET','/mcp-servers')
        elif args.command == 'request':
            body = json.loads(args.body.read_text(encoding='utf-8')) if args.body else None
            result = client.request(args.method, args.path, body)
        else:
            result = client.download(json.loads(args.asset.read_text(encoding='utf-8')), args.output)
        if args.command == 'request' and args.output:
            with args.output.open('x', encoding='utf-8') as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
            result = dict(saved=str(args.output))
        print(json.dumps(dict(ok=True, data=result), ensure_ascii=True))
    except (ClientError, OSError, ValueError, KeyError, TypeError):
        error = sys.exception()
        print(json.dumps(dict(ok=False, error=str(error) if isinstance(error, ClientError) else 'CLIENT_INPUT_OR_IO_ERROR')))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
