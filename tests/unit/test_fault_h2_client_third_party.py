"""The HTTP/2 broken client, against a server that is not ours.

Three of the grid's four cells were cross-checked against a third-party
implementation from the start: cell A drives the standard library's
`http.server`, and cells B and C drive `httpx`.  Cell D — the one this
sprint added — drove only BlackBull's own server, which leaves the obvious
question unanswered: **do these scenarios exercise HTTP/2, or do they
exercise BlackBull?**

The counterpart here is a minimal server built on `h2` (hyper-h2), the
same implementation `httpx[http2]` uses on the client side — so cells C
and D are checked against the same third party from opposite directions.

`h2` is a transitive dependency of the `fault-injection` extra, imported
nowhere in `blackbull/`.  BlackBull's protocol-ownership rule forbids
*implementing* HTTP/2 on a third-party library; using one as the thing
being tested against is the opposite of that, and is what cells B and C
have always done.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest

from blackbull.client.http2 import HTTP2Client
from blackbull.fault_injection.catalogue.h2_client import CATALOGUE

pytestmark = pytest.mark.asyncio

h2_config = pytest.importorskip('h2.config')
h2_connection = pytest.importorskip('h2.connection')
h2_events = pytest.importorskip('h2.events')


class _H2ReferenceServer:
    """A correct, minimal HTTP/2 server over h2c — the reference peer.

    It answers every request 200 and otherwise does exactly what `h2` tells
    it to.  Anything it rejects is `h2`'s judgement, not ours, which is the
    entire point: a scenario that only BlackBull objects to would pass here
    and expose the asymmetry.
    """

    def __init__(self, host: str = '127.0.0.1') -> None:
        self.host = host
        self.port = 0
        self._server: asyncio.base_events.Server | None = None
        self._conns: set[asyncio.Task] = set()
        #: Exceptions h2 raised while parsing what the client sent — the
        #: reference implementation's verdict on the scenario.
        self.protocol_errors: list[str] = []
        #: Set when a connection handler has finished.  Without waiting on
        #: it, a test reads ``protocol_errors`` before the server has seen
        #: the bytes: the client finishes its scenario, closes, and the
        #: verdict lands after the assertion.  The first run of this file
        #: reported "accepted" for every case for exactly that reason.
        self.connection_done = asyncio.Event()

    async def __aenter__(self) -> '_H2ReferenceServer':
        self._server = await asyncio.start_server(
            self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        for task in list(self._conns):
            task.cancel()
        if self._conns:
            await asyncio.gather(*self._conns, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._conns.add(task)
        conn = h2_connection.H2Connection(
            config=h2_config.H2Configuration(client_side=False))
        try:
            conn.initiate_connection()
            writer.write(conn.data_to_send())
            await writer.drain()
            while True:
                data = await reader.read(65535)
                if not data:
                    return
                try:
                    events = conn.receive_data(data)
                except Exception as exc:
                    # h2 refused what the client sent.  Recorded, and the
                    # connection ends the way h2 wants it to.
                    self.protocol_errors.append(f'{type(exc).__name__}: {exc}')
                    with_close = conn.data_to_send()
                    if with_close:
                        writer.write(with_close)
                        await writer.drain()
                    return
                for event in events:
                    if isinstance(event, h2_events.RequestReceived):
                        conn.send_headers(
                            event.stream_id,
                            [(':status', '200'), ('content-length', '2')])
                        conn.send_data(event.stream_id, b'ok',
                                       end_stream=True)
                out = conn.data_to_send()
                if out:
                    writer.write(out)
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            if task is not None:
                self._conns.discard(task)
            self.connection_done.set()
            with contextlib.suppress(Exception):
                writer.close()


#: Cases that end on their own.  Three catalogue entries close with a long
#: `Sleep` because *holding* the connection is the fault; what ends those is
#: the server's own deadline, and `h2` has none — it is a protocol state
#: machine, not a server.
_SELF_TERMINATING = (
    'rapid_reset_burst', 'ping_flood', 'settings_flood',
    'unknown_frame_type', 'settings_ack_with_payload',
    'abort_mid_header_block', 'preface_trickled',
)


class TestAgainstAThirdPartyServer:
    @pytest.mark.parametrize('case_name', _SELF_TERMINATING)
    async def test_the_scenario_reaches_a_non_blackbull_server(self, case_name):
        """The scenario must be HTTP/2, not BlackBull-shaped HTTP/2.

        The assertion is deliberately about the *executor*, not about how
        h2 reacts: what each catalogue case should provoke is the server
        author's question, and differs between implementations.  What must
        hold everywhere is that the scenario was delivered and folded into
        a result rather than raising.
        """
        async with _H2ReferenceServer() as server:
            async with HTTP2Client('127.0.0.1', server.port,
                                   scenario_mode=True) as client:
                result = await asyncio.wait_for(
                    client.execute_scenario(CATALOGUE[case_name]()),
                    timeout=10.0)

        assert result.steps_completed > 0, (
            f'{case_name} delivered no steps to a reference server — the '
            f'scenario may depend on something only BlackBull accepts')
        assert result.elapsed_s >= 0

    @pytest.mark.parametrize('case_name,expect_rejected', [
        # h2's own verdicts, recorded so a change in either side shows up.
        # These are *h2's* judgements, not assertions about what HTTP/2
        # requires — where the two differ, that difference is the finding.
        ('settings_ack_with_payload', True),   # §6.5 — FRAME_SIZE_ERROR
        ('unknown_frame_type', False),         # §4.1 — MUST be ignored
        ('ping_flood', False),                 # legal, just expensive
    ])
    async def test_the_reference_server_reacts_as_expected(
            self, case_name, expect_rejected):
        """A third party's verdict, waited for rather than raced.

        The first version of this file read ``protocol_errors`` straight
        after the client finished and reported "accepted" for every case —
        the client had closed before the server parsed the bytes.  Every
        row looked like a pass while measuring nothing.
        """
        async with _H2ReferenceServer() as server:
            async with HTTP2Client('127.0.0.1', server.port,
                                   scenario_mode=True) as client:
                await asyncio.wait_for(
                    client.execute_scenario(CATALOGUE[case_name]()),
                    timeout=10.0)
            await asyncio.wait_for(server.connection_done.wait(), timeout=5.0)
            rejected = bool(server.protocol_errors)

        assert rejected is expect_rejected, (
            f'{case_name}: h2 {"accepted" if not rejected else "rejected"} it '
            f'({server.protocol_errors}) — expected the opposite')

    async def test_a_well_formed_exchange_succeeds_against_it(self):
        """The control: the reference server answers a correct request.

        Without this, every row above could pass because the server is
        broken rather than because the scenario is portable.
        """
        async with _H2ReferenceServer() as server:
            async with HTTP2Client('127.0.0.1', server.port) as client:
                response = await asyncio.wait_for(
                    client.request('GET', '/'), timeout=5.0)

        assert response.status == 200
        assert response.body == b'ok'
        assert server.protocol_errors == [], (
            f'the reference server rejected a well-formed exchange: '
            f'{server.protocol_errors}')
