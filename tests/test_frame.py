from blackbull.frame import SettingFrame, Priority
from logging import getLogger
logger = getLogger(__name__)
"""
    +-----------------------------------------------+
    |                 Length (24)                   |
    +---------------+---------------+---------------+
    |   Type (8)    |   Flags (8)   |
    +-+-------------+---------------+-------------------------------+
    |R|                 Stream Identifier (31)                      |
    +=+=============================================================+
    |                   Frame Payload (0...)                      ...
    +---------------------------------------------------------------+
"""


def test_settingframe():
    type_ = b'\x04'
    flags = 0
    sid = 0
    payload = b''
    length = len(payload).to_bytes(3, byteorder='big')

    SettingFrame(length, type_, flags, sid, data=payload)
    assert 0 == 1


def test_priorityframe():
    type_ = b'\x02'
    flags = 0
    sid = 0

    exclusive = 0x80000000
    dependencies = (1).to_bytes(4, byteorder='big')
    weight = (100).to_bytes(1, byteorder='big')
    payload = dependencies + weight

    length = len(payload).to_bytes(3, byteorder='big')

    Priority(length, type_, flags, sid, data=payload)

    assert 0 == 1
