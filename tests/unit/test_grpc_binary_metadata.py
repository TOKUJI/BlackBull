"""Binary response metadata has one unambiguous HTTP wire representation."""
import base64

import pytest

from blackbull.grpc.asgi import _encode_outbound_metadata


def _b64(value: bytes) -> bytes:
    return base64.b64encode(value).rstrip(b'=')


def _field(tag: int, value: bytes) -> bytes:
    assert len(value) < 128
    return bytes((tag, len(value))) + value


_STATUS_WITH_MESSAGE = b'\x08\x03' + _field(0x12, b'bad arg')
_ANY_ONE = _field(0x0a, b'type.googleapis.com/example.One') + \
    _field(0x12, b'\x08\x01')
_ANY_TWO = _field(0x0a, b'type.googleapis.com/example.Two') + \
    _field(0x12, b'\x12\x01x')


@pytest.mark.parametrize('wire', [
    b'\x08\x03',
    _STATUS_WITH_MESSAGE,
    b'\x08\x03' + _field(0x1a, _ANY_ONE),
    _STATUS_WITH_MESSAGE + _field(0x1a, _ANY_ONE) +
    _field(0x1a, _ANY_TWO),
])
def test_canonical_non_ok_status_wire_value_is_preserved(wire):
    encoded = _b64(wire)

    assert _encode_outbound_metadata([
        (b'grpc-status-details-bin', encoded),
    ]) == [(b'grpc-status-details-bin', encoded)]


@pytest.mark.parametrize('value', [
    b'abcd',
    b'not?',
    _b64(b'\x08'),
    _b64(b'\x08\x03\x12\x05x'),
    _b64(b'\x0d\x03'),
    _b64(b'\x08\x00'),
    _b64(b'\x08\x03\x12\x01\xff'),
    _b64(b'\x08\x83\x00'),
    _b64(b'\x08\x03\x1a\x02\x0a'),
])
def test_non_status_values_are_encoded_exactly_once(value):
    assert _encode_outbound_metadata([
        (b'grpc-status-details-bin', value),
    ]) == [(b'grpc-status-details-bin', _b64(value))]


def test_raw_status_and_other_binary_metadata_are_encoded_exactly_once():
    assert _encode_outbound_metadata([
        (b'grpc-status-details-bin', _STATUS_WITH_MESSAGE),
        (b'x-custom-bin', b'abcd'),
    ]) == [
        (b'grpc-status-details-bin', _b64(_STATUS_WITH_MESSAGE)),
        (b'x-custom-bin', _b64(b'abcd')),
    ]
