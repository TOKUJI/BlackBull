"""Programmable HTTP/2 server that emits deliberate misbehaviour.

:class:`H2FaultServer` listens on ``127.0.0.1:<random>``, accepts one
HTTP/2 (h2c plaintext) connection at a time, and walks a
:class:`~blackbull.fault_injection.scenario_h2.ScenarioH2` against it.
It is designed for client-library / proxy / security-research test
suites that need to exercise a client against a server that
**deliberately** does the wrong thing — half-closed streams, exhausted
flow-control windows, illegal SETTINGS, weird frame sequences.

The server is an opt-in testing instrument.  It refuses to start in a
production context — when the framework's ``BLACKBULL_ENV=production``
signal (or the explicit ``BB_PRODUCTION`` override) is set — so a
deliberate-misbehaviour code path cannot accidentally fire on a production
deployment.  This is the isolated, hard-opt-in design called out in the
project's out-of-scope list as the only acceptable form for fault-injection
machinery inside a correctness-focused framework.

Tutorial: ``docs/guide/fault_injection.md``.

Quick start
-----------

.. code-block:: python

    import pytest
    from blackbull.fault_injection import H2FaultServer
    from blackbull.fault_injection.catalogue import half_closed_stream_no_data

    @pytest.fixture
    async def fault_server():
        async with H2FaultServer(scenario=half_closed_stream_no_data()) as srv:
            yield srv

    async def test_client_times_out_on_stalled_stream(fault_server):
        client = MyH2Client(fault_server.url)
        with pytest.raises(TimeoutError):
            await client.get('/', timeout=1.0)
"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (
    FrameTypes,
    SettingFrameFlags,
)

from .scenario_h2 import (
    Abort,
    CloseGracefully,
    ScenarioH2,
    ScenarioH2Result,
    SendFrame,
    SendRawBytes,
    Sleep,
    WaitForClientFrame,
    frame_matches,
)

logger = logging.getLogger(__name__)


# RFC 9113 §3.4 — client connection preface bytes.
CLIENT_PREFACE = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'


class H2FaultServerError(RuntimeError):
    """Raised when ``H2FaultServer`` cannot start.

    The most common cause is running in a production context: the
    deliberate-misbehaviour machinery refuses to start when the framework's
    production signal (``BLACKBULL_ENV=production``) — or the explicit
    ``BB_PRODUCTION`` override — is set, regardless of how it was reached.
    """


def _refuse_in_production() -> None:
    """Hard opt-out: refuse to start in a production context.

    The framework's canonical production signal is
    ``BLACKBULL_ENV=production`` (surfaced as ``Settings.env``); the previous
    check keyed on ``BB_PRODUCTION`` alone — a var read nowhere else in the
    codebase — so a standard production process did **not** trip the guard
    (bug 1.22a).  ``BB_PRODUCTION`` is still honoured as an explicit override
    so the guard also trips outside the Settings machinery.
    """
    override = os.environ.get('BB_PRODUCTION', '').strip().lower() in (
        '1', 'true', 'yes', 'on')
    in_production = False
    try:
        from ..env import get_settings, Environment  # noqa: PLC0415
        in_production = get_settings().env == Environment.PRODUCTION
    except Exception:
        # If Settings can't be resolved for any reason, fall back to the
        # explicit override alone rather than failing open silently.
        pass
    if override or in_production:
        raise H2FaultServerError(
            "H2FaultServer refuses to run in a production context "
            "(BLACKBULL_ENV=production or BB_PRODUCTION set).  This is a "
            "testing-only instrument that deliberately emits wrong HTTP/2 "
            "responses; running it in a production process would expose your "
            "service to the same misbehaviour.  Run it only in a test harness."
        )


# ---------------------------------------------------------------------------
# Frame serialisation — minimal wire encoder for the catalogue's needs
# ---------------------------------------------------------------------------


def _encode_frame_header(length: int, type_byte: bytes, flags: int,
                         stream_id: int) -> bytes:
    """RFC 9113 §4.1 — 9-byte fixed header (24-bit length, type, flags, R+stream_id)."""
    return (
        length.to_bytes(3, 'big')
        + type_byte
        + flags.to_bytes(1, 'big')
        + (stream_id & 0x7fffffff).to_bytes(4, 'big')
    )


def serialize_frame(frame) -> bytes:
    """Convert a :class:`FrameBase` instance to wire bytes.

    Restricted to the frame types this server emits: SETTINGS,
    WINDOW_UPDATE, RST_STREAM, GOAWAY, PING, DATA.  Anything else
    must go through :class:`SendRawBytes` instead.
    """
    name = type(frame).__name__
    flags = int(getattr(frame, 'flags', 0) or 0)
    stream_id = int(getattr(frame, 'stream_id', 0) or 0)

    if name == 'SettingFrame':
        if flags & int(SettingFrameFlags.ACK):
            payload = b''
        else:
            payload = b''.join(
                int(setting_id).to_bytes(2, 'big')
                + int(value).to_bytes(4, 'big')
                for setting_id, value in getattr(frame, 'settings', [])
            )
        return _encode_frame_header(
            len(payload), FrameTypes.SETTINGS.value, flags, 0) + payload

    if name == 'WindowUpdate':
        inc = int(getattr(frame, 'window_size_increment', 0))
        payload = inc.to_bytes(4, 'big')
        return _encode_frame_header(
            len(payload), FrameTypes.WINDOW_UPDATE.value, flags, stream_id
        ) + payload

    if name == 'RstStream':
        payload = int(getattr(frame, 'error_code', 0)).to_bytes(4, 'big')
        return _encode_frame_header(
            len(payload), FrameTypes.RST_STREAM.value, flags, stream_id
        ) + payload

    if name == 'GoAway':
        last_stream = int(getattr(frame, 'last_stream_id', 0))
        err_code = int(getattr(frame, 'error_code', 0))
        payload = last_stream.to_bytes(4, 'big') + err_code.to_bytes(4, 'big')
        return _encode_frame_header(
            len(payload), FrameTypes.GOAWAY.value, flags, 0) + payload

    if name == 'Ping':
        payload = bytes(getattr(frame, 'payload', b'') or b'')
        payload = (payload + b'\x00' * 8)[:8]  # PING is always 8 bytes
        return _encode_frame_header(
            8, FrameTypes.PING.value, flags, 0) + payload

    if name == 'Data':
        payload = bytes(getattr(frame, 'data', b'') or b'')
        return _encode_frame_header(
            len(payload), FrameTypes.DATA.value, flags, stream_id
        ) + payload

    raise TypeError(
        f'{name} cannot be serialised by H2FaultServer; use SendRawBytes.'
    )


# ---------------------------------------------------------------------------
# H2FaultServer
# ---------------------------------------------------------------------------


class H2FaultServer:
    """Programmable HTTP/2 server emitting a :class:`ScenarioH2`.

    Async context manager: enter to bind + start accepting, exit to
    shut down.  ``self.url`` is set after enter and gives the URL a
    real h2c client can dial.

    Parameters
    ----------
    scenario:
        The :class:`ScenarioH2` the executor walks for each accepted
        connection.
    host:
        Bind host.  Defaults to ``'127.0.0.1'`` — binding to a
        non-localhost interface is rejected unless ``allow_remote``
        is set (the misbehaviour mode is for local tests only).
    port:
        Bind port.  Defaults to ``0`` (kernel-assigned random port).
    allow_remote:
        Bypass the localhost-only safety check.  Off by default.
    ssl_context:
        Optional :class:`ssl.SSLContext`.  When provided, the server
        terminates TLS and ``self.url`` is ``https://...``; ALPN must
        offer ``h2`` so clients negotiating H/2 over TLS (httpx, curl
        ``--http2``, …) connect cleanly.  When ``None`` (default), the
        server speaks plaintext h2c — usable with prior-knowledge
        clients only.

    Attributes
    ----------
    url:
        ``http://<host>:<port>/`` after start, or ``https://...`` when
        a TLS context is provided.
    last_result:
        :class:`ScenarioH2Result` from the most recently completed
        connection.  ``None`` before any client has connected.
    """

    def __init__(
        self,
        scenario: ScenarioH2,
        *,
        host: str = '127.0.0.1',
        port: int = 0,
        allow_remote: bool = False,
        ssl_context: ssl.SSLContext | None = None,
    ):
        _refuse_in_production()
        if not allow_remote and host not in ('127.0.0.1', '::1', 'localhost'):
            raise H2FaultServerError(
                f"H2FaultServer refuses to bind to {host!r} without "
                "allow_remote=True.  Deliberate-misbehaviour mode is "
                "intended for local-loop tests only."
            )
        self.scenario = scenario
        self._host = host
        self._port = port
        self._ssl_context = ssl_context
        self._server: asyncio.base_events.Server | None = None
        self._factory = FrameFactory()
        self.url: str | None = None
        self.last_result: ScenarioH2Result | None = None
        self._connection_done = asyncio.Event()

    # ----- lifecycle ---------------------------------------------------

    async def __aenter__(self) -> 'H2FaultServer':
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, self._host, self._port,
            ssl=self._ssl_context,
        )
        sock = self._server.sockets[0]
        port = sock.getsockname()[1]
        scheme = 'https' if self._ssl_context is not None else 'http'
        self.url = f'{scheme}://{self._host}:{port}/'
        logger.debug('H2FaultServer bound at %s', self.url)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        logger.debug('H2FaultServer shut down')

    async def wait_for_connection_done(self, timeout: float = 5.0) -> None:
        """Block until a client has connected and the scenario finished."""
        await asyncio.wait_for(self._connection_done.wait(), timeout=timeout)

    # ----- per-connection handler --------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        result = ScenarioH2Result()
        t0 = time.monotonic()
        frame_queue: asyncio.Queue = asyncio.Queue()
        reader_task: asyncio.Task | None = None
        try:
            # Wait for the client preface (24 magic bytes).  We tolerate
            # the client sending its preface before / after our SETTINGS.
            await self._consume_client_preface(reader, result)

            # Optional handshake — server's initial SETTINGS frame.
            if self.scenario.send_preface:
                await self._send_initial_settings(writer, result)

            # Spin a background task that reads frames from the wire
            # and pushes them into frame_queue.  The main scenario
            # loop consumes from this queue when it hits a Wait step.
            reader_task = asyncio.create_task(
                self._inbound_frame_loop(reader, frame_queue, result))

            # Walk the steps.
            for step in self.scenario.steps:
                terminate = await self._run_step(
                    step, writer, frame_queue, result)
                result.steps_completed += 1
                if terminate:
                    result.terminated = True
                    break
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass  # peer disconnect or our own cancellation — the scenario simply ends.
        except Exception as exc:  # noqa: BLE001
            result.exception = repr(exc)
            logger.warning('scenario step raised: %r', exc)
        finally:
            if reader_task is not None and not reader_task.done():
                reader_task.cancel()
                try:
                    await reader_task
                except (asyncio.CancelledError, Exception):
                    pass  # teardown: reader task was just cancelled; ignore its unwind.
            result.elapsed_s = time.monotonic() - t0
            self.last_result = result
            self._connection_done.set()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _consume_client_preface(
        self,
        reader: asyncio.StreamReader,
        result: ScenarioH2Result,
    ) -> None:
        """Read and verify the 24-byte client connection preface."""
        preface = await reader.readexactly(len(CLIENT_PREFACE))
        result.client_bytes_received += len(preface)
        if preface != CLIENT_PREFACE:
            raise H2FaultServerError(
                f"client preface mismatch; got {preface!r}")

    async def _send_initial_settings(
        self,
        writer: asyncio.StreamWriter,
        result: ScenarioH2Result,
    ) -> None:
        """Send the server-initiated SETTINGS frame.

        The scenario's ``initial_settings`` parameter pairs feed the
        payload — empty tuple means an empty SETTINGS frame (still
        legal; signals "I accept defaults").
        """
        payload = b''.join(
            int(setting_id).to_bytes(2, 'big') + int(value).to_bytes(4, 'big')
            for setting_id, value in self.scenario.initial_settings
        )
        frame = _encode_frame_header(
            len(payload), FrameTypes.SETTINGS.value, 0, 0) + payload
        writer.write(frame)
        await writer.drain()
        result.server_bytes_sent += len(frame)

    async def _inbound_frame_loop(
        self,
        reader: asyncio.StreamReader,
        queue: asyncio.Queue,
        result: ScenarioH2Result,
    ) -> None:
        """Decode inbound frames and push them onto *queue*."""
        try:
            while True:
                header = await reader.readexactly(9)
                result.client_bytes_received += 9
                length = int.from_bytes(header[:3], 'big')
                payload = await reader.readexactly(length) if length else b''
                result.client_bytes_received += length
                try:
                    frame = self._factory.load(header + payload)
                except Exception as exc:  # noqa: BLE001
                    logger.debug('inbound frame decode failed: %r', exc)
                    continue
                await queue.put(frame)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass

    async def _run_step(
        self,
        step,
        writer: asyncio.StreamWriter,
        queue: asyncio.Queue,
        result: ScenarioH2Result,
    ) -> bool:
        """Execute one scenario step.  Returns True if the executor
        should stop walking the scenario after this step (Abort /
        CloseGracefully terminators)."""
        if isinstance(step, SendFrame):
            data = serialize_frame(step.frame)
            writer.write(data)
            await writer.drain()
            result.server_bytes_sent += len(data)
            return False

        if isinstance(step, SendRawBytes):
            if step.byte_interval > 0:
                for byte in step.data:
                    writer.write(bytes([byte]))
                    await writer.drain()
                    result.server_bytes_sent += 1
                    await asyncio.sleep(step.byte_interval)
            else:
                writer.write(step.data)
                await writer.drain()
                result.server_bytes_sent += len(step.data)
            return False

        if isinstance(step, WaitForClientFrame):
            deadline = time.monotonic() + step.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result.wait_timed_out = True
                    return False
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    result.wait_timed_out = True
                    return False
                if frame_matches(frame, step.match):
                    return False
                # else: frame didn't match — drop it and keep waiting

        if isinstance(step, Sleep):
            await asyncio.sleep(step.duration)
            return False

        if isinstance(step, Abort):
            transport = writer.transport
            try:
                transport.abort()
            except Exception:  # noqa: BLE001
                writer.close()
            return True

        if isinstance(step, CloseGracefully):
            payload = (int(step.last_stream_id).to_bytes(4, 'big')
                       + int(step.error_code).to_bytes(4, 'big'))
            goaway = _encode_frame_header(
                len(payload), FrameTypes.GOAWAY.value, 0, 0) + payload
            writer.write(goaway)
            await writer.drain()
            result.server_bytes_sent += len(goaway)
            return True

        raise TypeError(f'unknown step: {type(step).__name__}')


__all__ = [
    'CLIENT_PREFACE',
    'H2FaultServer',
    'H2FaultServerError',
    'serialize_frame',
]
