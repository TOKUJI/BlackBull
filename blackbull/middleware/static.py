import asyncio
import mimetypes
from collections import OrderedDict
from pathlib import Path
from urllib.parse import unquote
from http import HTTPStatus

from blackbull.env import get_settings, Environment
from blackbull.asgi import ASGIEvent


class StaticFiles:
    # Files at or below this size are read once and held in memory.
    # Static assets in the wild (CSS/JS/manifest/small images) cluster
    # well under this; larger files fall through to streaming.
    _CACHE_MAX_BYTES_PER_FILE = 4 * 1024 * 1024
    _CACHE_MAX_ENTRIES = 256
    # 64 KiB streaming chunk for files above the cache threshold.
    _CHUNK = 64 * 1024

    def __init__(self, directory: str | None = None, *,
                 url_prefix: str = '', root_dir: str | Path | None = None):
        resolved = directory or root_dir
        if resolved is None:
            raise ValueError('directory or root_dir is required')
        self._root = Path(resolved).resolve()
        self._url_prefix = url_prefix.rstrip('/')
        # path -> (mtime_ns, size, body, mime_bytes).  LRU via OrderedDict.
        self._cache: OrderedDict[Path, tuple[int, int, bytes, bytes]] = OrderedDict()

    async def __call__(self, scope, receive, send, call_next=None):
        if scope.get('type') != 'http' or scope.get('method') not in ('GET', 'HEAD'):
            if call_next:
                await call_next(scope, receive, send)
            else:
                await self._respond(send, HTTPStatus.NOT_FOUND)
            return

        if get_settings().env == Environment.PRODUCTION:
            if call_next:
                await call_next(scope, receive, send)
            else:
                await self._respond(send, HTTPStatus.NOT_FOUND)
            return

        raw_path = scope.get('path', '/')

        if self._url_prefix:
            if not raw_path.startswith(self._url_prefix):
                if call_next:
                    await call_next(scope, receive, send)
                else:
                    await self._respond(send, HTTPStatus.NOT_FOUND)
                return
            raw_path = raw_path[len(self._url_prefix):]

        decoded = unquote(raw_path)
        try:
            target = (self._root / decoded.lstrip('/')).resolve()
            target.relative_to(self._root)
        except ValueError:
            await self._respond(send, HTTPStatus.BAD_REQUEST)
            return

        if not target.is_file():
            if call_next:
                await call_next(scope, receive, send)
            else:
                await self._respond(send, HTTPStatus.NOT_FOUND)
            return

        await self._serve(scope, send, target)

    async def _serve(self, scope, send, path: Path):
        # stat() is one cheap syscall (~µs).  Running it sync in the event
        # loop avoids the asyncio thread-pool dispatch that became the
        # bottleneck under HttpArena's c=1024-6800 load (Sprint 28).
        try:
            st = path.stat()
        except OSError:
            await self._respond(send, HTTPStatus.NOT_FOUND)
            return
        size = st.st_size
        mtime_ns = st.st_mtime_ns

        body, mime = self._lookup(path, mtime_ns, size)
        if body is None and size <= self._CACHE_MAX_BYTES_PER_FILE:
            try:
                with open(path, 'rb') as f:
                    body = f.read()
            except OSError:
                await self._respond(send, HTTPStatus.NOT_FOUND)
                return
            mime = (mimetypes.guess_type(path.name)[0]
                    or 'application/octet-stream').encode()
            self._store(path, mtime_ns, size, body, mime)

        range_hdr = None
        for k, v in scope.get('headers', []):
            if k.lower() == b'range':
                range_hdr = v.decode()
                break

        start, end = 0, size - 1
        status = HTTPStatus.OK
        extra_headers: list[tuple[bytes, bytes]] = []

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
                await self._respond(send, HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    [(b'content-range', f'bytes */{size}'.encode())])
                return

            status = HTTPStatus.PARTIAL_CONTENT
            extra_headers.append(
                (b'content-range', f'bytes {start}-{end}/{size}'.encode()))

        body_len = end - start + 1

        if body is not None:
            # Cache-hit (or just-filled) fast path: two send() calls, no
            # thread-pool dispatch.  Slicing a bytes object is cheap and
            # the slice doesn't escape this coroutine.
            assert mime is not None
            await send({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': status,
                        'headers': [
                            (b'content-type', mime),
                            (b'content-length', str(body_len).encode()),
                            *extra_headers,
                        ]})
            chunk = body[start:end + 1] if (start or end != size - 1) else body
            await send({'type': ASGIEvent.HTTP_RESPONSE_BODY,
                        'body': chunk, 'more_body': False})
            return

        # Large-file streaming path — only hit when size exceeds the cache
        # threshold.  Goes through the asyncio thread pool one chunk at a
        # time so per-request peak memory stays at _CHUNK regardless of
        # body size.
        mime = (mimetypes.guess_type(path.name)[0]
                or 'application/octet-stream').encode()
        await send({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': status,
                    'headers': [
                        (b'content-type', mime),
                        (b'content-length', str(body_len).encode()),
                        *extra_headers,
                    ]})

        remaining = body_len
        fobj = await asyncio.to_thread(open, str(path), 'rb')
        try:
            if start:
                await asyncio.to_thread(fobj.seek, start)
            while remaining > 0:
                want = min(self._CHUNK, remaining)
                chunk = await asyncio.to_thread(fobj.read, want)
                if not chunk:
                    break
                remaining -= len(chunk)
                await send({'type': ASGIEvent.HTTP_RESPONSE_BODY,
                            'body': chunk, 'more_body': remaining > 0})
        finally:
            await asyncio.to_thread(fobj.close)

    def _lookup(self, path: Path, mtime_ns: int, size: int):
        entry = self._cache.get(path)
        if entry is None:
            return None, None
        if entry[0] != mtime_ns or entry[1] != size:
            # stale — drop and force refill
            self._cache.pop(path, None)
            return None, None
        # mark as recently used
        self._cache.move_to_end(path)
        return entry[2], entry[3]

    def _store(self, path: Path, mtime_ns: int, size: int,
               body: bytes, mime: bytes):
        self._cache[path] = (mtime_ns, size, body, mime)
        self._cache.move_to_end(path)
        while len(self._cache) > self._CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    @staticmethod
    async def _respond(send, status: int, extra_headers=None):
        await send({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': status,
                    'headers': extra_headers or []})
        await send({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b''})
