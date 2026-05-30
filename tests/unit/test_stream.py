import asyncio
import pytest
import logging

from blackbull.protocol.stream import Stream

logger = logging.getLogger(__name__)


def test_create():
    s = Stream(0)


def test_add_child():
    s = Stream(0)
    s.add_child(1)


def test_get_children():
    s = Stream(0)
    c = s.add_child(1)
    assert s.get_children() == [c]


# ---------------------------------------------------------------------------
# New coverage tests
# ---------------------------------------------------------------------------

def test_window_size_stored():
    s = Stream(1, window_size=65535)
    assert s.window_size == 65535


def test_drop_child():
    root = Stream(0)
    root.add_child(1)
    root.drop_child(1)
    assert 1 not in root.children


def test_find_child_recursive():
    root = Stream(0)
    child = root.add_child(1)
    grandchild = child.add_child(3)
    assert root.find_child(3) is grandchild


def test_update_event_with_data():
    s = Stream(1)
    from unittest.mock import MagicMock
    data = MagicMock()
    data.payload = b'hello'
    event = s.update_event(data)
    assert event['body'] == b'hello'
    assert event['type'] == 'http.request'


def test_update_event_no_data_returns_existing():
    s = Stream(1)
    s.update_event()  # creates event
    event = s.update_event()  # no-op
    assert event['type'] == 'http.request'


def test_update_scope_with_headers():
    """`:path` carries path+query joined by '?' (RFC 9113 §8.3.1); the
    scope must split them so the router sees just the path component
    and `query_string` is bytes per ASGI."""
    from unittest.mock import MagicMock
    from blackbull.protocol.frame_types import PseudoHeaders
    s = Stream(1)
    headers = MagicMock()
    headers.pseudo_headers = {
        PseudoHeaders.METHOD: 'GET',
        PseudoHeaders.PATH: '/foo?bar=1',
        PseudoHeaders.SCHEME: 'https',
    }
    headers.headers = []
    scope = s.update_scope(headers)
    assert scope['method'] == 'GET'
    assert scope['path'] == '/foo'
    assert scope['raw_path'] == b'/foo'
    assert scope['query_string'] == b'bar=1'


def test_update_scope_path_without_query():
    """No '?' in the request-target → empty query_string bytes."""
    from unittest.mock import MagicMock
    from blackbull.protocol.frame_types import PseudoHeaders
    s = Stream(1)
    headers = MagicMock()
    headers.pseudo_headers = {
        PseudoHeaders.METHOD: 'GET',
        PseudoHeaders.PATH: '/json/3',
        PseudoHeaders.SCHEME: 'https',
    }
    headers.headers = []
    scope = s.update_scope(headers)
    assert scope['path'] == '/json/3'
    assert scope['query_string'] == b''


def test_update_scope_path_with_query_routes_correctly():
    """Regression for the Sprint 27 / Task 4 HTTPArena finding:
    `/json/3?m=2.0` must yield path='/json/3' so a router pattern like
    `/json/{count:int}` matches.  Pre-fix, path was '/json/3?m=2.0'
    and the router returned 404."""
    from unittest.mock import MagicMock
    from blackbull.protocol.frame_types import PseudoHeaders
    s = Stream(1)
    headers = MagicMock()
    headers.pseudo_headers = {
        PseudoHeaders.METHOD: 'GET',
        PseudoHeaders.PATH: '/json/3?m=2.0',
        PseudoHeaders.SCHEME: 'https',
    }
    headers.headers = []
    scope = s.update_scope(headers)
    assert scope['path'] == '/json/3'
    assert scope['query_string'] == b'm=2.0'


def test_update_scope_path_bytes_input():
    """HPACK may hand the decoder bytes pseudo-headers; the scope
    builder must still produce ASGI-correct str path + bytes
    query_string."""
    from unittest.mock import MagicMock
    from blackbull.protocol.frame_types import PseudoHeaders
    s = Stream(1)
    headers = MagicMock()
    headers.pseudo_headers = {
        PseudoHeaders.METHOD: 'GET',
        PseudoHeaders.PATH: b'/echo?x=1&y=2',
        PseudoHeaders.SCHEME: 'https',
    }
    headers.headers = []
    scope = s.update_scope(headers)
    assert scope['path'] == '/echo'
    assert isinstance(scope['path'], str)
    assert scope['query_string'] == b'x=1&y=2'
    assert isinstance(scope['query_string'], bytes)


def test_update_scope_no_headers():
    s = Stream(1)
    scope = s.update_scope()
    assert scope['type'] == 'http'


def test_get_lock_returns_condition():
    s = Stream(1)
    lock = s.get_lock()
    assert isinstance(lock, asyncio.Condition)


def test_get_lock_returns_condition_again():
    s = Stream(1)
    lock = s.get_lock()
    assert isinstance(lock, asyncio.Condition)


def test_is_eos_and_flip():
    s = Stream(1)
    assert s.is_eos is False
    s.flip_eos()
    assert s.is_eos is True


def test_is_locked_false_initially():
    s = Stream(1)
    assert s.is_locked is False


def test_close_removes_from_parent():
    root = Stream(0)
    child = root.add_child(1)
    child.close()
    assert 1 not in root.children


def test_repr_nonempty():
    s = Stream(5)
    assert repr(s) != ''
    assert '5' in repr(s)


