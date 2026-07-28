"""Positive static proof: the ASGI message unions narrow, and all 19 shapes
construct.

This file is **never executed** — it is input to pyright, driven by
`tests/architecture/test_typing_gate.py`, and must produce **zero**
diagnostics.  Its companion `expect_errors.py` proves the converse.

What is being proven, in order:

1. ``ASGIEvent`` constants are ``Literal``, not ``str`` — without this no
   comparison or ``match`` case against them narrows anything, and the whole
   typing slice would be decorative.
2. ``event['type'] == ASGIEvent.X`` narrows the union to exactly one member.
3. ``match`` mapping patterns narrow the same way (the dispatch idiom the
   server stack actually uses).
4. Every one of the 19 shapes is constructible in its well-formed form and
   accepted by the matching channel callable — a static check of the
   ~100 construction sites' idiom.
"""
from typing import Literal, assert_type

from blackbull.asgi import (
    ASGIEvent,
    ASGIReceiveCallable,
    ASGIReceiveEvent,
    ASGISendCallable,
    ASGISendEvent,
    HTTPDisconnectEvent,
    HTTPRequestEvent,
    HTTPResponseBodyEvent,
    HTTPResponsePathsendEvent,
    HTTPResponsePushEvent,
    HTTPResponseStartEvent,
    HTTPResponseTrailersEvent,
    LifespanShutdownCompleteEvent,
    LifespanShutdownEvent,
    LifespanShutdownFailedEvent,
    LifespanStartupCompleteEvent,
    LifespanStartupEvent,
    LifespanStartupFailedEvent,
    WebSocketAcceptEvent,
    WebSocketCloseEvent,
    WebSocketConnectEvent,
    WebSocketDisconnectEvent,
    WebSocketReceiveEvent,
    WebSocketSendEvent,
)
from blackbull.headers import Headers


# --- 1. The keystone: constants are Literal, not str -----------------------
# `Final` in the class body is what buys this.  If someone drops the `Final`
# annotations, these lines fail and the gate reports it before any of the
# narrowing checks below get a chance to.

def constants_are_literals() -> None:
    assert_type(ASGIEvent.HTTP_REQUEST, Literal['http.request'])
    assert_type(ASGIEvent.HTTP_RESPONSE_START, Literal['http.response.start'])
    assert_type(ASGIEvent.WS_RECEIVE, Literal['websocket.receive'])
    assert_type(ASGIEvent.LIFESPAN_STARTUP, Literal['lifespan.startup'])


# --- 2. Equality narrowing on the receive union ----------------------------

def narrow_receive_by_equality(event: ASGIReceiveEvent) -> None:
    if event['type'] == ASGIEvent.HTTP_REQUEST:
        assert_type(event, HTTPRequestEvent)
        # ...and the narrowed member's own keys are then typed:
        body: bytes = event.get('body', b'')
        more: bool = event.get('more_body', False)
        _ = body, more
    elif event['type'] == ASGIEvent.HTTP_DISCONNECT:
        assert_type(event, HTTPDisconnectEvent)
    elif event['type'] == ASGIEvent.WS_CONNECT:
        assert_type(event, WebSocketConnectEvent)
    elif event['type'] == ASGIEvent.WS_RECEIVE:
        assert_type(event, WebSocketReceiveEvent)
        text: str | None = event.get('text')
        _ = text
    elif event['type'] == ASGIEvent.WS_DISCONNECT:
        assert_type(event, WebSocketDisconnectEvent)
    elif event['type'] == ASGIEvent.LIFESPAN_STARTUP:
        assert_type(event, LifespanStartupEvent)
    else:
        assert_type(event, LifespanShutdownEvent)


# --- 3. `match` mapping-pattern narrowing on the send union ----------------
# This is the shape `server/sender.py` dispatches with, so it is the case
# that actually matters for the production hubs.

def narrow_send_by_match(event: ASGISendEvent) -> None:
    match event['type']:
        case ASGIEvent.HTTP_RESPONSE_START:
            assert_type(event, HTTPResponseStartEvent)
            status: int = event['status']
            _ = status
        case ASGIEvent.HTTP_RESPONSE_BODY:
            assert_type(event, HTTPResponseBodyEvent)
        case ASGIEvent.HTTP_RESPONSE_TRAILERS:
            assert_type(event, HTTPResponseTrailersEvent)
        case ASGIEvent.HTTP_RESPONSE_PUSH:
            assert_type(event, HTTPResponsePushEvent)
        case ASGIEvent.HTTP_RESPONSE_PATHSEND:
            assert_type(event, HTTPResponsePathsendEvent)
            path: str = event['path']
            _ = path
        case ASGIEvent.WS_ACCEPT:
            assert_type(event, WebSocketAcceptEvent)
        case ASGIEvent.WS_SEND:
            assert_type(event, WebSocketSendEvent)
        case ASGIEvent.WS_CLOSE:
            assert_type(event, WebSocketCloseEvent)
        case ASGIEvent.LIFESPAN_STARTUP_COMPLETE:
            assert_type(event, LifespanStartupCompleteEvent)
        case ASGIEvent.LIFESPAN_STARTUP_FAILED:
            assert_type(event, LifespanStartupFailedEvent)
            message: str = event.get('message', '')
            _ = message
        case ASGIEvent.LIFESPAN_SHUTDOWN_COMPLETE:
            assert_type(event, LifespanShutdownCompleteEvent)
        case ASGIEvent.LIFESPAN_SHUTDOWN_FAILED:
            assert_type(event, LifespanShutdownFailedEvent)


# --- 4. All 19 shapes construct and are accepted by the channel ------------

async def construct_all_receive(receive: ASGIReceiveCallable) -> None:
    """Every receive-direction shape is a valid ``ASGIReceiveEvent``."""
    _ = await receive()

    events: list[ASGIReceiveEvent] = [
        {'type': 'http.request'},
        {'type': 'http.request', 'body': b'chunk', 'more_body': True},
        {'type': 'http.disconnect'},
        {'type': 'websocket.connect'},
        # Both keys present with one None — the recipient's actual idiom.
        {'type': 'websocket.receive', 'text': 'hi', 'bytes': None},
        {'type': 'websocket.receive', 'text': None, 'bytes': b'\x01'},
        {'type': 'websocket.disconnect', 'code': 1000},
        {'type': 'websocket.disconnect', 'code': 1002, 'reason': 'protocol'},
        {'type': 'lifespan.startup'},
        {'type': 'lifespan.shutdown'},
    ]
    _ = events


async def construct_all_send(send: ASGISendCallable) -> None:
    """Every send-direction shape is accepted by an ``ASGISendCallable``.

    ``Headers`` is passed where a ``headers`` key appears, proving the
    sender's place-``Headers``-as-is idiom types without conversion.
    """
    hdrs = Headers([(b'content-type', b'text/plain')])

    await send({'type': 'http.response.start', 'status': 200})
    await send({'type': 'http.response.start', 'status': 200, 'headers': hdrs})
    await send({'type': 'http.response.start', 'status': 204,
                'headers': [(b'x-trace', b'1')], 'trailers': True})
    await send({'type': 'http.response.body'})
    await send({'type': 'http.response.body', 'body': b'hello', 'more_body': False})
    await send({'type': 'http.response.trailers', 'headers': hdrs,
                'more_trailers': False})
    await send({'type': 'http.response.push', 'path': '/style.css'})
    await send({'type': 'http.response.push', 'path': '/app.js', 'headers': hdrs})
    await send({'type': 'http.response.pathsend', 'path': '/srv/file.bin'})

    await send({'type': 'websocket.accept'})
    await send({'type': 'websocket.accept', 'subprotocol': None, 'headers': hdrs})
    await send({'type': 'websocket.send', 'text': 'hi'})
    await send({'type': 'websocket.send', 'bytes': b'\x01'})
    await send({'type': 'websocket.close'})
    await send({'type': 'websocket.close', 'code': 1001, 'reason': 'going away'})

    await send({'type': 'lifespan.startup.complete'})
    await send({'type': 'lifespan.startup.failed', 'message': 'boom'})
    await send({'type': 'lifespan.shutdown.complete'})
    await send({'type': 'lifespan.shutdown.failed', 'message': 'boom'})
