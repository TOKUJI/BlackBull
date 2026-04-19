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

from blackbull.server.headers import Headers


def _scope(method: str = 'GET', path: str = '/',
           headers: dict[str, str] | None = None) -> dict:
    raw = [(k.lower().encode(), v.encode())
           for k, v in (headers or {}).items()]
    return {
        'type': 'http',
        'method': method,
        'path': path,
        'headers': Headers(raw),
    }


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
