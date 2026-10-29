"""Programmable malformed HTTP/2 server.

Production mode is refused; non-loopback binds require allow_remote=True.
Use raw-byte steps when normal frame encoding cannot express a fault.
"""
from __future__ import annotations

import asyncio
import logging

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameTypes

from ._scenario import _FaultServer, _ServerConnection
from .scenario_h2 import ScenarioH2Result, frame_bytes, frame_matches, serialize_frame
from .scenario_h2_client import CLIENT_PREFACE

logger = logging.getLogger(__name__)


class H2FaultServerError(RuntimeError):
    """Raised when ``H2FaultServer`` cannot start.

    The most common cause is running in a production context: the
    deliberate-misbehaviour machinery refuses to start when the framework's
    production signal (``BLACKBULL_ENV=production``) — or the explicit
    ``BB_PRODUCTION`` override — is set, regardless of how it was reached.
    """


class _H2Connection(_ServerConnection):
    _matches = staticmethod(frame_matches)

    def __init__(self, server: 'H2FaultServer', *args) -> None:
        super().__init__(*args)
        self.scenario = server.scenario
        self.frames: asyncio.Queue = asyncio.Queue()
        self.inbound: asyncio.Task | None = None

    async def open(self) -> None:
        # The client may send its preface before or after our SETTINGS.
        preface = await self.reader.readexactly(len(CLIENT_PREFACE))
        self.result.client_bytes_received += len(preface)
        if preface != CLIENT_PREFACE:
            raise H2FaultServerError(f"client preface mismatch; got {preface!r}")
        if self.scenario.send_preface:
            payload = b''.join(
                int(setting_id).to_bytes(2, 'big') + int(value).to_bytes(4, 'big')
                for setting_id, value in self.scenario.initial_settings)
            await self._write(frame_bytes(len(payload), FrameTypes.SETTINGS.value,
                                          0, 0, payload), 0.0)
        self.inbound = asyncio.create_task(self._inbound_frames())

    def release(self) -> None:
        if self.inbound is not None:
            self.inbound.cancel()

    def failed(self, exc: Exception) -> None:
        super().failed(exc)
        logger.warning('scenario step raised: %r', exc)

    async def _inbound_frames(self) -> None:
        """Decode inbound frames onto the queue the read steps consume."""
        factory = FrameFactory()
        try:
            while True:
                header = await self.reader.readexactly(9)
                self.result.client_bytes_received += 9
                length = int.from_bytes(header[:3], 'big')
                payload = await self.reader.readexactly(length) if length else b''
                self.result.client_bytes_received += length
                try:
                    frame = factory.load(header + payload)
                except Exception as exc:  # noqa: BLE001
                    logger.debug('inbound frame decode failed: %r', exc)
                    continue
                await self.frames.put(frame)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            await self.frames.put(None)

    async def _close(self, *, graceful: bool) -> None:
        # Not awaited: a TLS peer may never answer close_notify.
        if graceful:
            self.writer.close()
        else:
            await super()._close(graceful=False)

    async def _read(self, timeout: float):
        return await asyncio.wait_for(self.frames.get(), timeout=timeout)


class H2FaultServer(_FaultServer):
    """Programmable HTTP/2 server emitting a [`ScenarioH2`][].

    Async context manager: enter to bind + start accepting, exit to
    shut down.  ``self.url`` is set after enter and gives the URL a
    real h2c client can dial.

    Parameters
    ----------
    scenario:
        The [`ScenarioH2`][] the executor walks for each accepted
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
        Optional ``ssl.SSLContext``.  When provided, the server
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
        [`ScenarioH2Result`][] from the most recently completed
        connection.  ``None`` before any client has connected.
    """

    Error = H2FaultServerError
    _wrong = 'HTTP/2'

    def _connection(self, reader, writer) -> _H2Connection:
        return _H2Connection(self, self.scenario, ScenarioH2Result(),
                             reader, writer)


__all__ = [
    'CLIENT_PREFACE',
    'H2FaultServer',
    'H2FaultServerError',
    'serialize_frame',
]
