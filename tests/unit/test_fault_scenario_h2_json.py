"""Every arm of the server-scenario JSON codec round-trips.

Each arm is driven both ways: from a frame built as production builds one
(``FrameFactory``) and from a record as a file holds one.  Feeding the codec
only its own output hides an arm whose frame keeps its payload somewhere the
codec does not look.

``ROUND_TRIP_FRAME_CLASSES`` is asserted against the table, so an arm added
to the codec without a row here fails instead of passing by default.
"""
from __future__ import annotations

import base64

import pytest

from blackbull.fault_injection.h2_server import serialize_frame
from blackbull.fault_injection.scenario_h2 import (
    ROUND_TRIP_FRAME_CLASSES,
    ScenarioH2,
    SendFrame,
    _frame_from_dict,
    _frame_to_dict,
    scenario_from_json,
    scenario_to_json,
)
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (
    DataFrameFlags,
    ErrorCodes,
    FrameTypes,
    SettingFrame,
    SettingFrameFlags,
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def _wire(type_byte: bytes, flags: int, stream_id: int, payload: bytes = b'') -> bytes:
    """RFC 9113 §4.1 header + payload, written without the serialiser."""
    return (
        len(payload).to_bytes(3, 'big')
        + type_byte
        + flags.to_bytes(1, 'big')
        + (stream_id & 0x7fffffff).to_bytes(4, 'big')
        + payload
    )


# JSON has no tuples: a record read out of a file holds lists, and the writer
# has to emit that shape for the `== record` comparisons below to mean
# anything.
_SETTINGS_ENTRIES = [[3, 100], [4, 65535]]
_SETTINGS_PAYLOAD = b''.join(
    identifier.to_bytes(2, 'big') + value.to_bytes(4, 'big')
    for identifier, value in _SETTINGS_ENTRIES)
_PING_PAYLOAD = b'12345678'
_DATA_PAYLOAD = b'hello'

#: arm name -> (the record a scenario file holds, the bytes it describes)
ARMS: dict[str, tuple[dict, bytes]] = {
    'SettingFrame': (
        {'class': 'SettingFrame', 'stream_id': 0, 'flags': 0,
         'settings': _SETTINGS_ENTRIES},
        _wire(FrameTypes.SETTINGS.value, 0, 0, _SETTINGS_PAYLOAD),
    ),
    'WindowUpdate': (
        {'class': 'WindowUpdate', 'stream_id': 1, 'flags': 0,
         'window_size_increment': 4096},
        _wire(FrameTypes.WINDOW_UPDATE.value, 0, 1, (4096).to_bytes(4, 'big')),
    ),
    'RstStream': (
        {'class': 'RstStream', 'stream_id': 3, 'flags': 0, 'error_code': 8},
        _wire(FrameTypes.RST_STREAM.value, 0, 3, (8).to_bytes(4, 'big')),
    ),
    'GoAway': (
        {'class': 'GoAway', 'stream_id': 0, 'flags': 0,
         'last_stream_id': 3, 'error_code': 0, 'append_data': ''},
        _wire(FrameTypes.GOAWAY.value, 0, 0,
              (3).to_bytes(4, 'big') + (0).to_bytes(4, 'big')),
    ),
    'Ping': (
        {'class': 'Ping', 'stream_id': 0, 'flags': 0,
         'payload': _b64(_PING_PAYLOAD)},
        _wire(FrameTypes.PING.value, 0, 0, _PING_PAYLOAD),
    ),
    'Data': (
        {'class': 'Data', 'stream_id': 1, 'flags': 0,
         'data': _b64(_DATA_PAYLOAD)},
        _wire(FrameTypes.DATA.value, 0, 1, _DATA_PAYLOAD),
    ),
}


def _documented_frames() -> dict[str, object]:
    """One frame per arm, built the way production builds one."""
    factory = FrameFactory()
    return {
        'SettingFrame': factory.settings(max_concurrent_streams=100,
                                         initial_window_size=65535),
        'WindowUpdate': factory.window_update(1, 4096),
        'RstStream': factory.rst_stream(3, ErrorCodes.CANCEL),
        'GoAway': factory.goaway(last_stream_id=3, error_code=0),
        'Ping': factory.create(FrameTypes.PING, 0, 0, data=_PING_PAYLOAD),
        'Data': factory.create(FrameTypes.DATA, 0, 1, data=_DATA_PAYLOAD),
    }


def test_the_table_covers_every_declared_arm():
    """A new arm must arrive with a row; deleting one must arrive with an edit."""
    assert set(ARMS) == set(ROUND_TRIP_FRAME_CLASSES)


@pytest.mark.parametrize('name', sorted(ARMS))
def test_a_documented_frame_writes_the_record(name):
    record, _ = ARMS[name]
    assert _frame_to_dict(_documented_frames()[name]) == record


@pytest.mark.parametrize('name', sorted(ARMS))
def test_a_documented_frame_reaches_the_wire(name):
    """The executor's encoder and the frame's own encoder must agree."""
    _, wire = ARMS[name]
    frame = _documented_frames()[name]
    assert serialize_frame(frame) == wire
    assert frame.save() == wire


@pytest.mark.parametrize('name', sorted(ARMS))
def test_a_record_reads_back_to_itself(name):
    record, _ = ARMS[name]
    assert _frame_to_dict(_frame_from_dict(record)) == record


@pytest.mark.parametrize('name', sorted(ARMS))
def test_a_rebuilt_frame_matches_the_record_on_the_wire(name):
    record, wire = ARMS[name]
    frame = _frame_from_dict(record)
    assert serialize_frame(frame) == wire
    assert frame.save() == wire


@pytest.mark.parametrize('name,flags', [
    ('RstStream', 0x8),     # undefined for RST_STREAM
    ('WindowUpdate', 0x1),  # undefined for WINDOW_UPDATE
    ('Ping', 0x1),          # ACK
    ('Data', 0x1),          # END_STREAM
])
def test_recorded_flags_are_not_dropped(name, flags):
    """A flag the reader drops is a replay that differs from the recording.

    The frame classes' factory helpers fix ``flags`` to INIT, which is why
    the reader builds from the payload instead of calling them.
    """
    record, wire = ARMS[name]
    flagged = dict(record, flags=flags)
    rebuilt = _frame_from_dict(flagged)

    assert rebuilt.flags == flags
    assert _frame_to_dict(rebuilt) == flagged
    assert serialize_frame(rebuilt) == wire[:4] + bytes([flags]) + wire[5:]
    assert rebuilt.save() == wire[:4] + bytes([flags]) + wire[5:]


def test_a_padded_data_frame_is_refused_in_every_direction():
    """The record holds the unpadded body, so padding is not recoverable."""
    padded = int(DataFrameFlags.PADDED)
    parsed = FrameFactory().create(FrameTypes.DATA, padded, 1,
                                   data=b'\x02hello\x00\x00')
    assert parsed.payload == _DATA_PAYLOAD

    with pytest.raises(TypeError):
        _frame_to_dict(parsed)
    with pytest.raises(TypeError):
        serialize_frame(parsed)
    with pytest.raises(ValueError):
        _frame_from_dict({'class': 'Data', 'stream_id': 1, 'flags': padded,
                          'data': _b64(_DATA_PAYLOAD)})


def test_a_settings_payload_that_is_not_whole_entries_is_refused():
    """The record holds entries; a partial one has no representation.

    Nor can the executor re-encode it: the record's entries are what both
    paths describe, so the octets go through ``SendRawBytes`` instead.
    """
    malformed = FrameFactory().create(FrameTypes.SETTINGS, 0, 0,
                                      data=b'\x00\x03\x00')
    with pytest.raises(TypeError):
        _frame_to_dict(malformed)
    with pytest.raises(TypeError):
        serialize_frame(malformed)


def test_a_setting_identifier_this_build_does_not_name_survives():
    """RFC 9113 §6.5.2 — an unknown identifier is ignored, not dropped."""
    unknown = FrameFactory().create(FrameTypes.SETTINGS, 0, 0,
                                    data=b'\x00\x63\x00\x00\x00\x07')
    assert unknown.settings == [(0x63, 7)]

    record = _frame_to_dict(unknown)
    assert record['settings'] == [[0x63, 7]]
    rebuilt = _frame_from_dict(record)
    assert rebuilt.save() == unknown.save()
    assert serialize_frame(rebuilt) == serialize_frame(unknown)


def test_a_settings_frame_with_a_body_follows_its_own_encoder():
    """ACK does not blank the body: the recorded octets are what is sent."""
    with_body = FrameFactory().create(FrameTypes.SETTINGS,
                                      int(SettingFrameFlags.ACK), 0,
                                      data=b'\x00\x03\x00\x00\x00\x64')
    assert serialize_frame(with_body) == with_body.save()

    rebuilt = _frame_from_dict(_frame_to_dict(with_body))
    assert rebuilt.save() == with_body.save()
    assert serialize_frame(rebuilt) == serialize_frame(with_body)


def test_a_goaway_with_trailing_octets_round_trips():
    """GOAWAY's trailing octets are part of the frame, so the record keeps them."""
    parsed = FrameFactory().load(_wire(
        FrameTypes.GOAWAY.value, 0, 0,
        (3).to_bytes(4, 'big') + (0).to_bytes(4, 'big') + b'debug'))
    assert parsed.append_data == b'debug'

    record = _frame_to_dict(parsed)
    assert record['append_data'] == _b64(b'debug')
    rebuilt = _frame_from_dict(record)
    assert rebuilt.save() == parsed.save()
    assert serialize_frame(rebuilt) == serialize_frame(parsed)


def test_an_over_long_window_update_is_refused_rather_than_recorded():
    """A five-octet WINDOW_UPDATE is a fault the record cannot state.

    Reading it back would pack a value that needs five octets into the four
    the record describes, so neither path takes it; ``SendRawBytes`` does.
    """
    over_long = FrameFactory().create(FrameTypes.WINDOW_UPDATE, 0, 1,
                                      data=b'\x01\x00\x00\x00\x00')
    assert over_long.window_size == 2 ** 32

    with pytest.raises(TypeError):
        _frame_to_dict(over_long)
    with pytest.raises(TypeError):
        serialize_frame(over_long)


def test_a_window_update_with_a_mismatched_length_is_refused():
    """The header length and the payload have to agree before either is used."""
    mismatched = FrameFactory().create(FrameTypes.WINDOW_UPDATE, 0, 1,
                                       data=b'\x00\x00\x10\x00')
    mismatched.length = 3

    with pytest.raises(TypeError):
        _frame_to_dict(mismatched)
    with pytest.raises(TypeError):
        serialize_frame(mismatched)


def test_a_short_goaway_is_refused_rather_than_normalised():
    """A GOAWAY parsed from four octets keeps no octets to reproduce."""
    short = FrameFactory().create(FrameTypes.GOAWAY, 0, 0,
                                  data=b'\x00\x00\x00\x03')

    with pytest.raises(TypeError):
        _frame_to_dict(short)
    with pytest.raises(TypeError):
        serialize_frame(short)


def test_a_reserved_stream_bit_is_refused_by_both_directions():
    """RFC 9113 §4.1 reserves the stream identifier's top bit.

    The executor masks it, so a frame carrying it would replay on a different
    stream than the record states.
    """
    reserved = FrameFactory().create(FrameTypes.DATA, 0, 0x80000001,
                                     data=_DATA_PAYLOAD)
    assert reserved.stream_id == 0x80000001

    with pytest.raises(TypeError):
        _frame_to_dict(reserved)
    with pytest.raises(TypeError):
        serialize_frame(reserved)
    with pytest.raises(ValueError):
        _frame_from_dict({'class': 'Data', 'stream_id': 0x80000001, 'flags': 0,
                          'data': _b64(_DATA_PAYLOAD)})


def test_a_frame_carrying_the_wrong_type_byte_is_refused():
    """The record names a class, and the class names the wire type."""
    wrong = SettingFrame(length=6, type_=FrameTypes.DATA, flags=0, stream_id=0,
                         data=b'\x00\x03\x00\x00\x00\x64')

    with pytest.raises(TypeError):
        _frame_to_dict(wrong)
    with pytest.raises(TypeError):
        serialize_frame(wrong)


def test_a_record_field_that_cannot_fit_its_octets_is_refused():
    """A hand-written record is read for what it says, and refused if it lies."""
    with pytest.raises(ValueError):
        _frame_from_dict({'class': 'Data', 'stream_id': -1, 'flags': 0, 'data': ''})
    with pytest.raises(ValueError):
        _frame_from_dict({'class': 'Data', 'stream_id': 0, 'flags': 256, 'data': ''})
    with pytest.raises(ValueError):
        _frame_from_dict({'class': 'GoAway', 'stream_id': 3, 'flags': 0,
                          'last_stream_id': 0, 'error_code': 0})
    with pytest.raises(ValueError):
        _frame_from_dict({'class': 'RstStream', 'stream_id': 1, 'flags': 0,
                          'error_code': 2 ** 40})


def test_a_payload_over_the_frame_length_field_is_refused():
    """RFC 9113 §4.1 gives the frame length 24 bits, so nothing above fits."""
    over_long = FrameFactory().create(FrameTypes.DATA, 0, 1,
                                      data=b'x' * 0x1000000)

    with pytest.raises(TypeError):
        _frame_to_dict(over_long)
    with pytest.raises(TypeError):
        serialize_frame(over_long)
    with pytest.raises(ValueError):
        _frame_from_dict({'class': 'Data', 'stream_id': 1, 'flags': 0,
                          'data': _b64(b'x' * 0x1000000)})


def test_an_unknown_class_is_refused_by_name():
    with pytest.raises(ValueError):
        _frame_from_dict({'class': 'NoSuchFrame', 'stream_id': 0, 'flags': 0})


def test_a_recorded_rst_stream_scenario_survives_a_round_trip():
    """End to end: a RST_STREAM scenario written to JSON Lines is read back.

    The frame is built the way a scenario author builds one — through the
    factory — so the failure is in the reader, not in the test's setup.
    """
    scenario = ScenarioH2(
        name='rst_stream_replay',
        steps=(SendFrame(frame=FrameFactory().rst_stream(3, ErrorCodes.CANCEL)),))
    recorded = scenario_to_json(scenario)
    assert '"error_code": 8' in recorded

    replayed = scenario_from_json(recorded)
    assert scenario_to_json(replayed) == recorded


def test_a_window_update_record_carries_the_frames_payload():
    """One spelling: the record's increment is the octets the frame sends."""
    record, _ = ARMS['WindowUpdate']
    frame = _frame_from_dict(record)
    assert frame.window_size == record['window_size_increment']
    assert int.from_bytes(frame.payload, 'big') == record['window_size_increment']
