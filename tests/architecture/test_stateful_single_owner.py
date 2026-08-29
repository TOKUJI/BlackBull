"""A protocol that keeps state is answered by one process, or not at all.

The invariant is not "one process accepts" — it is that the process answering
a later exchange holds what the earlier one left behind.  Two things can break
that, and only one of them is about accepting:

* a **dedicated port** is bound once and served by one worker, which is sound;
* a **shared listener** is served by every worker, so a stateful protocol
  sniffed off it is answered by whichever worker took the connection, and the
  ones that never saw the earlier exchange answer with nothing.

``stateful=True`` (the default for a raw binding) is what lets the master tell
the two apart.  A stateful binding stops claiming shared-port connections once
there is more than one worker; a stateful binding that has *only* a shared
listener cannot be made single-owner at all, so it is refused rather than left
to answer wrongly.

Design: `BLA-A-17` [private].
"""
from __future__ import annotations

import socket

import pytest

from blackbull import BlackBull
from blackbull.server.listener import Listener, Tcp
from blackbull.server.multiworker import MultiWorkerServer
from blackbull.server.protocol_registry import ProtocolDetector


class _FirstByte(ProtocolDetector):
    def __init__(self, byte: bytes, name: str):
        self._byte, self._name = byte, name

    def detect(self, first_bytes: bytes, alpn: str | None) -> bool:
        return first_bytes[:1] == self._byte

    @property
    def protocol_name(self) -> str:
        return self._name


async def _noop(reader, writer, ctx):  # pragma: no cover - never dialled
    pass


@pytest.fixture
def listeners():
    socks = []

    def make():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        socks.append(sock)
        return [(Listener(Tcp(sock.getsockname()[1])), [sock])]

    yield make
    for sock in socks:
        sock.close()


class TestDefault:
    def test_a_raw_binding_is_stateful_unless_it_says_otherwise(self):
        app = BlackBull()
        binding = app.register_protocol_handler('brk', _noop, port=1)
        assert binding.stateful is True

    def test_and_can_say_otherwise(self):
        app = BlackBull()
        binding = app.register_protocol_handler('stateless', _noop, port=1,
                                                stateful=False)
        assert binding.stateful is False


class TestSharedDispatch:
    """Rule one: a stateful binding stops sharing once workers do."""

    @staticmethod
    def _app(**kwargs):
        app = BlackBull()
        app.register_protocol_handler(
            'brk', _noop, detector=_FirstByte(b'\x10', 'brk'), **kwargs)
        return app

    def test_one_worker_keeps_the_shared_port(self, listeners):
        app = self._app(port=1883)
        MultiWorkerServer(app, listeners(), None, workers=1)
        assert app._protocol_registry.raw_bindings['brk'].claims(b'\x10', None)

    def test_many_workers_take_it_away(self, listeners):
        app = self._app(port=1883)
        MultiWorkerServer(app, listeners(), None, workers=4)
        assert not app._protocol_registry.raw_bindings['brk'].claims(b'\x10', None), \
            'a stateful protocol must not be answered by whichever worker accepted'

    def test_a_stateless_binding_keeps_sharing(self, listeners):
        app = self._app(port=1883, stateful=False)
        MultiWorkerServer(app, listeners(), None, workers=4)
        assert app._protocol_registry.raw_bindings['brk'].claims(b'\x10', None)

    def test_the_dedicated_port_is_untouched(self, listeners):
        """Taking the shared route away leaves the sound one in place."""
        app = self._app(port=1883)
        MultiWorkerServer(app, listeners(), None, workers=4)
        assert app._protocol_registry.raw_bindings['brk'].port == 1883


class TestNoPortAndStatefulIsRefused:
    """Rule two: what cannot be made single-owner is not left to answer wrongly."""

    @staticmethod
    def _app(**kwargs):
        app = BlackBull()
        app.register_protocol_handler(
            'brk', _noop, detector=_FirstByte(b'\x10', 'brk'), **kwargs)
        return app

    def test_refused_before_the_workers_fork(self, listeners):
        with pytest.raises(RuntimeError) as excinfo:
            MultiWorkerServer(self._app(), listeners(), None, workers=4)
        message = str(excinfo.value)
        assert 'brk' in message
        assert 'port' in message, 'the message must name a way out'
        assert 'workers=1' in message, 'and the other one'

    def test_one_worker_is_allowed(self, listeners):
        # There is nothing to scatter across.
        MultiWorkerServer(self._app(), listeners(), None, workers=1)

    def test_stateless_is_allowed(self, listeners):
        MultiWorkerServer(self._app(stateful=False), listeners(), None, workers=4)

    def test_a_dedicated_port_is_allowed(self, listeners):
        MultiWorkerServer(self._app(port=1883), listeners(), None, workers=4)


def test_the_mqtt_extension_survives_the_documented_deployment(listeners):
    """``app.run(port=8000, workers=N)`` with MQTT on 1883 — what the guide
    and the write-up both show."""
    pytest.importorskip('blackbull.mqtt')
    from blackbull.mqtt import MQTTExtension

    app = BlackBull()
    app.add_extension(MQTTExtension(port=1883))
    MultiWorkerServer(app, listeners(), None, workers=4)

    binding = app._protocol_registry.raw_bindings['mqtt']
    assert binding.port == 1883, 'the broker keeps its own port'
    assert not binding.claims(b'\x10', None), 'and stops riding the HTTP one'
