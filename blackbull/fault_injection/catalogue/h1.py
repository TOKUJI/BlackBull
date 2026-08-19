"""Named HTTP/1.1 server-side misbehaviour cases.

The HTTP/1.1 twin of :mod:`blackbull.fault_injection.catalogue.h2`: each
entry is one thing a real server does wrong, named so a suite can
``parametrize`` over the set and report which case broke a client.

Every case is raw bytes.  The server assembles its own output rather than
going through the production send path — see
:mod:`blackbull.fault_injection.h1_server` for why that is load-bearing.
"""
from __future__ import annotations

from ..scenario_h1_server import (
    Abort, CloseGracefully, ScenarioH1Server, SendRawBytes, Sleep, WaitForRequest,
)

_OK_HEAD = b'HTTP/1.1 200 OK\r\n'


def content_length_overstated() -> ScenarioH1Server:
    """Declares 100 bytes, sends 5, then closes.

    The client must not present the short body as a complete response.
    """
    return ScenarioH1Server(name='content_length_overstated', steps=(
        WaitForRequest(),
        SendRawBytes(_OK_HEAD + b'Content-Length: 100\r\n\r\nshort'),
        CloseGracefully(),
    ))


def content_length_understated() -> ScenarioH1Server:
    """Declares 2 bytes and sends 40.

    The surplus is what a keep-alive client would parse as the *next*
    response's status line — the response-side twin of request smuggling.
    """
    return ScenarioH1Server(name='content_length_understated', steps=(
        WaitForRequest(),
        SendRawBytes(_OK_HEAD + b'Content-Length: 2\r\n\r\n'
                     + b'hi' + b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n'),
        CloseGracefully(),
    ))


def conflicting_content_length() -> ScenarioH1Server:
    """Two ``Content-Length`` headers that disagree (RFC 9110 §8.6)."""
    return ScenarioH1Server(name='conflicting_content_length', steps=(
        WaitForRequest(),
        SendRawBytes(_OK_HEAD + b'Content-Length: 5\r\nContent-Length: 10\r\n'
                     b'\r\nHELLOSURPLUS'),
        CloseGracefully(),
    ))


def chunked_stops_mid_chunk() -> ScenarioH1Server:
    """Announces a 5-byte chunk, sends 2, then EOF."""
    return ScenarioH1Server(name='chunked_stops_mid_chunk', steps=(
        WaitForRequest(),
        SendRawBytes(_OK_HEAD + b'Transfer-Encoding: chunked\r\n\r\n5\r\nab'),
        CloseGracefully(),
    ))


def chunked_never_terminates() -> ScenarioH1Server:
    """Well-formed chunks that never reach the zero-length terminator."""
    return ScenarioH1Server(name='chunked_never_terminates', steps=(
        WaitForRequest(),
        SendRawBytes(_OK_HEAD + b'Transfer-Encoding: chunked\r\n\r\n'
                     b'2\r\nab\r\n2\r\ncd\r\n'),
        Sleep(30.0),
    ))


def trickled_status_line() -> ScenarioH1Server:
    """A complete, correct response delivered one byte at a time.

    Nothing here is malformed — the *pacing* is the fault, which is why it
    cannot be expressed as anything but raw bytes plus an interval.
    """
    return ScenarioH1Server(name='trickled_status_line', steps=(
        WaitForRequest(),
        SendRawBytes(_OK_HEAD + b'Content-Length: 2\r\n\r\nhi',
                     byte_interval=0.01),
    ))


def headers_never_end() -> ScenarioH1Server:
    """Header lines forever, never the blank line that ends the head."""
    return ScenarioH1Server(name='headers_never_end', steps=(
        WaitForRequest(),
        SendRawBytes(_OK_HEAD + b'X-Pad: 1\r\n' * 50),
        Sleep(30.0),
    ))


def closed_without_response() -> ScenarioH1Server:
    """Accepts the request, answers nothing, resets the connection."""
    return ScenarioH1Server(name='closed_without_response', steps=(
        WaitForRequest(),
        Abort(),
    ))


def silent_after_request() -> ScenarioH1Server:
    """Holds the connection open and never writes.

    What a client's own response deadline is measured against; the server
    contributes nothing but patience.
    """
    return ScenarioH1Server(name='silent_after_request', steps=(
        WaitForRequest(),
        Sleep(30.0),
    ))


#: Every case, for ``parametrize``.
CATALOGUE = {
    fn.__name__: fn for fn in (
        content_length_overstated,
        content_length_understated,
        conflicting_content_length,
        chunked_stops_mid_chunk,
        chunked_never_terminates,
        trickled_status_line,
        headers_never_end,
        closed_without_response,
        silent_after_request,
    )
}
