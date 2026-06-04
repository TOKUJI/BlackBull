import asyncio
import mimetypes
import os
import time
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
    # Per-entry stat throttle.  Once a cached entry is validated by
    # ``stat()``, skip the syscall on subsequent requests until the
    # monotonic clock has advanced this many seconds.  Default 1 s keeps
    # edit-on-disk visibility under a second while removing the per-
    # request stat from the cache-hit hot path.  Override via
    # ``BB_STATIC_STAT_TTL_S`` (env var, float seconds); set to ``0`` to
    # restore the pre-Sprint-33 every-request behaviour.
    _STAT_TTL_S = float(os.environ.get('BB_STATIC_STAT_TTL_S', '1.0'))

    # Server preference order for precompressed variant selection.
    # Matches blackbull.middleware.compression's order (br > zstd > gzip).
    _ENCODING_SUFFIXES: tuple[tuple[bytes, str], ...] = (
        (b'br',   '.br'),
        (b'zstd', '.zst'),
        (b'gzip', '.gz'),
    )

    def __init__(self, directory: str | None = None, *,
                 url_prefix: str = '', root_dir: str | Path | None = None):
        resolved = directory or root_dir
        if resolved is None:
            raise ValueError('directory or root_dir is required')
        self._root = Path(resolved).resolve()
        self._url_prefix = url_prefix.rstrip('/')
        # cache key = the actual file path served (original or sibling).
        # value = (mtime_ns, size, body, mime, content_encoding, last_stat).
        # content_encoding is b'' for uncompressed; b'br'/b'gzip'/b'zstd'
        # for precompressed siblings.  ``last_stat`` is the monotonic
        # clock when ``stat()`` last confirmed the entry was still fresh
        # — used to throttle the per-request stat syscall.
        self._cache: OrderedDict[
            Path, tuple[int, int, bytes, bytes, bytes, float]
        ] = OrderedDict()

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

    @staticmethod
    def _client_accepts(accept_header: bytes, encoding: bytes) -> bool:
        """Cheap Accept-Encoding parser — True iff `encoding` is offered with q>0."""
        if not accept_header:
            return False
        for token in accept_header.split(b','):
            parts = token.strip().split(b';')
            if parts[0].strip().lower() != encoding:
                continue
            for param in parts[1:]:
                p = param.strip()
                if p.startswith(b'q='):
                    try:
                        if float(p[2:]) <= 0:
                            return False
                    except ValueError:
                        pass
            return True
        return False

    def _negotiate(self, scope, target: Path) -> tuple[Path, bytes]:
        """Pick which file to serve and what Content-Encoding to advertise.

        Returns ``(path_to_serve, content_encoding)``.  `content_encoding`
        is ``b''`` for uncompressed; ``b'br'`` / ``b'zstd'`` / ``b'gzip'``
        when a precompressed sibling (``<path>.<suffix>``) was selected.

        Range requests bypass the precompressed-sibling lookup — encoded
        bodies have a different size than the original and serving a
        Range over an encoded variant is messy.  Matches what nginx
        does with ``gzip_static`` + Range.
        """
        accept = b''
        for k, v in scope.get('headers', []):
            kl = k.lower()
            if kl == b'range':
                return target, b''
            if kl == b'accept-encoding':
                accept = v.lower()
        if not accept:
            return target, b''
        for enc, suffix in self._ENCODING_SUFFIXES:
            if not self._client_accepts(accept, enc):
                continue
            sibling = target.with_name(target.name + suffix)
            if sibling.is_file():
                return sibling, enc
        return target, b''

    async def _serve(self, scope, send, path: Path):
        # Pick variant (precompressed sibling if available + accepted).
        served_path, content_encoding = self._negotiate(scope, path)

        body: bytes | None
        mime: bytes
        size: int

        # Fast path: cached entry, still within the per-entry stat TTL.
        # No syscall, no ``mimetypes.guess_type`` regex.
        cached_entry = self._cache.get(served_path)
        now = time.monotonic()
        if (cached_entry is not None
                and self._STAT_TTL_S > 0
                and now - cached_entry[5] < self._STAT_TTL_S):
            _, size, body, mime, _, _ = cached_entry
            self._cache.move_to_end(served_path)
        else:
            # Cache miss, stale TTL, or TTL disabled — re-stat to
            # confirm the entry is still valid.
            try:
                st = served_path.stat()
            except OSError:
                await self._respond(send, HTTPStatus.NOT_FOUND)
                return
            size = st.st_size
            mtime_ns = st.st_mtime_ns

            if (cached_entry is not None
                    and cached_entry[0] == mtime_ns
                    and cached_entry[1] == size):
                # Entry still matches the file on disk: reuse body + mime
                # and refresh ``last_stat`` so the next request can take
                # the fast path again.
                _, _, body, mime, _, _ = cached_entry
                self._cache[served_path] = (
                    mtime_ns, size, body, mime, content_encoding, now)
                self._cache.move_to_end(served_path)
            elif size <= self._CACHE_MAX_BYTES_PER_FILE:
                # First fill or stale entry — read once and store.
                # Content-Type derives from the original path's extension,
                # not the .br/.gz/.zst suffix — e.g. text/javascript for
                # app.js.br.
                mime = (mimetypes.guess_type(path.name)[0]
                        or 'application/octet-stream').encode()
                try:
                    with open(served_path, 'rb') as f:
                        body = f.read()
                except OSError:
                    await self._respond(send, HTTPStatus.NOT_FOUND)
                    return
                self._store(served_path, mtime_ns, size, body, mime,
                            content_encoding, now)
            else:
                # Above the cache threshold — drop any stale entry and
                # fall through to the streaming/pathsend branch.
                self._cache.pop(served_path, None)
                mime = (mimetypes.guess_type(path.name)[0]
                        or 'application/octet-stream').encode()
                body = None

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

        if content_encoding:
            # When we negotiated a precompressed variant, tell the client
            # how it's encoded and that the response Varies on
            # Accept-Encoding (so HTTP caches don't mis-cache).
            extra_headers.append((b'content-encoding', content_encoding))
            extra_headers.append((b'vary', b'Accept-Encoding'))

        if body is not None:
            # Cache-hit (or just-filled) fast path: two send() calls, no
            # thread-pool dispatch.  Slicing a bytes object is cheap and
            # the slice doesn't escape this coroutine.
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
        # threshold.  Two variants:
        #
        # 1. ``http.response.pathsend`` ASGI extension is advertised by
        #    the server AND this is a full-file response (no Range).
        #    Hand the file path to the sender; HTTP1Sender calls
        #    ``loop.sendfile`` for zero-copy delivery — no per-chunk
        #    event-loop dispatch (vs. ~64 µs/chunk × 256 = 16 ms wasted
        #    on a 16 MiB transfer through the fallback path).
        #
        # 2. Fallback chunked streaming through ``asyncio.to_thread``.
        #    Used for TLS (kernel sendfile can't see plaintext), HTTP/2
        #    (h2 frames in user-space), Range requests (pathsend extension
        #    doesn't carry offset/count), and any server that doesn't
        #    advertise the extension.
        pathsend_ok = (status != HTTPStatus.PARTIAL_CONTENT
                       and 'http.response.pathsend' in scope.get('extensions', {}))

        await send({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': status,
                    'headers': [
                        (b'content-type', mime),
                        (b'content-length', str(body_len).encode()),
                        *extra_headers,
                    ]})

        if pathsend_ok:
            await send({'type': ASGIEvent.HTTP_RESPONSE_PATHSEND,
                        'path': str(served_path)})
            return

        remaining = body_len
        fobj = await asyncio.to_thread(open, str(served_path), 'rb')
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

    def _store(self, path: Path, mtime_ns: int, size: int,
               body: bytes, mime: bytes, content_encoding: bytes,
               last_stat: float):
        self._cache[path] = (mtime_ns, size, body, mime, content_encoding,
                             last_stat)
        self._cache.move_to_end(path)
        while len(self._cache) > self._CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    @staticmethod
    async def _respond(send, status: int, extra_headers=None):
        await send({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': status,
                    'headers': extra_headers or []})
        await send({'type': ASGIEvent.HTTP_RESPONSE_BODY, 'body': b''})
