"""
Shared fixtures for MQTT 5.0 conformance tests.

Provides in-process fake Reader/Writer so MQTT Actor tests can run without
live sockets (same pattern as tests/conformance/http2/).

See: tests/conformance/http2/conftest.py for the equivalent HTTP/2 fixtures.
"""

import asyncio
import contextlib

import pytest
import pytest_asyncio

from blackbull.server.protocol_registry import ProtocolContext
from blackbull.server.sender import AbstractWriter
from blackbull.server.recipient import AbstractReader


@pytest.fixture
def mqtt_ctx():
    """Return a ProtocolContext suitable for MQTT broker testing."""
    return ProtocolContext(
        peername=('127.0.0.1', 54321),
        sockname=('0.0.0.0', 1883),
        ssl=False,
        aggregator=None,
        connection_id='test-conn-001',
        protocol='mqtt',
    )


# ---------------------------------------------------------------------------
# Broker oracle seam (Sprint 53)
# ---------------------------------------------------------------------------
#
# The broker-driving conformance tests assert on the *packets the broker emits*
# in response to packets fed in — that is the MQTT wire contract we must keep.
# But they used to reach the broker by constructing ``MQTTActor`` directly and
# poking module globals, which couples them to the broker's internal structure.
#
# This harness is the single seam between those tests and the broker.  Today it
# wraps the procedural broker; after the Sprint 53 actor refactor only this class
# changes (to spin up a ``BrokerActor`` + per-connection actor) — the call sites
# and their assertions are untouched, so the same suite proves the refactor
# preserved wire behaviour.  That is what makes it a regression oracle rather
# than tests rewritten to match new code.

class MQTTHarness:
    """Stable test seam for the MQTT broker (see module note).

    Owns one live :class:`BrokerActor` task and serves each connection through
    :func:`serve_connection` — the same wiring ``MQTTExtension`` uses in
    production.  ``serve(reader, writer, ctx)`` returns an object exposing
    ``run()`` so the existing tests' ``create_task(actor.run())`` shape is
    unchanged.
    """

    def __init__(self) -> None:
        from blackbull.mqtt.broker import BrokerActor
        self._broker = BrokerActor()
        self._broker_task = None

    async def start(self) -> None:
        self._broker_task = asyncio.create_task(self._broker.run())

    async def stop(self) -> None:
        if self._broker_task is not None:
            self._broker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._broker_task

    def serve(self, reader, writer, ctx, **kwargs):
        from blackbull.mqtt.connection import serve_connection
        broker = self._broker
        app_handlers = kwargs.get('app_handlers')

        class _Conn:
            async def run(_self):
                await serve_connection(reader, writer, ctx, broker,
                                       app_handlers=app_handlers)

        return _Conn()

    # -- broker introspection used by session/retained state assertions --------

    @property
    def sessions(self) -> dict:
        return self._broker._sessions

    @property
    def retained(self) -> dict:
        return self._broker._retained


@pytest_asyncio.fixture
async def mqtt():
    """A fresh, empty broker harness for one test (broker task stopped on teardown)."""
    harness = MQTTHarness()
    await harness.start()
    try:
        yield harness
    finally:
        await harness.stop()
