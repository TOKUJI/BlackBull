"""Property tests for simplified handler return-type mapping (guide.md §3.4).

Verifies that every documented return type produces the correct response
across the full input space, not just hand-picked examples.
"""
import asyncio
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from blackbull.app import _wrap_send
from blackbull.router import _adapt_handler
from .strategies import json_value


# ---------------------------------------------------------------------------
# Fake send that captures body bytes after passing through _wrap_send
# ---------------------------------------------------------------------------

def _make_send():
    """Return a (_wrap_send-wrapped send, body_parts list).

    _adapt_handler passes Response/JSONResponse objects to send; _wrap_send
    decomposes them into standard ASGI ``http.response.start`` +
    ``http.response.body`` event dicts on the underlying raw_send.
    """
    body_parts = []

    async def raw_send(event):
        if isinstance(event, dict) and event.get('type') == 'http.response.body':
            body = event.get('body', b'')
            if body:
                body_parts.append(bytes(body))

    return _wrap_send(raw_send), body_parts


def _run_handler(fn, path='/'):
    wrapped = _adapt_handler(fn, path)

    async def _go():
        send, body_parts = _make_send()
        await wrapped({}, lambda: None, send)
        return body_parts

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

@pytest.mark.properties
@given(obj=st.dictionaries(st.text(), st.integers()))
def test_dict_return_becomes_json_response(obj):
    async def handler():
        return obj

    parts = _run_handler(handler)
    assert json.loads(b''.join(parts)) == obj


@pytest.mark.properties
@given(obj=st.lists(st.integers(), max_size=20))
def test_list_return_becomes_json_response(obj):
    async def handler():
        return obj

    parts = _run_handler(handler)
    assert json.loads(b''.join(parts)) == obj


@pytest.mark.properties
@given(s=st.text())
def test_str_return_becomes_html_response(s):
    async def handler():
        return s

    parts = _run_handler(handler)
    assert b''.join(parts) == s.encode()


@pytest.mark.properties
@given(b=st.binary())
def test_bytes_return_becomes_response(b):
    async def handler():
        return b

    parts = _run_handler(handler)
    assert b''.join(parts) == b


@pytest.mark.properties
def test_none_return_sends_nothing():
    async def handler():
        return None

    parts = _run_handler(handler)
    assert parts == []


@pytest.mark.properties
@given(obj=json_value)
@settings(max_examples=50)
def test_any_json_value_roundtrips(obj):
    """dict and list values both produce valid JSON with the original value preserved."""
    if not isinstance(obj, (dict, list)):
        return  # only dict/list are auto-JSON-encoded

    async def handler():
        return obj

    parts = _run_handler(handler)
    assert json.loads(b''.join(parts)) == obj
