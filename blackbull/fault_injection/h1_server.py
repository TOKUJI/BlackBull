"""A deliberately misbehaving HTTP/1.1 server.

The HTTP/1.1 half of the fault-injection grid's *server* direction — the
cell the toolkit did not have.  Point your own HTTP/1.1 client at it and
assert that the client survives what a real server can do wrong::

    scenario = ScenarioH1Server(steps=[
        WaitForRequest(),
        SendRawBytes(b'HTTP/1.1 200 OK\\r\\nContent-Length: 100\\r\\n\\r\\nshort'),
        CloseGracefully(),
    ])
    async with H1FaultServer(scenario) as srv:
        with pytest.raises(...):
            await my_client.get(f'http://{srv.host}:{srv.port}/')

**It assembles every byte itself.**  Nothing here imports
``blackbull.server.sender`` or ``blackbull.server.response``, and that is
load-bearing rather than incidental: a fault server built on the
production send path cannot emit a fault the production send path has, so
the one bug class it is least able to find is the one in the code it
shares.  The HTTP/2 half made the same choice — it carries its own frame
encoder rather than calling ``FrameBase.save()``.  There is a test for it
(``tests/unit/test_fault_injection_h1_server.py::TestTheBreakerIsIndependent``)
because the property is invisible until the day it matters.

Two safety locks, the same pair :class:`H2FaultServer` carries and both
in scope for security reports per ``SECURITY.md``: it refuses to start in
a production context, and it refuses a non-loopback bind without an
explicit ``allow_remote=True``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time

from ._transport import half_close
from .scenario_h1_server import (
    Abort,
    CloseGracefully,
    EndChunkedBody,
    EndHeaders,
    ExpectRequest,
    HalfClose,
    ScenarioH1Server,
    ScenarioH1ServerResult,
    SendChunk,
    SendHeader,
    SendRawBytes,
    SendStatusLine,
    Sleep,
    WaitForRequest,
    encode_chunk,
    encode_chunked_terminator,
    encode_header,
    encode_status_line,
    request_matches,
)

logger = logging.getLogger(__name__)

#: End of an HTTP/1.1 message head (RFC 9112 §2.1).
_HEAD_END = b'\r\n\r\n'



class H1FaultServerError(RuntimeError):
    """Raised when the fault server refuses to run."""


def _refuse_in_production() -> None:
    """Hard opt-out: refuse to start in a production context.

    Same rule and same two signals as the HTTP/2 half —
    ``BLACKBULL_ENV=production`` via ``Settings.env``, plus ``BB_PRODUCTION``
    as an explicit override so the guard trips even when the Settings
    machinery is unavailable.
    """
    override = os.environ.get('BB_PRODUCTION', '').strip().lower() in (
        '1', 'true', 'yes', 'on')
    in_production = False
    try:
        from ..env import get_settings, Environment  # noqa: PLC0415
        in_production = get_settings().env == Environment.PRODUCTION
    except Exception:
        # Fall back to the explicit override rather than failing open.
        pass
    if override or in_production:
        raise H1FaultServerError(
            "H1FaultServer refuses to run in a production context "
            "(BLACKBULL_ENV=production or BB_PRODUCTION set).  This is a "
            "testing-only instrument that deliberately emits wrong HTTP/1.1 "
            "responses; running it in a production process would expose your "
            "service to the same misbehaviour.  Run it only in a test harness."
        )


class H1FaultServer:
    """Serves one :class:`ScenarioH1Server` to each connecting client."""

    def __init__(
        self,
        scenario: ScenarioH1Server,
        *,
        host: str = '127.0.0.1',
        port: int = 0,
        allow_remote: bool = False,
        ssl_context: ssl.SSLContext | None = None,
    ):
        _refuse_in_production()
        if not allow_remote and host not in ('127.0.0.1', '::1', 'localhost'):
            raise H1FaultServerError(
                f"H1FaultServer refuses to bind to {host!r} without "
                "allow_remote=True.  Deliberate-misbehaviour mode is "
                "intended for local-loop tests only."
            )
        self.scenario = scenario
        self.host = host
        self.port = port
        self._ssl_context = ssl_context
        self._server: asyncio.base_events.Server | None = None
        # In-flight connection handlers.  ``asyncio.start_server`` stops
        # *accepting* on close but leaves running handlers alone, and this
        # server's whole job is to hold connections open — a scenario ending
        # in ``Sleep(30)`` would make ``stop()`` wait out the sleep.  Tracked
        # so teardown can cancel them.
        self._handlers: set[asyncio.Task] = set()
        self.url: str | None = None
        self.last_result: ScenarioH1ServerResult | None = None
        self._connection_done = asyncio.Event()

    # ----- lifecycle ---------------------------------------------------

    async def __aenter__(self) -> 'H1FaultServer':
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port,
            ssl=self._ssl_context,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        scheme = 'https' if self._ssl_context is not None else 'http'
        self.url = f'{scheme}://{self.host}:{self.port}/'
        logger.debug('H1FaultServer bound at %s', self.url)

    async def stop(self) -> None:
        """Stop accepting and cancel anything still mid-scenario.

        The cancellation is the part that matters: a scenario whose last
        step is a long ``Sleep`` is the *normal* shape here — that is how
        "the server never answers" is expressed — so a teardown that waited
        for handlers would wait out the sleep on every such case.
        """
        for task in list(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
            self._handlers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        logger.debug('H1FaultServer shut down')

    async def wait_for_connection_done(self, timeout: float = 5.0) -> None:
        """Block until a client has connected and the scenario finished."""
        await asyncio.wait_for(self._connection_done.wait(), timeout=timeout)

    # ----- per-connection handler --------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        result = ScenarioH1ServerResult()
        t0 = time.monotonic()
        try:
            for step in self.scenario.steps:
                terminal = await self._run_step(step, reader, writer, result)
                result.steps_completed.append(type(step).__name__)
                if terminal:
                    result.terminated = True
                    return
            # Scenario exhausted without a terminator: close cleanly, so a
            # client waiting on a body it will never get sees EOF rather
            # than hanging on a server that has simply run out of steps.
            await self._close(writer, graceful=True)
        except (ConnectionResetError, BrokenPipeError) as exc:
            logger.debug('H1FaultServer: client vanished mid-scenario')
            result.exception = exc
        except asyncio.CancelledError:
            # Teardown cancelled us mid-scenario; record what happened and
            # let the cancellation continue.
            result.elapsed_s = time.monotonic() - t0
            self.last_result = result
            self._connection_done.set()
            raise
        finally:
            if task is not None:
                self._handlers.discard(task)
            result.elapsed_s = time.monotonic() - t0
            self.last_result = result
            self._connection_done.set()

    async def _run_step(self, step, reader, writer,
                        result: ScenarioH1ServerResult) -> bool:
        """Execute one step.  Returns True when it terminates the connection."""
        if isinstance(step, WaitForRequest):
            # Filter over a pipeline: read heads until one matches, and
            # count what was skipped.  A skipped head is a request the
            # scenario can no longer answer, and HTTP/1.1 responses are
            # positional — so this desyncs the connection, deliberately,
            # and ``wait_skipped`` is how the scenario author sees it.
            deadline = asyncio.get_running_loop().time() + step.timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    result.wait_timed_out = True
                    return False
                head = await self._read_head(reader, remaining, result)
                if head is None:
                    return False
                if request_matches(head, step.match):
                    result.request_head = head
                    return False
                result.wait_skipped += 1

        if isinstance(step, ExpectRequest):
            # A guard, not a filter: one head, nothing skipped, and the
            # verdict recorded either way.
            head = await self._read_head(reader, step.timeout, result)
            if head is None:
                result.expectations.append((dict(step.match), False))
                return False
            result.request_head = head
            result.expectations.append(
                (dict(step.match), request_matches(head, step.match)))
            return False

        # The typed steps all reduce to bytes, and share one write path so
        # the pacing option and the byte accounting cannot drift apart.
        if isinstance(step, SendStatusLine):
            await self._write(writer, encode_status_line(step), 0.0, result)
            return False

        if isinstance(step, SendHeader):
            await self._write(writer, encode_header(step), 0.0, result)
            return False

        if isinstance(step, EndHeaders):
            await self._write(writer, b'\r\n', 0.0, result)
            return False

        if isinstance(step, SendChunk):
            await self._write(writer, encode_chunk(step), 0.0, result)
            return False

        if isinstance(step, EndChunkedBody):
            await self._write(writer, encode_chunked_terminator(step), 0.0,
                              result)
            return False

        if isinstance(step, SendRawBytes):
            await self._write(writer, step.data, step.byte_interval, result)
            return False

        if isinstance(step, Sleep):
            await asyncio.sleep(step.duration)
            return False

        if isinstance(step, Abort):
            await self._close(writer, graceful=False)
            return True

        if isinstance(step, CloseGracefully):
            await self._close(writer, graceful=True)
            return True

        if isinstance(step, HalfClose):
            # FIN on the write side only, and **not** terminal — the
            # connection is still readable, which is the whole difference
            # from CloseGracefully.  Making it terminal would return from
            # the handler without closing anything, leaving a half-open
            # socket for teardown to reap; it would also disagree with the
            # client half, where a half-closed scenario is still reading.
            result.half_closed = half_close(writer)
            return False

        raise H1FaultServerError(f'unknown scenario step: {step!r}')

    @staticmethod
    async def _read_head(reader: asyncio.StreamReader, timeout: float,
                         result: ScenarioH1ServerResult) -> bytes | None:
        """Read one request head, or ``None`` when none arrives.

        A client that never sends a request is itself a case worth
        scripting around, so the miss is recorded rather than raised —
        the behaviour this step has always had.
        """
        try:
            head = await asyncio.wait_for(
                reader.readuntil(_HEAD_END), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            result.wait_timed_out = True
            return None
        result.client_bytes_received += len(head)
        return head

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, data: bytes,
                     byte_interval: float,
                     result: ScenarioH1ServerResult) -> None:
        """The one write path every step goes through.

        Pacing lives here rather than per-step because a trickle is not a
        slow write of the whole buffer: each byte has to reach the wire
        before the pause, or the peer sees one burst after the total delay
        and the scenario tests nothing.
        """
        if byte_interval > 0:
            for i in range(len(data)):
                writer.write(data[i:i + 1])
                await writer.drain()
                result.server_bytes_sent += 1
                await asyncio.sleep(byte_interval)
        else:
            writer.write(data)
            await writer.drain()
            result.server_bytes_sent += len(data)

    @staticmethod
    async def _close(writer: asyncio.StreamWriter, *, graceful: bool) -> None:
        """Close the transport, orderly or hard.

        The distinction is the observable one: ``graceful`` sends FIN so the
        client sees EOF, while an abort sends RST.  Clients do not always
        treat the two alike, which is why both are scriptable.
        """
        try:
            if graceful:
                writer.close()
                await writer.wait_closed()
            else:
                writer.transport.abort()
        except Exception:  # pragma: no cover - the peer may already be gone
            logger.debug('H1FaultServer: close raced the peer')
