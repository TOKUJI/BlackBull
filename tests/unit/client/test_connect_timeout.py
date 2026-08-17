"""Client connection-establishment deadlines.

Every client opened its transport with a bare ``await asyncio.open_connection()``.
A peer that completes the TCP handshake and then goes silent left the coroutine
pending with nothing to end it: TLS negotiation has no kernel-side deadline, so
``async with SomeClient(...)`` could hang for the lifetime of the process.

Two distinct waits reach the same symptom and both are covered here:

* the transport open in ``__aenter__`` (all clients), and
* the RFC 6455 handshake read in ``WebSocketClient.connect()`` -- a peer can
  accept the connection and simply never send the 101.

The stalled peer is a real loopback listener rather than a patched
``open_connection``: the defect lives inside that call, so faking it would
assert the fix against a mock instead of against a socket.
"""
from __future__ import annotations

import asyncio
import inspect
import ssl as _ssl

import pytest

from blackbull.client._connect import DEFAULT_CONNECT_TIMEOUT
from blackbull.client.client import Client
from blackbull.client.http1 import HTTP1Client
from blackbull.client.http2 import HTTP2Client
from blackbull.client.websocket import WebSocketClient
from blackbull.client.websocket_h2 import WebSocketH2Client

# Short enough to keep the suite fast, long enough that a loaded CI box does
# not mistake scheduling delay for the deadline under test.
_BUDGET = 0.25


class _StalledPeer:
    """Loopback listener that accepts a connection and then never speaks.

    With TLS requested by the client this stalls inside the handshake, which is
    the unbounded case: a plaintext connect completes as soon as the kernel
    accepts, so only a peer that owes the client bytes can hold it.
    """

    def __init__(self, *, drain: bool = False) -> None:
        self._drain = drain
        self._server: asyncio.AbstractServer | None = None
        self._holds: list[asyncio.StreamWriter] = []
        self.port = 0

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        self._holds.append(writer)
        if self._drain:
            await reader.read(4096)  # swallow the request, still answer nothing
        await asyncio.Event().wait()

    async def __aenter__(self) -> '_StalledPeer':
        self._server = await asyncio.start_server(self._handle, '127.0.0.1', 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        for w in self._holds:
            w.close()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


def _permissive_tls() -> _ssl.SSLContext:
    """Client context that would accept anything -- the peer never gets far
    enough to be validated, so verification is beside the point here."""
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    return ctx


async def _assert_deadline_fires(coro) -> None:
    """The context entry must end itself, without the test imposing a bound."""
    task = asyncio.create_task(coro)
    done, _ = await asyncio.wait({task}, timeout=_BUDGET * 20)
    if not done:
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001 -- cleanup of a hung task
            pass
        pytest.fail('connect stayed pending: no deadline fired')
    with pytest.raises(TimeoutError):
        await task


class TestTransportOpenDeadline:
    """``__aenter__`` bounds the transport open on every client."""

    @pytest.mark.asyncio
    async def test_websocket_client_stops_on_stalled_peer(self):
        async with _StalledPeer() as peer:
            await _assert_deadline_fires(
                WebSocketClient('127.0.0.1', peer.port, ssl=_permissive_tls(),
                                connect_timeout=_BUDGET).__aenter__())

    @pytest.mark.asyncio
    async def test_http1_client_stops_on_stalled_peer(self):
        async with _StalledPeer() as peer:
            await _assert_deadline_fires(
                HTTP1Client('127.0.0.1', peer.port, ssl=_permissive_tls(),
                            connect_timeout=_BUDGET).__aenter__())

    @pytest.mark.asyncio
    async def test_http2_client_stops_on_stalled_peer(self):
        async with _StalledPeer() as peer:
            await _assert_deadline_fires(
                HTTP2Client('127.0.0.1', peer.port, ssl=_permissive_tls(),
                            connect_timeout=_BUDGET).__aenter__())

    @pytest.mark.asyncio
    async def test_alpn_client_stops_on_stalled_peer(self):
        async with _StalledPeer() as peer:
            await _assert_deadline_fires(
                Client('127.0.0.1', peer.port, ssl=_permissive_tls(),
                       connect_timeout=_BUDGET).__aenter__())

    @pytest.mark.asyncio
    async def test_websocket_h2_client_stops_on_stalled_peer(self):
        async with _StalledPeer() as peer:
            await _assert_deadline_fires(
                WebSocketH2Client('127.0.0.1', peer.port,
                                  ssl=_permissive_tls(),
                                  connect_timeout=_BUDGET).__aenter__())


class TestHandshakeDeadline:
    """A peer may accept the connection and still never complete the upgrade."""

    @pytest.mark.asyncio
    async def test_websocket_connect_stops_when_101_never_arrives(self):
        async with _StalledPeer(drain=True) as peer:
            async with WebSocketClient('127.0.0.1', peer.port) as c:
                # Plaintext: the transport open succeeds here by design; the
                # wait under test is the response read, one layer up.
                await _assert_deadline_fires(
                    c.connect('/ws', response_timeout=_BUDGET))


class TestDefaults:
    """The reported defect is about the *unconfigured* client, so the published
    defaults are part of the contract -- an opt-in knob would leave every
    caller who never passes it exactly as exposed as before."""

    def test_default_connect_timeout_is_finite(self):
        assert DEFAULT_CONNECT_TIMEOUT > 0
        assert DEFAULT_CONNECT_TIMEOUT != float('inf')

    @pytest.mark.parametrize('cls', [
        WebSocketClient, HTTP1Client, HTTP2Client, Client, WebSocketH2Client,
    ])
    def test_client_defaults_to_a_bounded_connect(self, cls):
        default = inspect.signature(cls).parameters['connect_timeout'].default
        assert default == DEFAULT_CONNECT_TIMEOUT

    def test_websocket_handshake_defaults_to_a_bounded_read(self):
        default = inspect.signature(
            WebSocketClient.connect).parameters['response_timeout'].default
        assert default is not None and default > 0

    @pytest.mark.asyncio
    async def test_connect_timeout_none_restores_the_unbounded_wait(self):
        """Escape hatch for callers who want to impose their own deadline."""
        async with _StalledPeer() as peer:
            task = asyncio.create_task(
                HTTP1Client('127.0.0.1', peer.port, ssl=_permissive_tls(),
                            connect_timeout=None).__aenter__())
            done, _ = await asyncio.wait({task}, timeout=_BUDGET * 2)
            assert not done, 'connect_timeout=None should not impose a deadline'
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 -- cleanup of a hung task
                pass
