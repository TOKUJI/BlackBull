"""`match` on the HTTP/1.1 server side — a filter and a guard.

The consistency sweep found `WaitForRequest` had no `match` grammar where
`WaitForClientFrame` does.  Porting it literally would have changed what
the word means: on HTTP/2 the executor skips non-matching frames and keeps
waiting, which is harmless because streams are independent.  On HTTP/1.1
requests are positional (RFC 9112 §9.3), so a skipped head is one the
scenario can never answer — everything after it is off by one.

So there are two steps, because there are two questions:

* `WaitForRequest(match=)` — *which* request do I want to break?  Filters a
  pipeline, skips what it passes over, and records the skips because they
  desync the connection.
* `ExpectRequest(match=)` — *is the client behaving as I assumed?*  Reads
  one head, skips nothing, records the verdict.
"""
from __future__ import annotations

import pytest

from blackbull.fault_injection import request_matches

pytestmark = pytest.mark.asyncio

_HEAD = (b'POST /submit HTTP/1.1\r\nHost: a\r\n'
         b'Expect: 100-continue\r\n\r\n')


class TestTheGrammar:
    async def test_the_recognised_keys(self):
        assert request_matches(_HEAD, {'method': 'POST'})
        assert request_matches(_HEAD, {'target': '/submit'})
        assert request_matches(_HEAD, {'version': 'HTTP/1.1'})
        assert request_matches(_HEAD, {'header': ('expect', '100-continue')})
        assert request_matches(_HEAD, {'header': ('host', None)})
        assert request_matches(_HEAD, {'header_absent': 'x-nope'})

    async def test_an_empty_match_matches_anything(self):
        """The pre-existing `WaitForRequest()` keeps its meaning."""
        assert request_matches(_HEAD, {})
        assert request_matches(b'nonsense\r\n\r\n', {})

    async def test_an_unknown_key_fails_closed(self):
        """A typo must not silently match — the rule `frame_matches` set."""
        assert not request_matches(_HEAD, {'methd': 'POST'})
        assert not request_matches(_HEAD, {'method': 'POST', 'oops': 1})

    async def test_a_malformed_request_line_does_not_raise(self):
        """A scenario may well be waiting for exactly that."""
        assert not request_matches(b'\r\n\r\n', {'method': 'GET'})
