import pytest

from blackbull.request import read_body, parse_cookies
from blackbull.server.headers import Headers


# ---------------------------------------------------------------------------
# read_body
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_body_single_chunk():
    async def receive():
        return {'body': b'hello', 'more_body': False}

    assert await read_body(receive) == b'hello'


@pytest.mark.asyncio
async def test_read_body_multiple_chunks():
    chunks = [
        {'body': b'hel', 'more_body': True},
        {'body': b'lo', 'more_body': False},
    ]
    it = iter(chunks)

    async def receive():
        return next(it)

    assert await read_body(receive) == b'hello'


@pytest.mark.asyncio
async def test_read_body_empty():
    async def receive():
        return {'body': b'', 'more_body': False}

    assert await read_body(receive) == b''


@pytest.mark.asyncio
async def test_read_body_missing_body_key():
    async def receive():
        return {'more_body': False}

    assert await read_body(receive) == b''


# ---------------------------------------------------------------------------
# parse_cookies
# ---------------------------------------------------------------------------

def test_parse_cookies_empty_header():
    scope = {'headers': Headers([])}
    assert parse_cookies(scope) == {}


def test_parse_cookies_single():
    scope = {'headers': Headers([(b'cookie', b'session_id=abc123')])}
    assert parse_cookies(scope) == {'session_id': 'abc123'}


def test_parse_cookies_multiple():
    scope = {'headers': Headers([(b'cookie', b'a=1; b=2; c=3')])}
    result = parse_cookies(scope)
    assert result == {'a': '1', 'b': '2', 'c': '3'}


def test_parse_cookies_strips_whitespace():
    scope = {'headers': Headers([(b'cookie', b'  key = value ')])}
    assert parse_cookies(scope) == {'key': 'value'}


def test_parse_cookies_no_cookie_header():
    scope = {'headers': Headers([(b'content-type', b'application/json')])}
    assert parse_cookies(scope) == {}
