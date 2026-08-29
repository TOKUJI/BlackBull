"""Unit tests for the listener vocabulary.

A deployment says what sockets it wants with :class:`Listener`; each one
names an address, what speaks there, whether TLS terminates there, and how
many workers own it.  These tests pin the parts that decide behaviour
elsewhere — the defaults, the single place ownership is derived, and the
validation that stops a mistake at construction rather than at bind time.

Design: `.claude/planning/designs/listener-vocabulary.md`.
"""
from __future__ import annotations

import ssl

import pytest

from blackbull.server.listener import InheritedFd, Listener, Tcp, Unix

try:
    from beartype.roar import BeartypeCallHintParamViolation as _BeartypeViolation
except ImportError:  # pragma: no cover - beartype is a hard dep in practice
    _BeartypeViolation = None
_TYPE_ERRORS = (TypeError,) if _BeartypeViolation is None else (TypeError, _BeartypeViolation)


class TestAddresses:
    def test_tcp_defaults_to_every_interface(self):
        assert Tcp(8080).host is None

    def test_tcp_carries_the_port_it_was_given(self):
        assert Tcp(8080, host='127.0.0.1') == Tcp(8080, host='127.0.0.1')
        assert Tcp(8080) != Tcp(8081)

    def test_port_zero_is_allowed(self):
        # The OS picks a free port; tests and ephemeral binds rely on it.
        assert Tcp(0).port == 0

    @pytest.mark.parametrize('port', [-1, 65536])
    def test_a_port_outside_the_range_is_refused(self, port):
        with pytest.raises(ValueError, match='port'):
            Tcp(port)

    def test_unix_and_fd_are_addresses_too(self):
        assert Unix('/tmp/bb.sock').path == '/tmp/bb.sock'
        assert InheritedFd(3).fd == 3

    def test_a_negative_fd_is_refused(self):
        with pytest.raises(ValueError, match='fd'):
            InheritedFd(-1)

    def test_addresses_are_immutable(self):
        addr = Tcp(8080)
        with pytest.raises(Exception):
            addr.port = 9090


class TestListenerDefaults:
    def test_a_bare_listener_speaks_http_in_the_clear_on_every_worker(self):
        listener = Listener(Tcp(8080))
        assert listener.speaks == 'http'
        assert listener.tls is None
        assert listener.workers == 'all'

    def test_tls_belongs_to_the_listener(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        assert Listener(Tcp(8443), tls=ctx).tls is ctx
        # ...and the one next to it can be cleartext at the same time.
        assert Listener(Tcp(8080)).tls is None


class TestWorkerOwnership:
    """Ownership is derived in exactly one place: here."""

    def test_http_is_owned_by_every_worker(self):
        assert Listener(Tcp(8080)).workers == 'all'

    def test_anything_else_is_owned_by_one(self):
        # A raw protocol holds state per broker, so scattering it across
        # workers scatters the state with it.
        assert Listener(Tcp(1883), speaks='mqtt').workers == 'one'

    def test_an_explicit_value_wins_over_the_derived_one(self):
        assert Listener(Tcp(1883), speaks='mqtt', workers='all').workers == 'all'
        assert Listener(Tcp(8080), workers='one').workers == 'one'

    def test_an_unknown_ownership_is_refused(self):
        # Under beartype instrumentation the Literal is refused before
        # __post_init__ runs; a served process has no instrumentation and
        # gets the ValueError.  Both are refusals.
        with pytest.raises((ValueError, *_TYPE_ERRORS)):
            Listener(Tcp(8080), workers='some')


class TestSpeaksIsPositive:
    """HTTP is a value, never the absence of a raw handler."""

    def test_http_is_named(self):
        assert Listener(Tcp(8080)).speaks == 'http'

    def test_a_raw_protocol_names_itself(self):
        assert Listener(Tcp(1883), speaks='mqtt').speaks == 'mqtt'

    @pytest.mark.parametrize('speaks', ['', '   '])
    def test_an_empty_name_is_refused(self, speaks):
        with pytest.raises(ValueError, match='speaks'):
            Listener(Tcp(8080), speaks=speaks)

    def test_none_is_not_a_protocol(self):
        with pytest.raises(_TYPE_ERRORS):
            Listener(Tcp(8080), speaks=None)


class TestFourPortsAreFourListeners:
    """The finding that sized the vocabulary: HttpArena's four ports differ
    only in address and whether TLS terminates there."""

    def test_the_four_differ_in_two_fields_and_no_others(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        listeners = [
            Listener(Tcp(8080)),            # cleartext h1 / h2c / ws
            Listener(Tcp(8082)),            # h2c — same stack, another port
            Listener(Tcp(8081), tls=ctx),   # TLS, ALPN picks http/1.1
            Listener(Tcp(8443), tls=ctx),   # TLS, ALPN picks h2
        ]
        assert {listener.speaks for listener in listeners} == {'http'}
        assert {listener.workers for listener in listeners} == {'all'}
        assert len({listener.where for listener in listeners}) == 4
