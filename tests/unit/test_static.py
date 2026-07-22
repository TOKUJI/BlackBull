"""Tests for static file serving middleware (P3)
================================================

P3 item: Static file serving middleware — stream files from a directory with
support for Range requests / 206 Partial Content (RFC 7233).

Expected location: blackbull.middleware.static.StaticFiles

StaticFiles is an ASGI callable:
    app = StaticFiles(directory='/var/www/static')
    # mounts as a stand-alone ASGI app or wraps another app
"""

import pathlib
import pytest

from blackbull.headers import Headers


def _scope(method: str = 'GET', path: str = '/',
           headers: dict[str, str] | None = None) -> 'Connection':
    # Sprint 80: BlackBull threads a native Connection (not an ASGI scope dict)
    # through the middleware/handler pipeline for HTTP.
    from blackbull.connection import Connection
    raw = [(k.lower().encode(), v.encode())
           for k, v in (headers or {}).items()]
    return Connection(method=method, path=path, raw_path=path.encode(),
                      headers=Headers(raw), type='http')


async def _noop_receive():
    return {'type': 'http.disconnect'}


async def _collect(app, scope) -> tuple[dict, bytes]:
    """Run *app* and return (start_event, concatenated_body)."""
    events = []

    async def send(event):
        events.append(event)

    await app(scope, _noop_receive, send)
    start = next(e for e in events if e.get('type') == 'http.response.start')
    body = b''.join(e.get('body', b'')
                    for e in events if e.get('type') == 'http.response.body')
    return start, body


@pytest.fixture
def static_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / 'hello.txt').write_bytes(b'Hello, static world!')
    (tmp_path / 'style.css').write_bytes(b'body { color: red; }')
    sub = tmp_path / 'sub'
    sub.mkdir()
    (sub / 'page.html').write_bytes(b'<html><body>sub</body></html>')
    return tmp_path


# ---------------------------------------------------------------------------
# Basic file serving
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStaticFilesBasic:
    """StaticFiles must serve files from a configured directory."""

    async def test_existing_file_returns_200(self, static_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/hello.txt'))
        assert start['status'] == 200, f'Expected 200; got {start["status"]}'

    async def test_existing_file_body_matches_content(self, static_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        _, body = await _collect(app, _scope(path='/hello.txt'))
        assert body == b'Hello, static world!', (
            f'Expected file content; got {body!r}'
        )

    async def test_missing_file_returns_404(self, static_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/missing.html'))
        assert start['status'] == 404, f'Expected 404; got {start["status"]}'

    async def test_subdirectory_file_served(self, static_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, body = await _collect(app, _scope(path='/sub/page.html'))
        assert start['status'] == 200, f'Expected 200; got {start["status"]}'
        assert b'sub' in body, f'Expected sub-page content; got {body!r}'

    async def test_content_type_css(self, static_dir):
        """CSS files must carry Content-Type: text/css."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/style.css'))
        headers = {k.lower(): v.lower() for k, v in start.get('headers', [])}
        assert b'text/css' in headers.get(b'content-type', b''), (
            f'Expected text/css; got {headers.get(b"content-type")!r}'
        )

    async def test_content_type_html(self, static_dir):
        """HTML files must carry Content-Type: text/html."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/sub/page.html'))
        headers = {k.lower(): v.lower() for k, v in start.get('headers', [])}
        assert b'text/html' in headers.get(b'content-type', b''), (
            f'Expected text/html; got {headers.get(b"content-type")!r}'
        )

    @pytest.mark.parametrize('filename,expected_ct', [
        ('asset.woff',  b'font/woff'),
        ('asset.woff2', b'font/woff2'),
        ('asset.webp',  b'image/webp'),
        ('asset.avif',  b'image/avif'),
        ('asset.wasm',  b'application/wasm'),
    ])
    async def test_content_type_web_assets_independent_of_system_db(
            self, static_dir, filename, expected_ct):
        """Common web asset MIME types must resolve correctly even when
        the host's ``/etc/mime.types`` is missing or sparse (e.g.
        ``python:3.13-slim`` containers).  Sprint 35 phase-trace traced
        a 30-60 ms per-request CPU tail on woff2 files to this case:
        no mime entry → ``application/octet-stream`` Content-Type →
        Compression middleware brotli-encoded already-compressed bytes.
        ``blackbull.middleware.static`` registers these at import to
        keep the deployed Content-Type accurate without forcing
        operators to install ``mime-support``."""
        from blackbull.middleware.static import StaticFiles
        (static_dir / filename).write_bytes(b'opaque-bytes' * 200)
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path=f'/{filename}'))
        headers = {k.lower(): v for k, v in start.get('headers', [])}
        assert headers.get(b'content-type') == expected_ct, (
            f'{filename}: expected {expected_ct!r}, '
            f'got {headers.get(b"content-type")!r}'
        )

    async def test_content_length_header_present(self, static_dir):
        """Response must include Content-Length matching the file size."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/hello.txt'))
        headers = {k.lower(): v for k, v in start.get('headers', [])}
        assert b'content-length' in headers, 'Expected Content-Length header'
        assert headers[b'content-length'] == b'20', (
            f'Expected content-length: 20; got {headers.get(b"content-length")!r}'
        )


# ---------------------------------------------------------------------------
# Path traversal security
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStaticFilesPathSecurity:
    """StaticFiles must reject paths that escape the configured directory."""

    async def test_path_traversal_dot_dot_rejected(self, static_dir):
        """/../etc/passwd must be rejected with 400 or 404."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/../etc/passwd'))
        assert start['status'] in (400, 403, 404), (
            f'Path traversal must be rejected; got {start["status"]}'
        )

    async def test_encoded_path_traversal_rejected(self, static_dir):
        """URL-encoded ../ (%2F%2E%2E) must also be rejected."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/%2F..%2Fetc%2Fpasswd'))
        assert start['status'] in (400, 403, 404), (
            f'Encoded path traversal must be rejected; got {start["status"]}'
        )

    async def test_directory_listing_not_served(self, static_dir):
        """Requesting a bare directory path must return 404 (no directory listing)."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/sub/'))
        assert start['status'] in (403, 404), (
            f'Directory listing must not be served; got {start["status"]}'
        )


# ---------------------------------------------------------------------------
# Range requests — RFC 7233
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStaticFilesRangeRequests:
    """StaticFiles must support Range requests per RFC 7233.

    A valid Range: bytes=<start>-<end> request must return:
      - 206 Partial Content
      - Content-Range: bytes <start>-<end>/<total> header
      - Only the requested byte slice in the body
    """

    FILE = b'Hello, static world!'   # 20 bytes

    async def test_range_request_returns_206(self, static_dir):
        """Range: bytes=0-3 must return 206 Partial Content."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(
            app, _scope(path='/hello.txt', headers={'Range': 'bytes=0-3'})
        )
        assert start['status'] == 206, f'Expected 206; got {start["status"]}'

    async def test_range_body_is_correct_slice(self, static_dir):
        """Range: bytes=0-3 body must be the first 4 bytes of the file."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        _, body = await _collect(
            app, _scope(path='/hello.txt', headers={'Range': 'bytes=0-3'})
        )
        assert body == b'Hell', f'Expected b"Hell"; got {body!r}'

    async def test_range_content_range_header(self, static_dir):
        """206 response must include Content-Range: bytes 0-3/20."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(
            app, _scope(path='/hello.txt', headers={'Range': 'bytes=0-3'})
        )
        headers = {k.lower(): v for k, v in start.get('headers', [])}
        assert b'content-range' in headers, 'Expected Content-Range header in 206'
        assert headers[b'content-range'] == b'bytes 0-3/20', (
            f'Expected "bytes 0-3/20"; got {headers.get(b"content-range")!r}'
        )

    async def test_range_content_length_is_slice_size(self, static_dir):
        """Content-Length in a 206 response must equal the slice length, not the
        full file size."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(
            app, _scope(path='/hello.txt', headers={'Range': 'bytes=0-3'})
        )
        headers = {k.lower(): v for k, v in start.get('headers', [])}
        assert headers.get(b'content-length') == b'4', (
            f'Content-Length must be 4 (slice size); got {headers.get(b"content-length")!r}'
        )

    async def test_mid_file_range(self, static_dir):
        """Range: bytes=7-12 must return bytes 7 through 12 inclusive."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        _, body = await _collect(
            app, _scope(path='/hello.txt', headers={'Range': 'bytes=7-12'})
        )
        expected = self.FILE[7:13]
        assert body == expected, f'Expected {expected!r}; got {body!r}'

    async def test_suffix_range(self, static_dir):
        """Range: bytes=-5 (last 5 bytes) must return the file tail."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        _, body = await _collect(
            app, _scope(path='/hello.txt', headers={'Range': 'bytes=-5'})
        )
        assert body == self.FILE[-5:], (
            f'Expected last 5 bytes {self.FILE[-5:]!r}; got {body!r}'
        )

    async def test_out_of_range_returns_416(self, static_dir):
        """A Range that exceeds the file size must return 416 Range Not Satisfiable."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(
            app, _scope(path='/hello.txt', headers={'Range': 'bytes=100-200'})
        )
        assert start['status'] == 416, (
            f'Out-of-range request must return 416; got {start["status"]}'
        )

    async def test_no_range_returns_full_file(self, static_dir):
        """Without a Range header the full file must be served with status 200."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, body = await _collect(app, _scope(path='/hello.txt'))
        assert start['status'] == 200
        assert body == self.FILE, f'Expected full file; got {body!r}'


# ---------------------------------------------------------------------------
# Environment-gated serving
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStaticFilesEnv:
    """BLACKBULL_ENV controls whether static files are served."""

    async def test_production_returns_404(self, static_dir, monkeypatch):
        monkeypatch.setenv('BLACKBULL_ENV', 'production')
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/hello.txt'))
        assert start['status'] == 404, f'Expected 404 in production; got {start["status"]}'

    async def test_development_serves_file(self, static_dir, monkeypatch):
        monkeypatch.setenv('BLACKBULL_ENV', 'development')
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/hello.txt'))
        assert start['status'] == 200, f'Expected 200 in development; got {start["status"]}'

    async def test_test_env_serves_file(self, static_dir, monkeypatch):
        monkeypatch.setenv('BLACKBULL_ENV', 'test')
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/hello.txt'))
        assert start['status'] == 200, f'Expected 200 in test env; got {start["status"]}'


# ---------------------------------------------------------------------------
# app.use() + app.static() integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestBlackBullStaticRegistration:
    """app.use() and app.static() register global middleware."""

    async def test_use_registers_global_middleware(self):
        from blackbull import BlackBull
        app = BlackBull()
        sentinel = object()
        app.use(sentinel)
        assert app._global_middlewares == [sentinel]

    async def test_static_roots_tracked(self, static_dir, tmp_path):
        from blackbull import BlackBull
        app = BlackBull()
        other = tmp_path / 'other'
        other.mkdir()
        app.static('/a', str(static_dir))
        app.static('/b', str(other))
        assert len(app._static_roots) == 2

    async def test_static_injects_staticfiles_into_global_chain(self, static_dir):
        from blackbull import BlackBull
        from blackbull.middleware.static import StaticFiles
        app = BlackBull()
        app.static('/assets', str(static_dir))
        assert len(app._global_middlewares) == 1
        assert isinstance(app._global_middlewares[0], StaticFiles)

    async def test_prefix_file_served_end_to_end(self, static_dir):
        """Global middleware serves /assets/hello.txt when registered via app.static()."""
        from blackbull import BlackBull
        app = BlackBull()
        app.static('/assets', str(static_dir))
        # Exercise the StaticFiles middleware directly (avoids _wrap_send adapter)
        mw = app._global_middlewares[0]
        start, body = await _collect(mw, _scope(path='/assets/hello.txt'))
        assert start['status'] == 200
        assert body == b'Hello, static world!'


# ---------------------------------------------------------------------------
# Precompressed-variant serving (Sprint 29 #1)
# ---------------------------------------------------------------------------

@pytest.fixture
def precompressed_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Static dir with both original and precompressed siblings."""
    (tmp_path / 'app.js').write_bytes(b'console.log("hi");' * 200)
    (tmp_path / 'app.js.br').write_bytes(b'BR-COMPRESSED-BYTES')
    (tmp_path / 'app.js.gz').write_bytes(b'GZ-COMPRESSED-BYTES')
    (tmp_path / 'app.js.zst').write_bytes(b'ZST-COMPRESSED-BYTES')
    # File without precompressed siblings
    (tmp_path / 'manifest.json').write_bytes(b'{}')
    return tmp_path


@pytest.mark.asyncio
class TestPrecompressedVariant:
    async def test_serves_br_sibling_when_accept_br(self, precompressed_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(precompressed_dir))
        start, body = await _collect(
            app, _scope(path='/app.js', headers={'accept-encoding': 'br, gzip'}))
        assert start['status'] == 200
        assert body == b'BR-COMPRESSED-BYTES'
        hdrs = dict(start['headers'])
        assert hdrs[b'content-encoding'] == b'br'
        assert hdrs[b'content-type'] == b'text/javascript'    # mime from .js, not .js.br
        assert hdrs[b'vary'] == b'Accept-Encoding'

    async def test_serves_gzip_when_only_gzip_offered(self, precompressed_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(precompressed_dir))
        start, body = await _collect(
            app, _scope(path='/app.js', headers={'accept-encoding': 'gzip'}))
        assert start['status'] == 200
        assert body == b'GZ-COMPRESSED-BYTES'
        hdrs = dict(start['headers'])
        assert hdrs[b'content-encoding'] == b'gzip'

    async def test_prefers_br_over_gzip_when_both_offered(self, precompressed_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(precompressed_dir))
        start, body = await _collect(
            app, _scope(path='/app.js', headers={'accept-encoding': 'gzip, br'}))
        # Server preference br > zstd > gzip overrides client list order
        hdrs = dict(start['headers'])
        assert hdrs[b'content-encoding'] == b'br'
        assert body == b'BR-COMPRESSED-BYTES'

    async def test_falls_through_when_no_sibling_exists(self, precompressed_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(precompressed_dir))
        # manifest.json has no .br/.gz/.zst sibling
        start, body = await _collect(
            app, _scope(path='/manifest.json',
                        headers={'accept-encoding': 'br, gzip'}))
        assert start['status'] == 200
        assert body == b'{}'
        hdrs = dict(start['headers'])
        assert b'content-encoding' not in hdrs

    async def test_zero_q_value_does_not_match(self, precompressed_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(precompressed_dir))
        # client offers br but with q=0 (explicitly refuses)
        start, body = await _collect(
            app, _scope(path='/app.js',
                        headers={'accept-encoding': 'br;q=0, gzip'}))
        hdrs = dict(start['headers'])
        assert hdrs[b'content-encoding'] == b'gzip'

    async def test_range_request_skips_sibling(self, precompressed_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(precompressed_dir))
        # Range requests must be served from the uncompressed file because
        # the encoded sibling has a different size (start/end would be wrong).
        start, body = await _collect(
            app, _scope(path='/app.js',
                        headers={'accept-encoding': 'br',
                                 'range': 'bytes=0-9'}))
        assert start['status'] == 206
        hdrs = dict(start['headers'])
        assert b'content-encoding' not in hdrs
        assert hdrs[b'content-type'] == b'text/javascript'
        assert body == b'console.lo'

    async def test_no_accept_encoding_serves_uncompressed(self, precompressed_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(precompressed_dir))
        start, body = await _collect(app, _scope(path='/app.js'))
        hdrs = dict(start['headers'])
        assert b'content-encoding' not in hdrs
        assert hdrs[b'content-type'] == b'text/javascript'
        # uncompressed body is what we wrote
        assert body == b'console.log("hi");' * 200

    async def test_cache_hit_returns_same_sibling(self, precompressed_dir):
        """Opting in to caching (``cache=True``): the second request
        serves the cached body of the first, no re-read from disk."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(precompressed_dir), cache=True)
        # First request fills the cache
        start1, body1 = await _collect(
            app, _scope(path='/app.js', headers={'accept-encoding': 'br'}))
        # Second request — cache hit
        start2, body2 = await _collect(
            app, _scope(path='/app.js', headers={'accept-encoding': 'br'}))
        assert body1 == body2 == b'BR-COMPRESSED-BYTES'
        assert dict(start1['headers'])[b'content-encoding'] == b'br'
        assert dict(start2['headers'])[b'content-encoding'] == b'br'

    async def test_different_encoding_uses_different_cache_entries(self, precompressed_dir):
        """With caching opted in, requesting a different encoding
        than the previously-cached one still serves the right
        sibling (cache key includes the served path)."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(precompressed_dir), cache=True)
        # Fill cache with br
        _, body_br = await _collect(
            app, _scope(path='/app.js', headers={'accept-encoding': 'br'}))
        # Then request gzip — should hit gzip sibling, not the cached br
        _, body_gz = await _collect(
            app, _scope(path='/app.js', headers={'accept-encoding': 'gzip'}))
        assert body_br == b'BR-COMPRESSED-BYTES'
        assert body_gz == b'GZ-COMPRESSED-BYTES'

    async def test_default_cache_off_no_body_stored(self, precompressed_dir):
        """Default (``cache=False``): the ``_cache`` dict stays empty
        even after multiple requests — every response is freshly read
        from disk."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(precompressed_dir))
        assert app._cache_enabled is False
        for _ in range(3):
            _, body = await _collect(
                app, _scope(path='/app.js', headers={'accept-encoding': 'br'}))
            assert body == b'BR-COMPRESSED-BYTES'
        assert len(app._cache) == 0
        assert len(app._sibling_cache) == 0

    async def test_cache_true_populates_cache(self, precompressed_dir):
        """Sanity-check the opt-in: a single request with
        ``cache=True`` leaves an entry in ``_cache``."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(precompressed_dir), cache=True)
        assert app._cache_enabled is True
        await _collect(
            app, _scope(path='/app.js', headers={'accept-encoding': 'br'}))
        assert len(app._cache) == 1


# ---------------------------------------------------------------------------
# Sprint 31 — ``http.response.pathsend`` extension wiring
# ---------------------------------------------------------------------------

@pytest.fixture
def large_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Directory with one file larger than the cache threshold so the
    middleware takes the streaming/pathsend branch."""
    from blackbull.middleware.static import StaticFiles
    large = b'L' * (StaticFiles._CACHE_MAX_BYTES_PER_FILE + 1024)
    (tmp_path / 'big.bin').write_bytes(large)
    return tmp_path


def _scope_with_pathsend(path: str, headers: dict | None = None) -> 'Connection':
    conn = _scope(path=path, headers=headers)
    conn.extensions = {'http.response.pathsend': {}}
    return conn


@pytest.mark.asyncio
class TestStaticFilesPathsend:
    async def test_emits_pathsend_when_extension_advertised(self, large_dir):
        """Above-cache file + cleartext H1 scope → pathsend event."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(large_dir))
        events: list = []

        async def send(event):
            events.append(event)

        await app(_scope_with_pathsend('/big.bin'), _noop_receive, send)

        types = [e.get('type') for e in events]
        assert 'http.response.pathsend' in types
        ps = next(e for e in events if e['type'] == 'http.response.pathsend')
        assert ps['path'].endswith('big.bin')
        # No body events on the pathsend path — the sender takes over.
        assert 'http.response.body' not in types

    async def test_falls_back_to_streaming_when_extension_absent(self, large_dir):
        """No pathsend in scope (TLS or HTTP/2) → chunked streaming."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(large_dir))
        # No 'extensions' key — same as TLS HTTP/1.1.
        start, body = await _collect(app, _scope(path='/big.bin'))
        assert start['status'] == 200
        assert len(body) == StaticFiles._CACHE_MAX_BYTES_PER_FILE + 1024

    async def test_range_request_does_not_use_pathsend(self, large_dir):
        """ASGI pathsend extension has no offset/count — Range requests
        must keep using the chunked streaming path so we honour the
        Content-Range response correctly."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(large_dir))
        events: list = []

        async def send(event):
            events.append(event)

        scope = _scope_with_pathsend('/big.bin',
                                     headers={'range': 'bytes=0-99'})
        await app(scope, _noop_receive, send)

        start = next(e for e in events if e['type'] == 'http.response.start')
        assert start['status'] == 206
        assert all(e.get('type') != 'http.response.pathsend' for e in events)

    async def test_small_file_does_not_use_pathsend(self, static_dir):
        """Small files (≤ ``_CACHE_MAX_BYTES_PER_FILE``) stay on the
        in-memory body path even when pathsend is advertised — the body
        is read into memory either way, so there's no point handing
        the file path to the sender for a second open."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        events: list = []

        async def send(event):
            events.append(event)

        await app(_scope_with_pathsend('/hello.txt'), _noop_receive, send)

        assert all(e.get('type') != 'http.response.pathsend' for e in events)
        body = b''.join(e.get('body', b'') for e in events
                        if e.get('type') == 'http.response.body')
        assert body == b'Hello, static world!'


# ---------------------------------------------------------------------------
# 1.21c — malformed / unsupported Range must never 500
# ---------------------------------------------------------------------------

def _headers_of(start: dict) -> dict[bytes, bytes]:
    return {k.lower(): v for k, v in start.get('headers', [])}


@pytest.mark.asyncio
class TestStaticFilesMalformedRange:
    """A crafted Range header must be ignored (200) or answered 416 — never 500.

    Bug 1.21c: bare ``int()`` on the range bounds raised ``ValueError`` out
    of ``_serve`` on inputs like ``bytes=abc-def``.
    """

    FILE = b'Hello, static world!'  # 20 bytes, matches static_dir fixture

    @pytest.mark.parametrize('bad_range', [
        'bytes=abc-def',
        'bytes=1-2-3',
        'bytes=xyz',
        'bytes=',
        'items=0-3',          # non-bytes unit
        'bytes=0-1,5-9',      # multi-range (we don't emit multipart/byteranges)
    ])
    async def test_malformed_range_serves_full_200(self, static_dir, bad_range):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, body = await _collect(
            app, _scope(path='/hello.txt', headers={'Range': bad_range})
        )
        assert start['status'] == 200, (
            f'{bad_range!r} must be ignored → 200; got {start["status"]}'
        )
        assert body == self.FILE

    @pytest.mark.parametrize('bad_range', [
        'bytes=--5',          # int('-5') suffix → unsatisfiable
        'bytes=-',
        'bytes= - ',
        'bytes=9999999999999999999999-',
        'bytes=0x10-0x20',
        'bytes=\x00-\x01',
    ])
    async def test_odd_range_never_500(self, static_dir, bad_range):
        """The RFC contract is ignore→200 *or* 416, never a crash."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(
            app, _scope(path='/hello.txt', headers={'Range': bad_range})
        )
        assert start['status'] in (200, 416), (
            f'{bad_range!r} must not 500; got {start["status"]}'
        )

    async def test_unsatisfiable_range_still_416(self, static_dir):
        """A well-formed but out-of-bounds range stays a 416 (not 200/500)."""
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(
            app, _scope(path='/hello.txt', headers={'Range': 'bytes=100-200'})
        )
        assert start['status'] == 416


# ---------------------------------------------------------------------------
# 1.21d — validators (ETag / Last-Modified) + conditional GET
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStaticFilesConditional:
    FILE = b'Hello, static world!'

    async def test_emits_etag_and_last_modified(self, static_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/hello.txt'))
        h = _headers_of(start)
        assert b'etag' in h, 'StaticFiles must emit an ETag'
        assert b'last-modified' in h, 'StaticFiles must emit Last-Modified'

    async def test_if_none_match_returns_304(self, static_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/hello.txt'))
        etag = _headers_of(start)[b'etag']

        start2, body2 = await _collect(
            app, _scope(path='/hello.txt',
                        headers={'If-None-Match': etag.decode()})
        )
        assert start2['status'] == 304
        assert body2 == b''

    async def test_if_none_match_star_returns_304(self, static_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, body = await _collect(
            app, _scope(path='/hello.txt', headers={'If-None-Match': '*'})
        )
        assert start['status'] == 304
        assert body == b''

    async def test_if_none_match_mismatch_serves_200(self, static_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, body = await _collect(
            app, _scope(path='/hello.txt',
                        headers={'If-None-Match': '"stale-tag"'})
        )
        assert start['status'] == 200
        assert body == self.FILE

    async def test_if_modified_since_returns_304(self, static_dir):
        from email.utils import formatdate
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/hello.txt'))
        last_mod = _headers_of(start)[b'last-modified'].decode()

        start2, body2 = await _collect(
            app, _scope(path='/hello.txt',
                        headers={'If-Modified-Since': last_mod})
        )
        assert start2['status'] == 304
        assert body2 == b''

    async def test_if_modified_since_old_date_serves_200(self, static_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, body = await _collect(
            app, _scope(path='/hello.txt',
                        headers={'If-Modified-Since':
                                 'Thu, 01 Jan 1970 00:00:00 GMT'})
        )
        assert start['status'] == 200
        assert body == self.FILE

    async def test_malformed_if_modified_since_serves_200(self, static_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, body = await _collect(
            app, _scope(path='/hello.txt',
                        headers={'If-Modified-Since': 'not-a-date'})
        )
        assert start['status'] == 200
        assert body == self.FILE

    async def test_304_carries_validators(self, static_dir):
        from blackbull.middleware.static import StaticFiles
        app = StaticFiles(directory=str(static_dir))
        start, _ = await _collect(app, _scope(path='/hello.txt'))
        etag = _headers_of(start)[b'etag']
        start2, _ = await _collect(
            app, _scope(path='/hello.txt',
                        headers={'If-None-Match': etag.decode()})
        )
        h = _headers_of(start2)
        assert h.get(b'etag') == etag
        assert b'last-modified' in h
