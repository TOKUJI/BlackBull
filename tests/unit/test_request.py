import pytest

from blackbull.request import (read_body, read_json, read_text, parse_cookies,
                               stream_body, ClientDisconnected)
from blackbull.headers import Headers


def _receive_from(*chunks):
    """Build an ASGI receive() that yields the given (body, more_body) events.

    A single bytes argument is wrapped as one final chunk.
    """
    if len(chunks) == 1 and isinstance(chunks[0], (bytes, bytearray)):
        events = [{'body': bytes(chunks[0]), 'more_body': False}]
    else:
        events = list(chunks)
    it = iter(events)

    async def receive():
        return next(it)

    return receive


# ---------------------------------------------------------------------------
# read_json
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_json_object():
    assert await read_json(_receive_from(b'{"a": 1, "b": [2, 3]}')) == {'a': 1, 'b': [2, 3]}


@pytest.mark.asyncio
async def test_read_json_array():
    assert await read_json(_receive_from(b'[1, 2, 3]')) == [1, 2, 3]


@pytest.mark.asyncio
async def test_read_json_empty_body_returns_none():
    assert await read_json(_receive_from(b'')) is None


@pytest.mark.asyncio
async def test_read_json_invalid_returns_none():
    assert await read_json(_receive_from(b'{not json')) is None


@pytest.mark.asyncio
async def test_read_json_invalid_utf8_returns_none():
    # Lone continuation byte — not decodable as UTF-8, must not raise.
    assert await read_json(_receive_from(b'\xff\xfe')) is None


@pytest.mark.asyncio
async def test_read_json_reassembles_multiple_chunks():
    received = await read_json(_receive_from(
        {'body': b'{"x":', 'more_body': True},
        {'body': b' 42}', 'more_body': False},
    ))
    assert received == {'x': 42}


# ---------------------------------------------------------------------------
# read_text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_text_decodes_utf8():
    assert await read_text(_receive_from('héllo'.encode())) == 'héllo'


@pytest.mark.asyncio
async def test_read_text_empty_body():
    assert await read_text(_receive_from(b'')) == ''


@pytest.mark.asyncio
async def test_read_text_invalid_utf8_uses_replacement():
    # errors='replace' → never raises; undecodable bytes become U+FFFD.
    result = await read_text(_receive_from(b'a\xffb'))
    assert result == 'a�b'


@pytest.mark.asyncio
async def test_read_text_honours_encoding_argument():
    assert await read_text(_receive_from('café'.encode('latin-1')),
                           encoding='latin-1') == 'café'


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


@pytest.mark.asyncio
async def test_read_body_single_chunk_is_returned_without_copy():
    """P1 fast path: a single-chunk body is returned as the *same* object,
    not an O(n²)-accumulated copy."""
    payload = b'x' * 4096

    async def receive():
        return {'body': payload, 'more_body': False}

    result = await read_body(receive)
    assert result is payload, 'single-chunk body must avoid an intermediate copy'


@pytest.mark.asyncio
async def test_read_body_three_chunks_join_in_order():
    chunks = [
        {'body': b'one', 'more_body': True},
        {'body': b'two', 'more_body': True},
        {'body': b'three', 'more_body': False},
    ]
    it = iter(chunks)

    async def receive():
        return next(it)

    assert await read_body(receive) == b'onetwothree'


@pytest.mark.asyncio
async def test_read_body_skips_empty_intermediate_chunks():
    """An empty chunk with more_body=True must not corrupt the result and must
    not count toward the single-chunk fast path."""
    chunks = [
        {'body': b'a', 'more_body': True},
        {'body': b'', 'more_body': True},
        {'body': b'b', 'more_body': False},
    ]
    it = iter(chunks)

    async def receive():
        return next(it)

    assert await read_body(receive) == b'ab'


# ---------------------------------------------------------------------------
# stream_body
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_body_yields_each_chunk_in_order():
    received = [c async for c in stream_body(_receive_from(
        {'body': b'one', 'more_body': True},
        {'body': b'two', 'more_body': True},
        {'body': b'three', 'more_body': False},
    ))]
    assert received == [b'one', b'two', b'three']


@pytest.mark.asyncio
async def test_stream_body_skips_empty_intermediate_chunks():
    received = [c async for c in stream_body(_receive_from(
        {'body': b'a', 'more_body': True},
        {'body': b'', 'more_body': True},
        {'body': b'b', 'more_body': False},
    ))]
    assert received == [b'a', b'b']


@pytest.mark.asyncio
async def test_stream_body_empty_body_yields_nothing():
    received = [c async for c in stream_body(_receive_from(b''))]
    assert received == []


@pytest.mark.asyncio
async def test_stream_body_never_materializes_whole_payload():
    """The streaming contract: the sum equals the payload, but no single yield
    is larger than one source chunk (the whole body is never joined)."""
    total = 0
    max_chunk = 0
    async for chunk in stream_body(_receive_from(
        {'body': b'x' * 4096, 'more_body': True},
        {'body': b'y' * 4096, 'more_body': True},
        {'body': b'z' * 4096, 'more_body': False},
    )):
        total += len(chunk)
        max_chunk = max(max_chunk, len(chunk))
    assert total == 3 * 4096
    assert max_chunk == 4096


@pytest.mark.asyncio
async def test_stream_body_disconnect_mid_body_raises():
    received = []
    with pytest.raises(ClientDisconnected):
        async for chunk in stream_body(_receive_from(
            {'body': b'partial', 'more_body': True},
            {'type': 'http.disconnect'},
        )):
            received.append(chunk)
    assert received == [b'partial']  # chunks before the disconnect were delivered


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


def test_parse_cookies_multiple_http2_fields():
    # RFC 7540 §8.1.2.5: HTTP/2 sends each cookie as a separate header field.
    # All fields must be concatenated with "; " before parsing.
    scope = {'headers': Headers([
        (b'cookie', b'session_id=abc'),
        (b'cookie', b'chat_method=poll'),
        (b'cookie', b'other=xyz'),
    ])}
    assert parse_cookies(scope) == {
        'session_id': 'abc',
        'chat_method': 'poll',
        'other': 'xyz',
    }
