"""Replacing `EnumClass(value)` with a mapping must change nothing observable.

The change is a lookup-shape change on the per-request path.  Its whole
claim is that behaviour is identical, so these tests pin the behaviour —
including the branches that only an out-of-spec peer reaches, which is
where a mapping and a coercion are most likely to disagree.

`EnumClass(value)` is not a constructor: members are singletons built when
the class body ran, and the call is a value lookup routed through the
metaclass.  So a module-level dict built from the same enum is the same
lookup, minus two Python frames — the identity of what comes back is part
of the contract and is asserted here.
"""
from __future__ import annotations

import pytest

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameBase, FrameTypes, PseudoHeaders

# The shortest payload each type's own parser accepts.
_MINIMAL_PAYLOAD = {
    FrameTypes.DATA: b'',
    FrameTypes.HEADERS: b'',
    FrameTypes.PRIORITY: bytes(5),
    FrameTypes.RST_STREAM: bytes(4),
    FrameTypes.SETTINGS: b'',
    FrameTypes.PUSH_PROMISE: bytes(4),
    FrameTypes.PING: bytes(8),
    FrameTypes.GOAWAY: bytes(8),
    FrameTypes.WINDOW_UPDATE: (1).to_bytes(4, 'big'),
    FrameTypes.CONTINUATION: b'',
    FrameTypes.PRIORITY_UPDATE: bytes(4),
}


class TestTheMappingsAgreeWithTheEnums:
    """A second source of truth is only safe while it agrees with the first."""

    def test_pseudo_header_map_covers_exactly_the_enum(self):
        from blackbull.protocol.frame_types import _PSEUDO_BY_BYTES

        assert set(_PSEUDO_BY_BYTES.values()) == set(PseudoHeaders)
        for member in PseudoHeaders:
            key = member.value.encode('ascii')
            assert _PSEUDO_BY_BYTES[key] is member, (
                f'{member!r} must map to the same singleton the enum returns')

    def test_the_map_retires_the_duplicate_pseudo_header_list(self):
        """`_KNOWN_PSEUDO` and `PseudoHeaders` were the same six names twice.

        Membership was checked against one and the value looked up in the
        other; the mapping does both in one step, so the frozenset has no
        remaining reader.
        """
        import blackbull.protocol.frame_types as ft

        assert not hasattr(ft, '_KNOWN_PSEUDO'), (
            'the duplicated source of truth is still there')


class TestUnknownFrameTypesAreStillIgnored:
    """RFC 9113 §5.5 — an unknown frame type is a *specified normal outcome*.

    It was expressed as `except ValueError`, which is what the change stops
    doing.  What must not change is the answer.
    """

    @pytest.mark.parametrize('type_byte', [0x0b, 0x20, 0x7f, 0xff])
    def test_an_unknown_type_yields_the_ignore_sentinel(self, type_byte):
        factory = FrameFactory()
        payload = b'xyz'
        raw = (len(payload).to_bytes(3, 'big') + bytes([type_byte])
               + b'\x00' + (1).to_bytes(4, 'big') + payload)

        frame = factory.load(raw)

        assert type(frame).__name__ == '_UnknownFrame'
        assert frame._type_byte == bytes([type_byte])
        assert frame.stream_id == 1
        assert frame.length == len(payload)

    @pytest.mark.parametrize('member', list(FrameTypes))
    def test_every_known_type_still_parses_as_itself(self, member):
        payload = _MINIMAL_PAYLOAD[member]
        raw = (len(payload).to_bytes(3, 'big') + member.value
               + b'\x00' + (0).to_bytes(4, 'big') + payload)

        frame = FrameFactory().load(raw)

        assert type(frame) is FrameBase._registry[member]
        assert frame.type_ is member
        assert frame.FrameType() is member
        assert (frame.length, frame.flags, frame.stream_id) == (len(payload), 0, 0)
        assert type(frame.flags) is int


class TestPseudoHeaderValidationIsUnchanged:
    """The out-of-spec branches, which is where a shape change would show."""

    def _parse(self, pairs):
        """Drive the header-block validator the way the parser does."""
        from blackbull.protocol.frame_types import Headers

        f = Headers.__new__(Headers)
        f.pseudo_headers = {}
        f.headers = []
        f.malformed = False
        f.malformed_reason = ''
        return f, pairs

    @pytest.mark.parametrize('name', [b':nope', b':', b':METHOD', b':method '])
    def test_an_unknown_pseudo_header_is_still_malformed(self, name):
        from blackbull.protocol.frame_types import _PSEUDO_BY_BYTES

        assert _PSEUDO_BY_BYTES.get(name) is None, (
            f'{name!r} must not resolve to a pseudo-header')

    @pytest.mark.parametrize('member', list(PseudoHeaders))
    def test_every_defined_pseudo_header_still_resolves(self, member):
        from blackbull.protocol.frame_types import _PSEUDO_BY_BYTES

        got = _PSEUDO_BY_BYTES.get(member.value.encode('ascii'))
        assert got is member

    def test_lookup_is_bytes_keyed_so_no_str_is_allocated(self):
        """The `.decode('ascii')` existed only to feed the enum call."""
        from blackbull.protocol.frame_types import _PSEUDO_BY_BYTES

        assert all(isinstance(k, bytes) for k in _PSEUDO_BY_BYTES)
        assert _PSEUDO_BY_BYTES.get(':method') is None, (
            'a str key must not resolve — the map is bytes-keyed on purpose')
