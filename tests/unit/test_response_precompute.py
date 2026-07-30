"""Response-side precomputation must be byte-identical to what it replaces.

Two per-response allocations are answered from tables built once at import:
the status line (`f'HTTP/1.1 {status} {status.phrase}'.encode()`) and small
Content-Length values (`str(n).encode()`).  A table is only safe if it agrees
with the expression it replaced on *every* input, including the ones that fall
off the end of it — so these tests check the tables against the original
expressions rather than against a restatement of the table.
"""
from http import HTTPStatus

import pytest

from blackbull.server.sender import (
    _CONTENT_LENGTH_CACHE_MAX,
    _content_length_bytes,
    _status_line,
)


# --------------------------------------------------------------------------
# Status line
# --------------------------------------------------------------------------

def _status_line_reference(status: HTTPStatus) -> bytes:
    """The expression as it stood before the table."""
    return f'HTTP/1.1 {status} {status.phrase}'.encode() + b'\r\n'


@pytest.mark.parametrize('status', list(HTTPStatus), ids=lambda s: s.name)
def test_status_line_matches_the_fstring_for_every_member(status):
    assert _status_line(status) == _status_line_reference(status)


def test_status_line_covers_the_whole_enum():
    """No member may fall through to the slow path unnoticed."""
    from blackbull.server.sender import _STATUS_LINES
    assert set(_STATUS_LINES) == set(HTTPStatus)


def test_status_line_falls_back_for_a_status_outside_the_enum():
    """An unregistered code must still render, not raise a KeyError."""
    assert _status_line(599) == b'HTTP/1.1 599 \r\n'


def test_status_line_ends_with_crlf():
    assert _status_line(HTTPStatus.OK) == b'HTTP/1.1 200 OK\r\n'


# --------------------------------------------------------------------------
# Content-Length
# --------------------------------------------------------------------------

@pytest.mark.parametrize('n', [0, 1, 2, 9, 10, 99, 100, 1023, 1024, 4096,
                               _CONTENT_LENGTH_CACHE_MAX - 1,
                               _CONTENT_LENGTH_CACHE_MAX,
                               _CONTENT_LENGTH_CACHE_MAX + 1,
                               65536, 1 << 30])
def test_content_length_matches_str_encode(n):
    assert _content_length_bytes(n) == str(n).encode()


def test_content_length_agrees_across_the_whole_cached_range():
    mismatched = [n for n in range(_CONTENT_LENGTH_CACHE_MAX + 2)
                  if _content_length_bytes(n) != str(n).encode()]
    assert mismatched == []


def test_content_length_has_no_leading_zeros():
    """RFC 9110 §8.6 — a leading zero is a smuggling vector we reject inbound;
    never emit one outbound either."""
    for n in (0, 1, 10, 100, 1000):
        rendered = _content_length_bytes(n)
        assert rendered == b'0' or not rendered.startswith(b'0')
