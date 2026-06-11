"""Regression + sizing for hot-path classes that use ``__slots__``.

The classes under test are allocated per HTTP/2 stream or per HTTP/1.1
response and therefore live on the request hot path; dropping the
per-instance ``__dict__`` saves both bytes and ``dict_setitem`` work
on every construction.  The assertions here are regression guards —
if a future change reintroduces ``__dict__`` (e.g. by adding an attribute
without extending ``__slots__``), the test fails before the perf
regression hits a benchmark.

The printed sizes are informational only: ``sys.getsizeof`` reports the
base object size, not the recursive footprint of the slot values, but
that's the dimension this sprint set out to compress.
"""
import sys

import pytest

from blackbull.protocol.stream import Stream
from blackbull.server.sender import (
    AbstractWriter,
    HTTP1Sender,
    HTTP2Sender,
    WebSocketSender,
)


class _FakeWriter(AbstractWriter):
    """Stand-in for ``AbstractWriter`` — the senders only need an object
    to bind to ``self._writer``; they don't call into it during construction.
    Inherits from ``AbstractWriter`` so beartype's ``writer: AbstractWriter``
    parameter check on ``HTTP1Sender.__init__`` is satisfied.
    """
    async def write(self, data: bytes) -> None:  # pragma: no cover - test stub
        return None

    async def writelines(self, parts) -> None:  # pragma: no cover - test stub
        return None

    async def drain(self) -> None:  # pragma: no cover - test stub
        return None


# ---------------------------------------------------------------------------
# Slots-in-effect regression: __dict__ must be absent on instances.
# ---------------------------------------------------------------------------

def test_stream_has_no_dict():
    s = Stream(1)
    assert not hasattr(s, '__dict__'), (
        '__dict__ resurfaced on Stream; an attribute was added to __init__ '
        'without extending __slots__.'
    )
    assert hasattr(Stream, '__slots__')


def test_http1_sender_has_no_dict():
    s = HTTP1Sender(_FakeWriter())
    assert not hasattr(s, '__dict__'), (
        '__dict__ resurfaced on HTTP1Sender; check BaseSender and '
        'HTTP1Sender __slots__ declarations.'
    )


def test_http2_sender_has_no_dict():
    from blackbull.protocol.frame import FrameFactory
    s = HTTP2Sender(_FakeWriter(), FrameFactory(), stream_id=1)
    assert not hasattr(s, '__dict__'), (
        '__dict__ resurfaced on HTTP2Sender; check BaseSender and '
        'HTTP2Sender __slots__ declarations.'
    )


def test_websocket_sender_has_no_dict():
    s = WebSocketSender(_FakeWriter())
    assert not hasattr(s, '__dict__'), (
        '__dict__ resurfaced on WebSocketSender; check BaseSender and '
        'WebSocketSender __slots__ declarations.'
    )


# ---------------------------------------------------------------------------
# Sizing report — printed under -s, not asserted (machine-dependent).
# ---------------------------------------------------------------------------

def _sizeof(obj):
    """Best-effort per-instance size including the slot-storage descriptor.

    ``sys.getsizeof`` on a slot-equipped instance returns just the
    fixed-size object header + the inline slot pointers; the values the
    slots point at are not included.  For documentation that's the
    dimension we want — it characterises the per-instance overhead the
    framework pays for *holding* one of these objects, not the cost of
    the values themselves.
    """
    return sys.getsizeof(obj)


def test_print_per_instance_sizes(capsys):
    from blackbull.protocol.frame import FrameFactory

    sizes = {
        'Stream':           _sizeof(Stream(1)),
        'HTTP1Sender':      _sizeof(HTTP1Sender(_FakeWriter())),
        'HTTP2Sender':      _sizeof(HTTP2Sender(_FakeWriter(), FrameFactory(), 1)),
        'WebSocketSender':  _sizeof(WebSocketSender(_FakeWriter())),
    }
    # Print under pytest -s (or always — captured otherwise) so the values
    # land in the test log when the sprint-close measurement is taken.
    print('\nPer-instance sizes (sys.getsizeof, bytes):')
    for name, size in sizes.items():
        print(f'  {name:<20s} {size:>4d}')

    # Loose upper bounds — guard against accidental dict resurrection.
    # A ``__dict__``-equipped instance picks up ~56 bytes from the
    # PyDictObject header alone on CPython 3.12, so any per-instance
    # size above ~200 bytes here would suggest slots have been broken.
    for name, size in sizes.items():
        assert size < 200, (
            f'{name} is {size} bytes — far above the slots-equipped '
            f'baseline (~48-96 bytes); check that __slots__ is still in '
            f'effect.'
        )


# ---------------------------------------------------------------------------
# Functional smoke: a slot-equipped Stream must still support every
# operation the priority tree depends on.
# ---------------------------------------------------------------------------

def test_stream_priority_tree_still_works():
    root = Stream(0)
    child = root.add_child(1)
    grandchild = child.add_child(3)
    assert root.find_child(3) is grandchild
    assert grandchild.parent is child

    # Closing should remove the child from its parent.
    grandchild.close()
    assert child.find_child(3) is None


def test_stream_state_transitions_still_work():
    from blackbull.protocol.stream import StreamState

    s = Stream(1)
    assert s.state is StreamState.IDLE
    s.on_headers_received(end_stream=False)
    assert s.state is StreamState.OPEN
    s.on_data_received(end_stream=True)
    assert s.state is StreamState.CLOSED


def test_stream_rejects_undeclared_attribute():
    """Hard regression: assigning an undeclared attribute must raise.

    If this stops raising, ``__slots__`` has been silently broken (e.g.
    by adding ``__dict__`` to the slots list) and the memory win is
    gone.
    """
    s = Stream(1)
    with pytest.raises(AttributeError):
        s.brand_new_attribute = 1  # type: ignore[attr-defined]
