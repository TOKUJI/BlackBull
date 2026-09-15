"""HTTP/2 frame objects compare by identity and are hashable.

Equality that looked only at type, flags and stream id reported frames with
different wire bytes as equal — two GOAWAYs with different error codes, two
PINGs are the everyday case — raised ``AttributeError`` when a frame met a
non-frame (``frame in [None, ...]``), and made every frame unhashable.
Identity has none of those defects: it never calls different bytes equal,
compares with anything, and agrees with ``hash``.
"""
import pytest

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameTypes

#: A payload each frame type's constructor accepts, keyed by every
#: ``FrameTypes`` member so a frame class added later cannot be skipped.
_PAYLOADS = {
    FrameTypes.DATA: b'abc',
    FrameTypes.HEADERS: b'',
    FrameTypes.PRIORITY: b'\x00\x00\x00\x01\x10',
    FrameTypes.RST_STREAM: b'\x00\x00\x00\x08',
    FrameTypes.SETTINGS: b'\x00\x03\x00\x00\x00\x64',
    FrameTypes.PUSH_PROMISE: b'\x00\x00\x00\x02',
    FrameTypes.PING: b'12345678',
    FrameTypes.GOAWAY: b'\x00\x00\x00\x01\x00\x00\x00\x00',
    FrameTypes.WINDOW_UPDATE: b'\x00\x00\x00\x0a',
    FrameTypes.CONTINUATION: b'',
    FrameTypes.PRIORITY_UPDATE: b'\x00\x00\x00\x01u=1',
}

_EVERY_TYPE = [pytest.param(t, id=t.name) for t in FrameTypes]


def _frame(type_: FrameTypes):
    return FrameFactory().create(type_, 0, 1, data=_PAYLOADS[type_])


def test_the_payload_table_covers_every_frame_type():
    assert set(_PAYLOADS) == set(FrameTypes)


def test_goaways_with_different_payloads_are_unequal():
    factory = FrameFactory()
    assert (factory.goaway(last_stream_id=1, error_code=0)
            != factory.goaway(last_stream_id=9, error_code=2))


def test_two_pings_with_one_payload_are_distinct_objects():
    factory = FrameFactory()
    first = factory.create(FrameTypes.PING, 0, 0, data=b'12345678')
    second = factory.create(FrameTypes.PING, 0, 0, data=b'12345678')
    assert first != second


@pytest.mark.parametrize('type_', _EVERY_TYPE)
def test_every_frame_is_hashable_and_distinct_in_a_set(type_):
    frame, twin = _frame(type_), _frame(type_)
    hash(frame)
    assert len({frame, twin}) == 2


@pytest.mark.parametrize('type_', _EVERY_TYPE)
def test_comparing_with_a_non_frame_answers_a_bool(type_):
    frame = _frame(type_)
    assert (frame == None) is False  # noqa: E711 — the comparison is the subject
    assert (frame != object()) is True


@pytest.mark.parametrize('type_', _EVERY_TYPE)
def test_a_frame_equals_itself(type_):
    frame = _frame(type_)
    assert frame == frame
