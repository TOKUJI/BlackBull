import asyncio
import mimetypes
import os
import time
from collections import OrderedDict
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote
from http import HTTPStatus

from blackbull.connection import Connection
from blackbull.env import get_settings, Environment
from blackbull.asgi import ASGIEvent


# Common web-asset MIME types that may be missing from the host's
# ``/etc/mime.types`` file.  Without this registration, slim container
# images (e.g. ``python:3.13-slim`` ships no mime-support package) make
# ``mimetypes.guess_type('foo.woff2')`` return ``None``; StaticFiles
# falls back to ``application/octet-stream``; downstream Compression
# middleware then runs brotli on already-compressed font/image bytes —
# Sprint 35 phase-trace traced this to a 30-60 ms per-request CPU tail
# on HttpArena's ``regular.woff2`` and ``bold.woff2`` files.
#
# ``mimetypes.add_type`` is idempotent, runs once at module import, and
# integrates with the standard machinery so ``mimetypes.guess_type``
# returns the right answer everywhere — including for callers other
# than StaticFiles.  Keep this list conservative: only entries that are
# both (a) commonly served by static-files mounts and (b) standardised
# IANA / WHATWG types.
for _ext, _mime in (
    ('.woff',  'font/woff'),
    ('.woff2', 'font/woff2'),
    ('.webp',  'image/webp'),
    ('.avif',  'image/avif'),
    ('.wasm',  'application/wasm'),
):
    mimetypes.add_type(_mime, _ext)
del _ext, _mime


def _parse_byte_range(range_hdr: str, size: int) -> tuple[int, int] | None:
    """Parse a single ``bytes=`` Range header into an inclusive ``(start, end)``.

    Returns ``None`` for anything we do not answer with a partial response —
    a non-``bytes`` unit, a multi-range set (we don't emit
    ``multipart/byteranges``), or a syntactically malformed range.  Per
    RFC 9110 §14.2 an unparseable/ignored Range is served as a normal 200,
    so the caller treats ``None`` as "serve the whole file".  Never raises:
    the pre-Sprint-69 code ran bare ``int()`` here and 500'd on
    ``Range: bytes=abc-def`` (bug 1.21c).  *Satisfiability* against ``size``
    is still checked by the caller (so an out-of-range spec stays a 416).
    """
    if not range_hdr.startswith('bytes='):
        return None
    spec = range_hdr[6:].strip()
    if not spec or ',' in spec:
        return None
    start_s, sep, end_s = spec.partition('-')
    if not sep:
        return None
    start_s, end_s = start_s.strip(), end_s.strip()
    try:
        if start_s == '':
            # Suffix range: bytes=-N → the last N bytes.
            if end_s == '':
                return None
            n = int(end_s)
            return (max(0, size - n), size - 1)
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    return (start, end)


def _not_modified(headers, etag: bytes, mtime_ns: int) -> bool:
    """Evaluate the conditional-GET preconditions (RFC 9110 §13).

    ``If-None-Match`` takes precedence over ``If-Modified-Since``; when the
    former is present the latter is ignored.  Returns ``True`` when the
    client's cached copy is still fresh and the caller should answer 304.
    """
    inm = None
    ims = None
    for k, v in headers:
        kl = k.lower()
        if kl == b'if-none-match':
            inm = v
        elif kl == b'if-modified-since':
            ims = v
    if inm is not None:
        candidate = inm.strip()
        if candidate == b'*':
            return True
        target = etag[2:] if etag.startswith(b'W/') else etag
        for tag in candidate.split(b','):
            t = tag.strip()
            if t.startswith(b'W/'):
                t = t[2:]
            if t == target:
                return True
        # If-None-Match present but no match → not fresh; ignore IMS.
        return False
    if ims is not None:
        try:
            ims_dt = parsedate_to_datetime(ims.decode('latin-1'))
        except (ValueError, TypeError):
            return False
        if ims_dt is None:
            return False
        try:
            ims_ts = ims_dt.timestamp()
        except (ValueError, OverflowError, OSError):
            return False
        # HTTP dates carry 1-second granularity; compare at whole seconds.
        return mtime_ns // 1_000_000_000 <= int(ims_ts)
    return False


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
                 url_prefix: str = '', root_dir: str | Path | None = None,
                 cache: bool = False, index: str | None = None,
                 conditional: bool = True):
        """Serve files from ``directory`` (or ``root_dir``).

        ``index`` (default ``None`` — off): when set to a filename (e.g.
        ``'index.html'``), a request that resolves to a *directory* is
        served that file from inside the directory if it exists.  Off by
        default so existing exact-path serving is unchanged; the
        ``blackbull serve`` CLI turns it on to match ``python -m
        http.server``'s directory-index behaviour.

        ``cache`` (default ``False``): when ``True``, file bodies up to
        ``_CACHE_MAX_BYTES_PER_FILE`` are held in an in-memory
        ``OrderedDict`` (capped at ``_CACHE_MAX_ENTRIES``), and the
        per-request ``stat()`` syscall is throttled by
        ``_STAT_TTL_S``.  When ``False`` (the default), every request
        does a fresh ``stat()`` and reads the body from disk — matching
        the behaviour of Starlette / FastAPI / Flask static serving and
        the requirement HttpArena's standard-mode rules place on static
        profiles ("read files from disk on every request, no in-memory
        caching").  Set ``cache=True`` only for standalone deployments
        where BlackBull terminates static traffic directly (i.e. no
        nginx / CDN in front).

        ``conditional`` (default ``True``): emit ``ETag`` + ``Last-Modified``
        validators and honour ``If-None-Match`` / ``If-Modified-Since`` with a
        304.  Set ``False`` to suppress validators (e.g. the ``blackbull
        serve --no-etag`` path).
        """
        resolved = directory or root_dir
        if resolved is None:
            raise ValueError('directory or root_dir is required')
        # Internal hot path uses ``str`` + ``os.path`` rather than
        # ``pathlib.Path``: each request previously allocated several
        # PurePath / Path objects for the same traversal-safety check
        # and showed up under ``mw_static_in → static_pre_send`` in the
        # Sprint 33 phase trace.  ``os.path`` is a thin C wrapper.
        self._root_str: str = os.path.realpath(os.fspath(resolved))
        # Pre-computed prefix for the traversal check — accept
        # ``<root>/...`` exactly, reject ``<root>x/...``.
        self._root_sep: str = self._root_str + os.sep
        self._url_prefix = url_prefix.rstrip('/')
        self._cache_enabled: bool = cache
        self._index: str | None = index
        self._conditional: bool = conditional
        # cache key = the actual filesystem path served (original or
        # sibling), held as a ``str`` so the hash is cheap and the
        # key matches the value returned by ``os.path.realpath``.
        # value = (mtime_ns, size, body, mime, content_encoding, last_stat).
        # content_encoding is b'' for uncompressed; b'br'/b'gzip'/b'zstd'
        # for precompressed siblings.  ``last_stat`` is the monotonic
        # clock when ``stat()`` last confirmed the entry was still fresh
        # — used to throttle the per-request stat syscall.
        # Allocated even when caching is disabled so the read sites can
        # check membership without a None-guard; the cache simply never
        # gets populated.
        self._cache: OrderedDict[
            str, tuple[int, int, bytes, bytes, bytes, float]
        ] = OrderedDict()
        # Per-path sibling-availability cache: target → {b'br': sibling_path, ...}.
        # _negotiate calls os.path.isfile for each encoding suffix on every
        # request; when caching is enabled we memoise the answer after the
        # first lookup (deterministic for the lifetime of the server).
        # When caching is disabled we recompute siblings every request so
        # the from-disk-every-request contract extends to the sibling
        # existence check.
        # Key = original request path string, Value = dict of available encodings.
        self._sibling_cache: dict[str, dict[bytes, str]] = {}

    @property
    def _root(self) -> Path:
        """Backwards-compat: pre-Sprint-33 callers and tests may inspect
        ``staticfiles._root`` as a :class:`Path`.  Built on demand so
        the hot path keeps its plain-string representation."""
        return Path(self._root_str)

    async def __call__(self, conn, receive, send, call_next=None):
        # BlackBull threads a native Connection for HTTP; a WebSocket/non-HTTP
        # scope dict (only possible in the middleware role) passes through.
        if (not isinstance(conn, Connection) or conn.type != 'http'
                or conn.method not in ('GET', 'HEAD')):
            if call_next:
                await call_next(conn, receive, send)
            else:
                await self._respond(send, HTTPStatus.NOT_FOUND)
            return

        if get_settings().env == Environment.PRODUCTION:
            if call_next:
                await call_next(conn, receive, send)
            else:
                await self._respond(send, HTTPStatus.NOT_FOUND)
            return

        raw_path = conn.path

        if self._url_prefix:
            if not raw_path.startswith(self._url_prefix):
                if call_next:
                    await call_next(conn, receive, send)
                else:
                    await self._respond(send, HTTPStatus.NOT_FOUND)
                return
            raw_path = raw_path[len(self._url_prefix):]

        decoded = unquote(raw_path)
        # ``realpath`` follows symlinks the same way ``Path.resolve()``
        # used to.  The traversal check is then a single string-prefix
        # comparison against the pre-computed ``<root>/`` form — no
        # ``PurePath.relative_to`` allocation per request.
        target = os.path.realpath(os.path.join(self._root_str, decoded.lstrip('/')))
        if target != self._root_str and not target.startswith(self._root_sep):
            await self._respond(send, HTTPStatus.BAD_REQUEST)
            return

        if not os.path.isfile(target):
            # Directory request → serve the configured index file when one
            # is set (off by default, so ``app.static()`` callers keep the
            # exact-file-only behaviour).  The index candidate is run
            # through the same realpath + traversal guard as any other
            # target so a crafted ``index`` can't escape the root.
            if self._index and os.path.isdir(target):
                candidate = os.path.realpath(os.path.join(target, self._index))
                if ((candidate == self._root_str or candidate.startswith(self._root_sep))
                        and os.path.isfile(candidate)):
                    await self._serve(conn, send, candidate)
                    return
            if call_next:
                await call_next(conn, receive, send)
            else:
                await self._respond(send, HTTPStatus.NOT_FOUND)
            return

        await self._serve(conn, send, target)

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
                        pass  # malformed q-value → treat as acceptable.
            return True
        return False

    def _negotiate(self, conn, target: str) -> tuple[str, bytes]:
        """Pick which file to serve and what Content-Encoding to advertise.

        Returns ``(path_to_serve, content_encoding)``.  `content_encoding`
        is ``b''`` for uncompressed; ``b'br'`` / ``b'zstd'`` / ``b'gzip'``
        when a precompressed sibling (``<path>.<suffix>``) was selected.

        Range requests bypass the precompressed-sibling lookup — encoded
        bodies have a different size than the original and serving a
        Range over an encoded variant is messy.  Matches what nginx
        does with ``gzip_static`` + Range.

        Sibling file-existence is memoised in ``_sibling_cache`` so the
        per-request ``os.path.isfile`` syscalls for ``.br`` / ``.zst`` /
        ``.gz`` siblings happen only once per path.
        """
        accept = b''
        for k, v in conn.headers:
            kl = k.lower()
            if kl == b'range':
                return target, b''
            if kl == b'accept-encoding':
                accept = v.lower()
        if not accept:
            return target, b''

        # Sibling existence: cached if `cache=True`, recomputed every
        # request otherwise (so the from-disk-every-request contract
        # extends to the sibling existence check).
        if self._cache_enabled:
            siblings = self._sibling_cache.get(target)
            if siblings is None:
                siblings = {}
                for enc, suffix in self._ENCODING_SUFFIXES:
                    sibling = target + suffix
                    if os.path.isfile(sibling):
                        siblings[enc] = sibling
                self._sibling_cache[target] = siblings
        else:
            siblings = {}
            for enc, suffix in self._ENCODING_SUFFIXES:
                sibling = target + suffix
                if os.path.isfile(sibling):
                    siblings[enc] = sibling

        for enc, _suffix in self._ENCODING_SUFFIXES:
            sibling = siblings.get(enc)
            if sibling is not None and self._client_accepts(accept, enc):
                return sibling, enc
        return target, b''

    async def _serve(self, conn, send, path: str):
        # Pick variant (precompressed sibling if available + accepted).
        served_path, content_encoding = self._negotiate(conn, path)

        body: bytes | None
        mime: bytes
        size: int

        # Fast path: cached entry, still within the per-entry stat TTL.
        # Only consulted when caching is enabled — when ``cache=False``
        # every request flows through the stat + read branch below.
        cached_entry = self._cache.get(served_path) if self._cache_enabled else None
        now = time.monotonic()
        if (cached_entry is not None
                and self._STAT_TTL_S > 0
                and now - cached_entry[5] < self._STAT_TTL_S):
            mtime_ns, size, body, mime, _, _ = cached_entry
            self._cache.move_to_end(served_path)
        else:
            # Cache miss, stale TTL, or caching disabled — re-stat to
            # confirm the entry is still valid (or to learn it for the
            # first time when caching is off).
            try:
                st = os.stat(served_path)
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
                # the fast path again.  Only reachable when caching is on.
                _, _, body, mime, _, _ = cached_entry
                self._cache[served_path] = (
                    mtime_ns, size, body, mime, content_encoding, now)
                self._cache.move_to_end(served_path)
            elif size <= self._CACHE_MAX_BYTES_PER_FILE:
                # Read from disk.  When caching is enabled, also store
                # the body for next time.  When caching is disabled,
                # this branch fires on every request and the read is
                # always fresh.
                # Content-Type derives from the ORIGINAL request's path
                # extension, not the .br/.gz/.zst suffix — e.g. app.js.br
                # is still ``text/javascript``.  ``mimetypes.guess_type``
                # only inspects the extension so passing the full path
                # is equivalent to passing the basename, with one fewer
                # call.
                mime = (mimetypes.guess_type(path)[0]
                        or 'application/octet-stream').encode()
                try:
                    with open(served_path, 'rb') as f:
                        body = f.read()
                except OSError:
                    await self._respond(send, HTTPStatus.NOT_FOUND)
                    return
                if self._cache_enabled:
                    self._store(served_path, mtime_ns, size, body, mime,
                                content_encoding, now)
            else:
                # Above the cache threshold — drop any stale entry and
                # fall through to the streaming/pathsend branch.
                if self._cache_enabled:
                    self._cache.pop(served_path, None)
                mime = (mimetypes.guess_type(path)[0]
                        or 'application/octet-stream').encode()
                body = None

        range_hdr = None
        for k, v in conn.headers:
            if k.lower() == b'range':
                range_hdr = v.decode()
                break

        start, end = 0, size - 1
        status = HTTPStatus.OK
        extra_headers: list[tuple[bytes, bytes]] = []

        if self._conditional:
            # Validators: a strong ETag over (mtime, size) plus Last-Modified,
            # so ``If-None-Match`` / ``If-Modified-Since`` can produce a 304
            # instead of a full re-transfer (bug 1.21d).  Cheap; emitted on
            # every response, including the streaming path.
            etag = f'"{mtime_ns:x}-{size:x}"'.encode()
            last_modified = formatdate(mtime_ns / 1_000_000_000, usegmt=True).encode()

            # Conditional GET — answer 304 before touching the body (avoids the
            # large-file read/stream entirely on a cache revalidation).
            if _not_modified(conn.headers, etag, mtime_ns):
                cond_headers: list[tuple[bytes, bytes]] = [
                    (b'etag', etag), (b'last-modified', last_modified)]
                if content_encoding:
                    cond_headers.append((b'vary', b'Accept-Encoding'))
                await self._respond(send, HTTPStatus.NOT_MODIFIED, cond_headers)
                return

            extra_headers.append((b'etag', etag))
            extra_headers.append((b'last-modified', last_modified))

        if range_hdr:
            parsed = _parse_byte_range(range_hdr, size)
            # ``None`` → unparseable/multi-range → ignore and serve full 200.
            if parsed is not None:
                start, end = parsed
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
                       and 'http.response.pathsend' in conn.extensions)

        await send({'type': ASGIEvent.HTTP_RESPONSE_START, 'status': status,
                    'headers': [
                        (b'content-type', mime),
                        (b'content-length', str(body_len).encode()),
                        *extra_headers,
                    ]})

        if pathsend_ok:
            await send({'type': ASGIEvent.HTTP_RESPONSE_PATHSEND,
                        'path': served_path})
            return

        remaining = body_len
        fobj = await asyncio.to_thread(open, served_path, 'rb')
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

    def _store(self, path: str, mtime_ns: int, size: int,
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
