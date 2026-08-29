"""The sockets a deployment wants, said once each.

A :class:`Listener` is one listening socket: where it is, what speaks there,
whether TLS terminates there, and how many workers own it.  ``Server`` binds a
list of them, so no port is privileged over another and HTTP is a value rather
than the absence of a raw handler.

The four ports a cleartext-plus-TLS deployment needs are four listeners that
differ in address and TLS and in nothing else — HTTP/1.1 versus h2c is preface
detection, and TLS h1 versus h2 is ALPN, both already handled downstream.

Design: `BLA-A-17` [private].
"""
from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Literal

__all__ = ['Address', 'InheritedFd', 'Listener', 'Tcp', 'Unix', 'Workers']

Workers = Literal['all', 'one']

_MAX_PORT = 65535


@dataclass(frozen=True, slots=True)
class Tcp:
    """A TCP port, on *host* or on every interface when it is ``None``.

    ``port=0`` asks the OS for a free one; the bound port is read back after
    binding.
    """

    port: int
    host: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.port <= _MAX_PORT:
            raise ValueError(f'port must be 0..{_MAX_PORT}, got {self.port}')


@dataclass(frozen=True, slots=True)
class Unix:
    """An AF_UNIX path."""

    path: str

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError('path must not be empty')


@dataclass(frozen=True, slots=True)
class InheritedFd:
    """A socket already bound and listening, handed over by a supervisor.

    Covers systemd-style activation and the re-exec handoff that keeps the
    listener continuous across an auto-reload.
    """

    fd: int

    def __post_init__(self) -> None:
        if self.fd < 0:
            raise ValueError(f'fd must not be negative, got {self.fd}')


Address = Tcp | Unix | InheritedFd

HTTP = 'http'
"""What :attr:`Listener.speaks` names for the detecting HTTP stack."""


@dataclass(frozen=True, slots=True)
class Listener:
    """One listening socket and what happens on it.

    *speaks* is always a positive name: ``'http'`` selects the stack that
    detects HTTP/1.1, h2c and WebSocket upgrades, and a raw protocol names
    itself.

    *tls* is the listener's own context, so a second certificate — or mTLS on
    one port and not another — is sayable.

    *workers* is where ownership is decided, and the only place.  Left unset it
    follows *speaks*: the HTTP stack is stateless and runs on every worker,
    while a raw protocol holds state that scattering across workers would
    scatter with it.  Pass it explicitly to override.
    """

    where: Address
    speaks: str = HTTP
    tls: ssl.SSLContext | None = None
    workers: Workers | None = None

    def __post_init__(self) -> None:
        # beartype checks these annotations only under instrumentation; a
        # served process runs without it, and `None.strip()` is a worse
        # answer than saying which argument was wrong.
        if not isinstance(self.speaks, str):
            raise TypeError(
                f'speaks must be a protocol name, got {type(self.speaks).__name__}')
        if not self.speaks.strip():
            raise ValueError('speaks must name a protocol, not an empty string')
        if self.workers is None:
            object.__setattr__(
                self, 'workers', 'all' if self.speaks == HTTP else 'one')
        elif self.workers not in ('all', 'one'):
            raise ValueError(
                f"workers must be 'all' or 'one', got {self.workers!r}")
