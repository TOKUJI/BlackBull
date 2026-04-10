"""
Tests for blackbull/middlewares.py
===================================

websocket()
-----------
The ``websocket`` middleware must:
  * Accept a first ``receive`` event whose ``type`` is ``'websocket.connect'``
    regardless of any additional keys in the dict (ASGI spec allows extra fields).
  * Reject (raise ``ValueError``) when the first event has a different or absent
    ``type``.
  * Send ``websocket.accept`` before delegating to ``inner``.
  * Send ``websocket.close`` after ``inner`` returns.

P1 bug: the original implementation uses ``msg != {'type': 'websocket.connect'}``
(exact dict equality), which incorrectly rejects valid ASGI connect events that
carry extra fields such as ``headers`` or ``subprotocols``.  The fix is to check
``msg.get('type') != 'websocket.connect'`` instead.
"""

import pytest
from blackbull.middlewares import websocket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_receive(*events):
    """Return an async callable that pops and returns events in order."""
    queue = list(events)

    async def receive():
        return queue.pop(0)

    return receive


async def noop_send(event):
    pass


async def noop_inner(scope, receive, send):
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebsocketMiddleware:

    # --- happy-path acceptance -------------------------------------------------

    async def test_plain_connect_event_is_accepted(self):
        """Exact ``{'type': 'websocket.connect'}`` must be accepted."""
        called = []

        async def inner(scope, receive, send):
            called.append(True)

        receive = make_receive({'type': 'websocket.connect'})
        await websocket({}, receive, noop_send, inner)
        assert called

    async def test_connect_event_with_extra_headers_is_accepted(self):
        """Connect event carrying a ``headers`` field must still be accepted.

        This is the P1 bug: exact dict equality rejects this valid ASGI event.
        The fix (``msg.get('type') != 'websocket.connect'``) accepts it.
        """
        called = []

        async def inner(scope, receive, send):
            called.append(True)

        connect = {
            'type': 'websocket.connect',
            'headers': [(b'host', b'localhost'), (b'origin', b'https://example.com')],
        }
        receive = make_receive(connect)
        await websocket({}, receive, noop_send, inner)
        assert called

    async def test_connect_event_with_subprotocols_is_accepted(self):
        """Connect event carrying a ``subprotocols`` field must be accepted."""
        called = []

        async def inner(scope, receive, send):
            called.append(True)

        connect = {
            'type': 'websocket.connect',
            'subprotocols': ['chat', 'superchat'],
        }
        receive = make_receive(connect)
        await websocket({}, receive, noop_send, inner)
        assert called

    async def test_connect_event_with_multiple_extra_keys_is_accepted(self):
        """Any combination of extra ASGI fields must not cause rejection."""
        called = []

        async def inner(scope, receive, send):
            called.append(True)

        connect = {
            'type': 'websocket.connect',
            'headers': [(b'host', b'localhost')],
            'subprotocols': ['v1'],
            'extensions': {'permessage-deflate': {}},
        }
        receive = make_receive(connect)
        await websocket({}, receive, noop_send, inner)
        assert called

    # --- rejection -------------------------------------------------------------

    async def test_non_connect_type_raises_value_error(self):
        """First event with wrong type must raise ``ValueError``."""
        receive = make_receive({'type': 'websocket.receive', 'text': 'hi'})
        with pytest.raises(ValueError):
            await websocket({}, receive, noop_send, noop_inner)

    async def test_missing_type_key_raises_value_error(self):
        """First event with no ``type`` key must raise ``ValueError``."""
        receive = make_receive({})
        with pytest.raises(ValueError):
            await websocket({}, receive, noop_send, noop_inner)

    async def test_http_request_event_raises_value_error(self):
        """An HTTP request event must raise ``ValueError`` (wrong protocol)."""
        receive = make_receive({'type': 'http.request', 'body': b''})
        with pytest.raises(ValueError):
            await websocket({}, receive, noop_send, noop_inner)

    # --- ordering of accept / inner / close ------------------------------------

    async def test_accept_is_sent_before_inner_is_called(self):
        """``websocket.accept`` must be sent before ``inner`` is invoked."""
        order = []

        async def tracking_send(event):
            order.append(event.get('type'))

        async def inner(scope, receive, send):
            order.append('inner')

        receive = make_receive({'type': 'websocket.connect'})
        await websocket({}, receive, tracking_send, inner)

        assert 'websocket.accept' in order
        assert 'inner' in order
        assert order.index('websocket.accept') < order.index('inner')

    async def test_close_is_sent_after_inner_returns(self):
        """``websocket.close`` must be sent after ``inner`` returns."""
        order = []

        async def tracking_send(event):
            order.append(event.get('type'))

        async def inner(scope, receive, send):
            order.append('inner')

        receive = make_receive({'type': 'websocket.connect'})
        await websocket({}, receive, tracking_send, inner)

        assert 'websocket.close' in order
        assert 'inner' in order
        assert order.index('inner') < order.index('websocket.close')

    async def test_full_sequence_is_accept_then_inner_then_close(self):
        """Complete event sequence must be: accept → inner → close."""
        order = []

        async def tracking_send(event):
            order.append(event.get('type'))

        async def inner(scope, receive, send):
            order.append('inner')

        receive = make_receive({'type': 'websocket.connect'})
        await websocket({}, receive, tracking_send, inner)

        assert order == ['websocket.accept', 'inner', 'websocket.close']
