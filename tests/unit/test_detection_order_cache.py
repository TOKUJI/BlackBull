"""Unit tests for ``ProtocolRegistry.detection_order`` — the cached
cleartext-detection chain.

Background (limited-conn churn analysis, 2026-07-12): ``ConnectionActor``
rebuilt the detection order on every accepted connection (a dict copy + two
list allocations).  The order only changes when a raw protocol is registered
— at startup — so the registry now caches it as a tuple and rebuilds on
``register()``.  HTTP-only apps pay nothing per connection for the raw-
protocol machinery.
"""
from __future__ import annotations

from blackbull.server.protocol_registry import ProtocolRegistry


async def _noop_handler(reader, writer, ctx):  # pragma: no cover - never run
    pass


def test_default_order_is_http2_then_http1():
    reg = ProtocolRegistry()
    names = [b.name for b in reg.detection_order]
    assert names == ['http2', 'http1']


def test_order_is_cached_not_rebuilt_per_access():
    reg = ProtocolRegistry()
    assert reg.detection_order is reg.detection_order


def test_register_rebuilds_with_raw_binding_first():
    reg = ProtocolRegistry()
    before = reg.detection_order
    reg.register('mqtt', _noop_handler)
    after = reg.detection_order
    assert after is not before
    assert [b.name for b in after] == ['mqtt', 'http2', 'http1']
