"""Deliberately malformed HTTP/1.1 server.

Assemble output independently of production senders. Production mode is
refused; non-loopback binds require allow_remote=True.
"""
from __future__ import annotations

import asyncio

from ..protocol.framing import _HEAD_END
from ._scenario import _FaultServer, _ServerConnection
from .scenario_h1_server import ScenarioH1ServerResult, request_matches


class H1FaultServerError(RuntimeError):
    """Raised when the fault server refuses to run."""


class _H1Connection(_ServerConnection):
    _matches = staticmethod(request_matches)

    async def _read(self, timeout: float) -> bytes | None:
        """One request head, or ``None`` when the client stops sending."""
        try:
            head = await asyncio.wait_for(self.reader.readuntil(_HEAD_END),
                                          timeout=timeout)
        except asyncio.IncompleteReadError:
            return None
        self.result.client_bytes_received += len(head)
        return head

    def _seen(self, head: bytes, *, kept: bool) -> None:
        if kept:
            self.result.request_head = head

    def _completed(self, step) -> None:
        self.result.steps_completed.append(type(step).__name__)

    def failed(self, exc: Exception) -> None:
        self.result.exception = exc


class H1FaultServer(_FaultServer):
    """Serves one [`ScenarioH1Server`][] to each connecting client."""

    Error = H1FaultServerError
    _wrong = 'HTTP/1.1'

    def _connection(self, reader, writer) -> _H1Connection:
        return _H1Connection(self.scenario, ScenarioH1ServerResult(),
                             reader, writer)
