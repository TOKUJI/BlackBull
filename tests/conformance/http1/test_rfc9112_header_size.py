"""Header-size limits — DoS defence (RFC 9112 §11.2, RFC 6585 §5).

Without per-line / total-block limits, a single peer could:

* send a 1 GB header line (``X-Foo: AAAA…``) and hold it in
  ``readuntil``'s buffer until OOM kills the worker, or
* send thousands of small headers totalling hundreds of megabytes for
  the same effect.

BlackBull caps each line at ``BB_HEADER_MAX_LINE`` (default 8 KiB) and
the whole block at ``BB_HEADER_MAX_TOTAL`` (default 64 KiB).  Exceeding
either limit yields 431 Request Header Fields Too Large per RFC 6585 §5.

These tests use the default 8 KiB / 64 KiB limits — no env override —
so the suite stays fast and the defaults are the thing under test.
"""
import pytest

from .conftest import send_raw


@pytest.mark.integration
class TestHeaderLineLimit:
    """``BB_HEADER_MAX_LINE`` — single header line limit."""

    def test_normal_header_line_accepted(self, h1_app):
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'X-Normal: ' + b'a' * 200 + b'\r\n'
                     b'\r\n')
        assert r.status == 200

    def test_8k_header_line_rejected(self, h1_app):
        """A single header value of 9 KiB — just over the 8 KiB default."""
        big_value = b'A' * (9 * 1024)
        r = send_raw('127.0.0.1', h1_app.port,
                     b'GET / HTTP/1.1\r\n'
                     b'Host: localhost\r\n'
                     b'X-Big: ' + big_value + b'\r\n'
                     b'\r\n',
                     read_max=65536)
        # 431 expected; asyncio's own LimitOverrunError may also kick in
        # and surface as a close — either is acceptable.
        assert r.status in (431, None), (
            f'huge header line MUST be rejected; got {r.status}')


@pytest.mark.integration
class TestHeaderTotalLimit:
    """``BB_HEADER_MAX_TOTAL`` — whole header block limit."""

    def test_many_small_headers_accepted(self, h1_app):
        """50 × 100-byte headers ≈ 5 KiB, well under the 64 KiB limit."""
        lines = [b'GET / HTTP/1.1', b'Host: localhost']
        for i in range(50):
            lines.append(b'X-H-' + str(i).encode() + b': ' + b'a' * 80)
        lines.extend([b'', b''])
        r = send_raw('127.0.0.1', h1_app.port, b'\r\n'.join(lines))
        assert r.status == 200

    def test_total_header_block_at_limit_rejected(self, h1_app):
        """Many short header lines whose *sum* exceeds the 64 KiB total."""
        # 100 lines of ~700 bytes each ≈ 70 KiB.  Each line is well under
        # the per-line 8 KiB cap so this tests the *total* cap specifically.
        lines = [b'GET / HTTP/1.1', b'Host: localhost']
        for i in range(120):
            lines.append(b'X-Filler-' + str(i).encode() + b': ' + b'a' * 600)
        lines.extend([b'', b''])
        r = send_raw('127.0.0.1', h1_app.port,
                     b'\r\n'.join(lines),
                     read_max=65536)
        # Either 431 from our explicit limit OR a clean close from
        # asyncio's own LimitOverrunError.
        assert r.status in (431, None)
