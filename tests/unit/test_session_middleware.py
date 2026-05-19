"""Unit tests for the signed-cookie session middleware.

These tests use a stub ``call_next`` and a captured ``send`` so the
middleware can be exercised without spinning up a server.  Integration
coverage (real HTTP round-trip) lives in ``tests/integration``.
"""
import os
from unittest.mock import patch

import pytest

from blackbull.middleware.session import (
    Session,
    _SessionDict,
    _parse_cookie_header,
)


SECRET = b'test-secret-32-bytes-of-random-x' * 1


# ---------------------------------------------------------------------------
# Construction / configuration
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_explicit_secret_accepted(self):
        mw = Session(secret=SECRET)
        assert mw._secret == SECRET

    def test_str_secret_encoded(self):
        mw = Session(secret='hello')
        assert mw._secret == b'hello'

    def test_secret_from_env(self):
        with patch.dict(os.environ, {'BB_SESSION_SECRET': 'env-secret'}, clear=False):
            mw = Session()
            assert mw._secret == b'env-secret'

    def test_missing_secret_raises(self):
        env = {k: v for k, v in os.environ.items() if k != 'BB_SESSION_SECRET'}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match='requires a secret'):
                Session()

    def test_empty_string_secret_raises(self):
        env = {k: v for k, v in os.environ.items() if k != 'BB_SESSION_SECRET'}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError):
                Session(secret='')

    def test_invalid_samesite_raises(self):
        with pytest.raises(ValueError):
            Session(secret=SECRET, samesite='Bogus')

    def test_samesite_none_allowed(self):
        Session(secret=SECRET, samesite=None)  # no exception


# ---------------------------------------------------------------------------
# Encode / decode round-trip
# ---------------------------------------------------------------------------

class TestEncodeDecode:
    def test_round_trip_simple_payload(self):
        mw = Session(secret=SECRET)
        encoded = mw._encode({'user': 'alice', 'count': 3})
        assert mw._decode(encoded) == {'user': 'alice', 'count': 3}

    def test_round_trip_empty_dict(self):
        mw = Session(secret=SECRET)
        encoded = mw._encode({})
        assert mw._decode(encoded) == {}

    def test_tampered_payload_returns_empty(self):
        mw = Session(secret=SECRET)
        encoded = mw._encode({'user': 'alice'})
        # Flip the first byte of the payload portion.
        payload_b64, _, mac = encoded.partition(b'.')
        tampered = (b'X' + payload_b64[1:]) + b'.' + mac
        assert mw._decode(tampered) == {}

    def test_tampered_mac_returns_empty(self):
        mw = Session(secret=SECRET)
        encoded = mw._encode({'user': 'alice'})
        payload_b64, _, mac = encoded.partition(b'.')
        tampered = payload_b64 + b'.' + b'0' * len(mac)
        assert mw._decode(tampered) == {}

    def test_wrong_secret_returns_empty(self):
        a = Session(secret=b'one')
        b = Session(secret=b'two')
        encoded = a._encode({'user': 'alice'})
        assert b._decode(encoded) == {}

    def test_malformed_cookie_returns_empty(self):
        mw = Session(secret=SECRET)
        assert mw._decode(b'not-a-valid-cookie') == {}
        assert mw._decode(b'') == {}
        assert mw._decode(b'.') == {}
        assert mw._decode(b'AAA.') == {}
        assert mw._decode(b'.AAA') == {}

    def test_non_dict_payload_rejected(self):
        """A signed cookie carrying a JSON list (not dict) should not crash."""
        import base64, hmac, hashlib, json
        mw = Session(secret=SECRET)
        payload = json.dumps([1, 2, 3]).encode()
        mac = hmac.new(SECRET, payload, hashlib.sha256).hexdigest().encode()
        payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b'=')
        encoded = payload_b64 + b'.' + mac
        assert mw._decode(encoded) == {}


class TestMaxAge:
    def test_max_age_includes_ts_in_payload(self):
        mw = Session(secret=SECRET, max_age=60)
        encoded = mw._encode({'k': 'v'})
        # _ts should be stripped on decode but signed in.
        assert mw._decode(encoded) == {'k': 'v'}

    def test_expired_cookie_returns_empty(self):
        mw = Session(secret=SECRET, max_age=60)
        # Build a cookie with an old timestamp by patching time.time on encode.
        with patch('blackbull.middleware.session.time.time', return_value=1_000_000.0):
            encoded = mw._encode({'k': 'v'})
        # Now decode 'now' is well past max_age window
        with patch('blackbull.middleware.session.time.time', return_value=1_000_000.0 + 120):
            assert mw._decode(encoded) == {}

    def test_fresh_cookie_within_window_accepted(self):
        mw = Session(secret=SECRET, max_age=60)
        with patch('blackbull.middleware.session.time.time', return_value=2_000_000.0):
            encoded = mw._encode({'k': 'v'})
        with patch('blackbull.middleware.session.time.time', return_value=2_000_000.0 + 30):
            assert mw._decode(encoded) == {'k': 'v'}


# ---------------------------------------------------------------------------
# Cookie parsing
# ---------------------------------------------------------------------------

class TestCookieParser:
    def test_single_pair(self):
        assert _parse_cookie_header(b'foo=bar') == {b'foo': b'bar'}

    def test_multiple_pairs(self):
        h = b'foo=bar; baz=qux; session=abc.def'
        out = _parse_cookie_header(h)
        assert out == {b'foo': b'bar', b'baz': b'qux', b'session': b'abc.def'}

    def test_leading_trailing_whitespace_trimmed(self):
        h = b'  foo  =  bar  ;  baz=qux  '
        out = _parse_cookie_header(h)
        assert out[b'foo'] == b'bar'
        assert out[b'baz'] == b'qux'

    def test_empty_segments_ignored(self):
        h = b';;;foo=bar;;'
        assert _parse_cookie_header(h) == {b'foo': b'bar'}

    def test_no_equals_ignored(self):
        h = b'foo=bar; nope; baz=qux'
        out = _parse_cookie_header(h)
        assert out == {b'foo': b'bar', b'baz': b'qux'}

    def test_first_occurrence_wins(self):
        """RFC 6265 §5.4 step 11 — leftmost cookie with a given name wins."""
        h = b'foo=first; foo=second'
        assert _parse_cookie_header(h) == {b'foo': b'first'}


# ---------------------------------------------------------------------------
# Modified-flag dict
# ---------------------------------------------------------------------------

class TestSessionDict:
    def test_setitem_marks_modified(self):
        d = _SessionDict()
        assert d._modified is False
        d['x'] = 1
        assert d._modified is True

    def test_delitem_marks_modified(self):
        d = _SessionDict({'x': 1})
        assert d._modified is False
        del d['x']
        assert d._modified is True

    def test_clear_on_nonempty_marks_modified(self):
        d = _SessionDict({'x': 1})
        d.clear()
        assert d._modified is True

    def test_clear_on_empty_does_not_mark(self):
        d = _SessionDict()
        d.clear()
        assert d._modified is False

    def test_update_only_marks_if_changes_contents(self):
        d = _SessionDict({'x': 1})
        d.update({'x': 1})  # same value
        assert d._modified is False
        d.update({'x': 2})
        assert d._modified is True

    def test_setdefault_existing_does_not_mark(self):
        d = _SessionDict({'x': 1})
        d.setdefault('x', 99)
        assert d._modified is False

    def test_setdefault_new_marks_modified(self):
        d = _SessionDict()
        d.setdefault('x', 1)
        assert d._modified is True

    def test_pop_existing_marks_modified(self):
        d = _SessionDict({'x': 1})
        d.pop('x')
        assert d._modified is True

    def test_pop_missing_with_default_does_not_mark(self):
        d = _SessionDict()
        d.pop('x', None)
        assert d._modified is False


# ---------------------------------------------------------------------------
# Full middleware __call__
# ---------------------------------------------------------------------------

async def _run_mw(mw, scope, app_callable):
    """Drive ``mw`` against a fake call_next that runs ``app_callable``.

    Returns the captured ``send`` events.
    """
    sent: list = []

    async def send(event):
        sent.append(event)

    async def call_next(s, r, se):
        await app_callable(s, r, se)

    await mw(scope, None, send, call_next)
    return sent


def _make_scope(cookie: bytes = b'', type_: str = 'http') -> dict:
    headers = [(b'host', b'localhost')]
    if cookie:
        headers.append((b'cookie', cookie))
    return {
        'type': type_,
        'method': 'GET',
        'path': '/',
        'headers': headers,
    }


@pytest.mark.asyncio
class TestMiddlewareCall:
    async def test_no_cookie_yields_empty_session(self):
        mw = Session(secret=SECRET)
        captured = {}

        async def app(scope, receive, send):
            captured['session'] = scope['session']
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        await _run_mw(mw, _make_scope(), app)
        assert captured['session'] == {}

    async def test_unmodified_session_does_not_set_cookie(self):
        mw = Session(secret=SECRET)

        async def app(scope, receive, send):
            _ = scope['session']        # read only
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        events = await _run_mw(mw, _make_scope(), app)
        starts = [e for e in events if e.get('type') == 'http.response.start']
        assert starts
        set_cookies = [v for k, v in starts[0]['headers'] if k.lower() == b'set-cookie']
        assert set_cookies == [], 'unmodified session must NOT emit Set-Cookie'

    async def test_modified_session_sets_cookie(self):
        mw = Session(secret=SECRET)

        async def app(scope, receive, send):
            scope['session']['user'] = 'alice'
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        events = await _run_mw(mw, _make_scope(), app)
        starts = [e for e in events if e.get('type') == 'http.response.start']
        set_cookies = [v for k, v in starts[0]['headers'] if k.lower() == b'set-cookie']
        assert len(set_cookies) == 1
        assert set_cookies[0].startswith(b'session=')
        assert b'Path=/' in set_cookies[0]
        assert b'HttpOnly' in set_cookies[0]
        assert b'Secure' in set_cookies[0]
        assert b'SameSite=Lax' in set_cookies[0]

    async def test_round_trip_via_cookie(self):
        """First request sets session, second request reads it back."""
        mw = Session(secret=SECRET)

        async def writer(scope, receive, send):
            scope['session']['count'] = 7
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        events = await _run_mw(mw, _make_scope(), writer)
        set_cookie = next(
            v for k, v in events[0]['headers'] if k.lower() == b'set-cookie')
        # Extract just ``name=value`` (before the first ``;``)
        cookie_pair = set_cookie.split(b';', 1)[0]

        captured = {}

        async def reader(scope, receive, send):
            captured['session'] = dict(scope['session'])
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        await _run_mw(mw, _make_scope(cookie=cookie_pair), reader)
        assert captured['session'] == {'count': 7}

    async def test_emptying_session_emits_tombstone(self):
        """Clearing the session must emit a deletion cookie (Max-Age=0)."""
        mw = Session(secret=SECRET)

        # Build a cookie first.
        encoded = mw._encode({'user': 'alice'})
        cookie = b'session=' + encoded

        async def app(scope, receive, send):
            scope['session'].clear()
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        events = await _run_mw(mw, _make_scope(cookie=cookie), app)
        set_cookie = next(
            v for k, v in events[0]['headers'] if k.lower() == b'set-cookie')
        assert b'Max-Age=0' in set_cookie, (
            f'cleared session must emit deletion cookie; got {set_cookie!r}')

    async def test_websocket_scope_gets_session_but_no_cookie_write(self):
        mw = Session(secret=SECRET)
        captured = {}

        async def app(scope, receive, send):
            captured['type'] = scope['type']
            captured['session'] = scope['session']
            scope['session']['x'] = 1  # mutation has no wire effect for WS

        await _run_mw(mw, _make_scope(type_='websocket'), app)
        assert captured['type'] == 'websocket'
        assert isinstance(captured['session'], dict)

    async def test_lifespan_scope_skips_middleware(self):
        """Lifespan events MUST NOT have ``scope['session']`` injected."""
        mw = Session(secret=SECRET)
        captured = {}

        async def app(scope, receive, send):
            captured['session'] = scope.get('session')

        await _run_mw(mw, {'type': 'lifespan', 'headers': []}, app)
        assert captured['session'] is None


@pytest.mark.asyncio
class TestSecureFlagDefaults:
    async def test_secure_false_does_not_emit_secure(self):
        mw = Session(secret=SECRET, secure=False)

        async def app(scope, receive, send):
            scope['session']['k'] = 'v'
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        events = await _run_mw(mw, _make_scope(), app)
        cookie = next(v for k, v in events[0]['headers'] if k.lower() == b'set-cookie')
        assert b'Secure' not in cookie

    async def test_httponly_false_does_not_emit_httponly(self):
        mw = Session(secret=SECRET, httponly=False)

        async def app(scope, receive, send):
            scope['session']['k'] = 'v'
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        events = await _run_mw(mw, _make_scope(), app)
        cookie = next(v for k, v in events[0]['headers'] if k.lower() == b'set-cookie')
        assert b'HttpOnly' not in cookie
