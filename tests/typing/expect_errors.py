"""Negative static proof: malformed message-channel traffic is a type error.

The companion of `assert_narrowing.py`.  That file proves the good cases pass;
this one proves the bad cases *fail* — a gate that only checks the positive
direction would still pass if the unions were secretly `Any`.

Every line tagged ``# EXPECT-ERROR`` must produce at least one pyright
diagnostic.  `tests/architecture/test_typing_gate.py` asserts exactly that,
line by line, so a diagnostic drifting to a neighbouring line fails the gate
rather than passing silently.

This file is **never executed**; several lines here would raise at runtime,
which is the point.
"""
from typing import Any, cast

from blackbull.asgi import ASGIReceiveEvent, ASGISendCallable, ASGISendEvent
from blackbull.response import Response


# --- The 0.43.2 type-confusion bug, caught statically ----------------------
# A `Response` object leaked into a middleware `send` wrapper, which subscripts
# `msg['type']` → `TypeError: 'Response' object is not subscriptable`.
# `tests/unit/test_middleware_decorator.py` catches this at runtime through the
# full app stack; here the same mistake is rejected before the code runs.

async def response_object_is_not_a_send_event(send: ASGISendCallable) -> None:
    await send(Response('<h1>Hello</h1>'))  # EXPECT-ERROR


async def response_object_is_not_subscriptable(event: ASGISendEvent) -> None:
    resp = Response(b'data')
    _ = resp['type']  # EXPECT-ERROR
    _ = event


# --- Misspelled / unknown keys --------------------------------------------
# TypedDict closed-ness is what makes the ~100 construction sites checkable.

async def misspelled_notrequired_key(send: ASGISendCallable) -> None:
    await send({'type': 'http.response.body', 'body': b'x', 'more_bodies': True})  # EXPECT-ERROR


async def unknown_key_on_start(send: ASGISendCallable) -> None:
    await send({'type': 'http.response.start', 'status': 200, 'statuss': 200})  # EXPECT-ERROR


# --- Wrong value types -----------------------------------------------------

async def status_must_be_int(send: ASGISendCallable) -> None:
    await send({'type': 'http.response.start', 'status': '200'})  # EXPECT-ERROR


async def body_must_be_bytes(send: ASGISendCallable) -> None:
    await send({'type': 'http.response.body', 'body': 'not bytes'})  # EXPECT-ERROR


# --- Missing required keys -------------------------------------------------

async def start_requires_status(send: ASGISendCallable) -> None:
    await send({'type': 'http.response.start'})  # EXPECT-ERROR


async def pathsend_requires_path(send: ASGISendCallable) -> None:
    await send({'type': 'http.response.pathsend'})  # EXPECT-ERROR


# --- Direction confusion ---------------------------------------------------
# The two unions are disjoint; sending a receive-direction event (or an event
# string that exists but belongs to the other direction) is rejected.

async def receive_event_is_not_sendable(send: ASGISendCallable) -> None:
    await send({'type': 'http.request', 'body': b'x'})  # EXPECT-ERROR


async def send_event_is_not_receivable() -> None:
    event: ASGIReceiveEvent = {'type': 'http.response.start', 'status': 200}  # EXPECT-ERROR
    _ = event


# --- Unknown event type ----------------------------------------------------

async def unknown_event_type(send: ASGISendCallable) -> None:
    await send({'type': 'http.response.teapot'})  # EXPECT-ERROR


# --- The escape hatch still exists -----------------------------------------
# `cast` is deliberately *not* an error: the compression seam threads
# `ResponseStart`/`ResponseBody` wrappers (dict subclasses, not statically
# union members) through typed sends this way.  If this line ever started
# erroring, Phase 2's annotation sweep would have no legal escape.

async def cast_is_the_sanctioned_escape(send: ASGISendCallable) -> None:
    wrapper: Any = {'type': 'http.response.start', 'status': 200}
    await send(cast(ASGISendEvent, wrapper))
