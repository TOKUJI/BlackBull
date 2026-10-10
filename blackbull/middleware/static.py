"""Static-file middleware.

Import registers additional MIME types process-wide. Usage, containment and
cache visibility contracts are in docs/guide/static-files.md.
"""
import asyncio
import mimetypes
import os
import time
from collections import OrderedDict
from stat import S_ISLNK, S_ISREG
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote
from http import HTTPStatus

from blackbull.connection import Connection
from blackbull.env import get_settings, Environment
from blackbull.native import NativeResponse
from ._accept_encoding import acceptable_encodings


# Register standard web-asset MIME types missing from slim host databases.
# Keep compressed fonts/images out of downstream compression.
for _ext, _mime in (
    ('.woff',  'font/woff'),
    ('.woff2', 'font/woff2'),
    ('.webp',  'image/webp'),
    ('.avif',  'image/avif'),
    ('.wasm',  'application/wasm'),
):
    mimetypes.add_type(_mime, _ext)
del _ext, _mime


def _parse_byte_range(range_hdr: bytes, size: int) -> tuple[int, int] | None:
    """Parse a single ``bytes=`` Range header into an inclusive ``(start, end)``
    with ``end`` clipped to ``size - 1``; ``start >= size`` means unsatisfiable.

    Returns ``None`` — serve the whole file — for a Range to ignore: a
    non-``bytes`` unit, a multi-range set, an invalid range-spec, or a suffix
    range on an empty file.  Never raises.
    """
    if not range_hdr.startswith(b'bytes='):
        return None
    # A multi-range set or any non-digit fails the 1*DIGIT checks.
    start_s, sep, end_s = range_hdr[6:].strip(b' \t').partition(b'-')
    if (not sep or (start_s and not start_s.isdigit())
            or (end_s and not end_s.isdigit())):
        return None
    try:
        if not start_s:
            # Suffix range: bytes=-N → the last N bytes.
            if not end_s:
                return None
            n = int(end_s)
            if size == 0 and n > 0:
                return None
            return (max(0, size - n), size - 1)
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:  # beyond int()'s digit limit
        return None
    if end_s and end < start:
        return None
    return (start, end if end < size else size - 1)


def _not_modified(headers, etag: bytes, mtime_ns: int) -> bool:
    """Evaluate the conditional-GET preconditions (RFC 9110 §13).

    ``If-None-Match`` takes precedence over ``If-Modified-Since``; when the
    former is present the latter is ignored.  Returns ``True`` when the
    client's cached copy is still fresh and the caller should answer 304.
    """
    inm = None
    ims = None
    for k, v in headers:
        if k == b'if-none-match':
            inm = v
        elif k == b'if-modified-since':
            ims = v
    if inm is not None:
        candidate = inm
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


def _file_stat(path: str) -> os.stat_result | None:
    """``os.path.isfile`` that keeps the stat it took."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st if S_ISREG(st.st_mode) else None


class StaticFiles:
    """Serve files from one directory, as route middleware or on its own.

    ``app.static(url_prefix, root_dir)`` attaches it to a route; called with
    three arguments instead of four it is a plain ASGI app that can be mounted
    anywhere.  The difference shows up on a miss: with a ``call_next`` the
    request continues down the chain, and without one it is answered 404.

    Only ``GET`` and ``HEAD`` are served.  A resolved path outside the root is
    a 400, checked after symlinks are followed, so a link pointing out of the
    tree does not escape it.  Under ``BLACKBULL_ENV=production`` nothing is
    served at all — a production deployment is expected to have a proxy or CDN
    in front, and two things serving the same files is the problem being
    avoided.

    What a hit produces depends on the request and on what is on disk.  ETag
    and Last-Modified are emitted unless ``conditional`` is off, and a matching
    ``If-None-Match`` / ``If-Modified-Since`` gets a 304 without the body being
    read.  A ``Range`` request gets a 206, or a 416 when the range is
    unsatisfiable.  A precompressed sibling (``app.js.br``, ``.zst``, ``.gz``)
    is served in place of the original when the client accepts that encoding
    and the request is not a range request; those responses carry
    ``Vary: Accept-Encoding``.  Files larger than four megabytes stream in
    chunks rather than being read whole, so peak memory does not follow file
    size.

    ``docs/guide/static-files.md`` covers the choices this leaves open — the
    opt-in cache in particular, and why it is off by default.
    """

    # Files at or below this size are read once and held in memory.
    # Static assets in the wild (CSS/JS/manifest/small images) cluster
    # well under this; larger files fall through to streaming.
    _CACHE_MAX_BYTES_PER_FILE = 4 * 1024 * 1024
    _CACHE_MAX_ENTRIES = 256
    # 64 KiB streaming chunk for files above the cache threshold.
    _CHUNK = 64 * 1024
    # Seconds between cached-body validations. Requests still stat the target;
    # 0 compares every request. This can delay visibility of on-disk edits.
    _STAT_TTL_S = float(os.environ.get('BB_STATIC_STAT_TTL_S', '1.0'))

    #: Precompressed sibling suffix and Content-Encoding token per coding.
    _VARIANTS: dict[str, tuple[str, bytes]] = {
        'br': ('.br', b'br'),
        'zstd': ('.zst', b'zstd'),
        'gzip': ('.gz', b'gzip'),
    }

    def __init__(self, directory: str | None = None, *,
                 url_prefix: str = '', root_dir: str | Path | None = None,
                 cache: bool = False, index: str | None = None,
                 conditional: bool = True):
        """Serve directory or root_dir; at least one must be supplied.

        index=None disables directory indexes. cache=True holds small bodies in a
        bounded per-worker cache; its validation TTL can delay on-disk edits.
        conditional=False disables validators and 304 responses. See
        docs/guide/static-files.md for cache, traversal and production-mode limits.
        """
        resolved = directory or root_dir
        if resolved is None:
            raise ValueError('directory or root_dir is required')
        # Use the canonical real path for the traversal boundary.
        self._root_str: str = os.path.realpath(os.fspath(resolved))
        # Pre-computed prefix for the traversal check — accept
        # ``<root>/...`` exactly, reject ``<root>x/...``.
        self._root_sep: str = self._root_str + os.sep
        self._url_prefix = url_prefix.rstrip('/')
        # None until the first request resolves it; see `__call__`.
        self._enabled: bool | None = None
        self._cache_enabled: bool = cache
        self._index: str | None = index
        self._conditional: bool = conditional
        # Key by the canonical served path, including precompressed siblings.
        # Values are (mtime_ns, size, body, mime, content_encoding, last_validation).
        # The validation TTL can defer cached-body changes; requests still stat.
        self._cache: OrderedDict[
            str, tuple[int, int, bytes, bytes, bytes, float]
        ] = OrderedDict()
        # target → the codings with a sibling on disk, probed once when
        # caching is enabled; otherwise every request probes afresh.
        self._sibling_cache: dict[str, frozenset[str]] = {}

    @property
    def _root(self) -> Path:
        """Backwards-compat: callers and tests may inspect
        ``staticfiles._root`` as a [`Path`][].  Built on demand so
        the hot path keeps its plain-string representation."""
        return Path(self._root_str)

    async def __call__(self, conn, receive, send, call_next=None):
        # Retain HTTP/method guards for standalone ASGI use outside the route table.
        if (not isinstance(conn, Connection) or conn.type != 'http'
                or conn.method not in ('GET', 'HEAD')):
            if call_next:
                await call_next(conn, receive, send)
            else:
                await self._respond(send, HTTPStatus.NOT_FOUND)
            return

        # Resolve settings on first use, after late configuration loading.
        if self._enabled is None:
            self._enabled = get_settings().env != Environment.PRODUCTION
        if not self._enabled:
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
        # Resolve symlinks before checking the root boundary.
        target = os.path.realpath(os.path.join(self._root_str, decoded.lstrip('/')))
        if not self._inside_root(target):
            await self._respond(send, HTTPStatus.BAD_REQUEST)
            return

        st = _file_stat(target)
        if st is None:
            # Directory request → serve the configured index file when one
            # is set (off by default, so ``app.static()`` callers keep the
            # exact-file-only behaviour).  The index candidate is run
            # through the same realpath + traversal guard as any other
            # target so a crafted ``index`` can't escape the root.
            if self._index and os.path.isdir(target):
                candidate = os.path.realpath(os.path.join(target, self._index))
                if self._inside_root(candidate):
                    st = _file_stat(candidate)
                    if st is not None:
                        await self._serve(conn, send, candidate, st)
                        return
            if call_next:
                await call_next(conn, receive, send)
            else:
                await self._respond(send, HTTPStatus.NOT_FOUND)
            return

        await self._serve(conn, send, target, st)

    def _inside_root(self, resolved: str) -> bool:
        """The one boundary definition: request target, index candidate and
        any variant selection are judged here."""
        return resolved == self._root_str or resolved.startswith(self._root_sep)

    def _negotiate(self, conn, target: str, st: os.stat_result,
                   ranged: bool,
                   ) -> tuple[str, bytes, os.stat_result] | None:
        """The file to serve, its Content-Encoding (``b''`` for none) and its
        stat, or ``None`` when the selected precompressed sibling resolves
        outside the root: the request is then refused, not served another
        variant.

        A range request never selects a sibling; its byte offsets name the
        original.
        """
        if ranged:
            return target, b'', st
        accept = conn.headers.get_combined(b'accept-encoding')
        if not accept:
            return target, b'', st
        present = None
        if self._cache_enabled:
            present = self._sibling_cache.get(target)
            if present is None:
                present = self._sibling_cache[target] = frozenset(
                    name for name, (suffix, _) in self._VARIANTS.items()
                    if os.path.isfile(target + suffix))
        for name in acceptable_encodings(accept):
            if present is not None and name not in present:
                continue
            suffix, token = self._VARIANTS[name]
            sibling = target + suffix
            try:
                sibling_st = os.lstat(sibling)
                if S_ISLNK(sibling_st.st_mode):
                    # The walk in __call__ covered the directory; only a link
                    # can leave the root, and it is followed every request.
                    sibling_st = os.stat(sibling)
                    if (S_ISREG(sibling_st.st_mode) and not self._inside_root(
                            os.path.realpath(sibling))):
                        return None
            except OSError:
                continue
            if S_ISREG(sibling_st.st_mode):
                return sibling, token, sibling_st
        return target, b'', st

    async def _serve(self, conn, send, path: str, st: os.stat_result):
        ranges = conn.headers.getlist(b'range')
        selection = self._negotiate(conn, path, st, bool(ranges))
        if selection is None:
            await self._respond(send, HTTPStatus.BAD_REQUEST)
            return
        served_path, content_encoding, st = selection

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
            # Cache miss, stale TTL, or caching disabled: the stat taken
            # while selecting this file decides.
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
                # Derive Content-Type from the original extension, not the compression suffix.
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

        range_hdr = ranges[0][1] if ranges else None

        start, end = 0, size - 1
        status = HTTPStatus.OK
        extra_headers: list[tuple[bytes, bytes]] = []

        if self._conditional:
            # Validators: a strong ETag over (mtime, size) plus Last-Modified,
            # so ``If-None-Match`` / ``If-Modified-Since`` can produce a 304
            # instead of a full re-transfer.  Cheap; emitted on
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
            if parsed is not None:
                start, end = parsed
                if start >= size:
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
            chunk = body[start:end + 1] if (start or end != size - 1) else body
            await send(NativeResponse(
                status=status,
                header=[
                    (b'content-type', mime),
                    (b'content-length', str(body_len).encode()),
                    *extra_headers,
                ],
                body=chunk))
            return

        # Use pathsend only for full-file responses on a host advertising it.
        # TLS, HTTP/2 and Range responses need the threaded-read fallback.
        pathsend_ok = (status != HTTPStatus.PARTIAL_CONTENT
                       and 'http.response.pathsend' in conn.extensions)

        header = [
            (b'content-type', mime),
            (b'content-length', str(body_len).encode()),
            *extra_headers,
        ]

        if pathsend_ok:
            # Header and the file in one object: the sender flushes the
            # headers and hands the path to ``loop.sendfile``.
            await send(NativeResponse(status=status, header=header,
                                      file_path=served_path))
            return

        await send(NativeResponse(status=status, header=header))

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
                await send(NativeResponse(body=chunk,
                                          more_body=remaining > 0))
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
        await send(NativeResponse(status=status,
                                  header=list(extra_headers or []),
                                  body=b''))
