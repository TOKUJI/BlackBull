import pytest

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import FrameTypes


@pytest.mark.parametrize('tail', [b'', b'\xff', b'\xff\xff\x00\x00\x00'])
def test_settings_preserve_entries_and_wire_bytes(tail):
    entries = [(6, 65536), (0xffff, 123), (6, 4096), (2, 0), (4, 65535)]
    payload = b''.join(k.to_bytes(2, 'big') + v.to_bytes(4, 'big')
                       for k, v in entries) + tail
    frame = FrameFactory().create(FrameTypes.SETTINGS, 0, 0, data=payload)
    assert frame.settings == entries
    assert frame.max_header_list_size == 4096
    assert frame.enable_push == 0
    assert frame.initial_window_size == 65535
    assert frame.payload == payload
    assert frame.save() == (len(payload).to_bytes(3, 'big')
                            + b'\x04\x00\x00\x00\x00\x00' + payload)
    returned = frame.settings
    returned.clear()
    assert frame.settings == entries
    assert frame.payload == payload


@pytest.mark.parametrize('kwargs,entries', [
    ({}, []),
    ({'ack': True, 'max_header_list_size': 123}, []),
    ({'max_header_list_size': 0xffffffff, 'enable_push': 0},
     [(2, 0), (6, 0xffffffff)]),
])
def test_settings_built_and_received_have_the_same_fields(kwargs, entries):
    factory = FrameFactory()
    outgoing = factory.settings(**kwargs)
    incoming = factory.load(outgoing.save())
    assert outgoing.settings == incoming.settings == entries
    assert outgoing.payload == incoming.payload
    assert outgoing.save() == incoming.save()
