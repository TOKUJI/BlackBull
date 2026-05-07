"""Tests for blackbull/server/parser.py — HTTP2ParserBase registration guards."""
import pytest
from unittest.mock import MagicMock

from blackbull.server.parser import HTTP2ParserBase


def test_subclass_frame_type_none_not_registered():
    before = dict(HTTP2ParserBase._registry)

    class AbstractSub(HTTP2ParserBase):
        FRAME_TYPE = None

    assert HTTP2ParserBase._registry == before


def test_subclass_duplicate_frame_type_raises():
    with pytest.raises(ValueError, match='Duplicate FRAME_TYPE'):
        class DupA(HTTP2ParserBase):
            FRAME_TYPE = 'parser_test_sentinel'

        class DupB(HTTP2ParserBase):
            FRAME_TYPE = 'parser_test_sentinel'

    HTTP2ParserBase._registry.pop('parser_test_sentinel', None)


def test_base_parse_not_implemented():
    frame = MagicMock()
    frame.stream_id = 1
    stream = MagicMock()
    parser = HTTP2ParserBase(frame, stream)
    with pytest.raises(NotImplementedError):
        parser.parse()
