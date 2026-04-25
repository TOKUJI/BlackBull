import mimetypes
from pathlib import Path
from urllib.parse import unquote

from blackbull.env import get_env, Environment


class StaticFiles:
    def __init__(self, directory: str | None = None, *,
                 url_prefix: str = '', root_dir: str | Path | None = None):
        resolved = directory or root_dir
        if resolved is None:
            raise ValueError('directory or root_dir is required')
        self._root = Path(resolved).resolve()
        self._url_prefix = url_prefix.rstrip('/')

    async def __call__(self, scope, receive, send, call_next=None):
        if scope.get('type') != 'http' or scope.get('method') not in ('GET', 'HEAD'):
            if call_next:
                await call_next(scope, receive, send)
            else:
                await self._respond(send, 404)
            return

        if get_env() == Environment.PRODUCTION:
            if call_next:
                await call_next(scope, receive, send)
            else:
                await self._respond(send, 404)
            return

        raw_path = scope.get('path', '/')

        if self._url_prefix:
            if not raw_path.startswith(self._url_prefix):
                if call_next:
                    await call_next(scope, receive, send)
                else:
                    await self._respond(send, 404)
                return
            raw_path = raw_path[len(self._url_prefix):]

        decoded = unquote(raw_path)
        try:
            target = (self._root / decoded.lstrip('/')).resolve()
            target.relative_to(self._root)
        except ValueError:
            await self._respond(send, 400)
            return

        if not target.is_file():
            if call_next:
                await call_next(scope, receive, send)
            else:
                await self._respond(send, 404)
            return

        await self._serve(scope, send, target)

    async def _serve(self, scope, send, path: Path):
        data = path.read_bytes()
        size = len(data)
        mime = (mimetypes.guess_type(path.name)[0]
                or 'application/octet-stream').encode()

        range_hdr = None
        for k, v in scope.get('headers', []):
            if k.lower() == b'range':
                range_hdr = v.decode()
                break

        if range_hdr and range_hdr.startswith('bytes='):
            spec = range_hdr[6:]
            start_s, _, end_s = spec.partition('-')
            if start_s == '':
                n = int(end_s)
                start, end = max(0, size - n), size - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1

            if start >= size or end >= size or start > end:
                await self._respond(send, 416,
                    [(b'content-range', f'bytes */{size}'.encode())])
                return

            chunk = data[start:end + 1]
            await send({'type': 'http.response.start', 'status': 206, 'headers': [
                (b'content-type', mime),
                (b'content-length', str(len(chunk)).encode()),
                (b'content-range', f'bytes {start}-{end}/{size}'.encode()),
            ]})
            await send({'type': 'http.response.body', 'body': chunk})
            return

        await send({'type': 'http.response.start', 'status': 200, 'headers': [
            (b'content-type', mime),
            (b'content-length', str(size).encode()),
        ]})
        await send({'type': 'http.response.body', 'body': data})

    @staticmethod
    async def _respond(send, status: int, extra_headers=None):
        await send({'type': 'http.response.start', 'status': status,
                    'headers': extra_headers or []})
        await send({'type': 'http.response.body', 'body': b''})
