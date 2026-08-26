"""A Host header the parser cannot decode must be a 400, not a dropped call.

`_parse_host_header` calls `value.decode('utf-8')` in five places with no
guard.  A high byte reaches it because neither of the two checks in front
excludes one: `_HOST_FORBIDDEN_RE` looks for `/ ? #` and whitespace, and
the CTL check covers `\\x00-\\x08\\x0a-\\x1f\\x7f` — `\\xff` is in neither
set.

The resulting `UnicodeDecodeError` is not caught anywhere in `run()`, so
the connection closes with no bytes written.  A client sees "empty reply
from server"; nginx answers 400.  Failing to answer is worse than
answering wrongly: the caller cannot tell a rejection from a crash.
"""
from __future__ import annotations

import pytest

from blackbull.headers import Headers
from blackbull.server.http1_actor import (
    BadRequestError, _parse_host_header, _validate_host,
)


def _headers(value: bytes) -> Headers:
    return Headers([(b'host', value)])


class TestANonDecodableHostIsRejected:
    @pytest.mark.parametrize('value', [
        b'\xff',                       # bare high byte
        b'example.com\xff',            # trailing
        b'\xffexample.com',            # leading
        b'ex\xffample.com:8080',       # mid-authority, with a port
        b'\xc3\x28',                   # valid-looking lead byte, bad continuation
    ])
    def test_validate_rejects_it(self, value):
        with pytest.raises(BadRequestError):
            _validate_host(_headers(value))

    @pytest.mark.parametrize('value', [
        b'\xff', b'example.com\xff', b'ex\xffample.com:8080',
    ])
    def test_the_parser_never_sees_it(self, value):
        """Belt and braces: even called directly it must not raise.

        `_validate_host` is the gate, but the parser is reachable from
        other call sites and a bare `decode` there is a latent repeat of
        the same defect.
        """
        host, port = _parse_host_header(value, 80)
        assert isinstance(host, str)
        assert isinstance(port, int)


class TestValidHostsStillWork:
    """The fix must not narrow what was already accepted."""

    @pytest.mark.parametrize('value,host,port', [
        (b'example.com', 'example.com', 80),
        (b'example.com:8080', 'example.com', 8080),
        (b'[::1]:8100', '::1', 8100),
        (b'[::1]', '::1', 80),
        (b'127.0.0.1:9', '127.0.0.1', 9),
        (b'xn--n3h.example', 'xn--n3h.example', 80),   # punycode is ASCII
    ])
    def test_accepted(self, value, host, port):
        _validate_host(_headers(value))                # must not raise
        assert _parse_host_header(value, 80) == (host, port)

    def test_the_existing_delimiter_rule_is_unchanged(self):
        with pytest.raises(BadRequestError):
            _validate_host(_headers(b'0/0'))
        with pytest.raises(BadRequestError):
            _validate_host(_headers(b''))
