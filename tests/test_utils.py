import asyncio
import pytest
import logging
import socket

from blackbull.utils import Scheme, check_port, pop_safe, serializable, parse_post_data, EventEmitter
from blackbull.router import Router

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# check_port
# ---------------------------------------------------------------------------

def test_check_port_free():
    # Bind to an ephemeral port, record it, close, then verify check_port says True (free).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('localhost', 0))
        port = s.getsockname()[1]
    assert check_port('localhost', port) is True


def test_check_port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('localhost', 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert check_port('localhost', port) is False


# ---------------------------------------------------------------------------
# pop_safe
# ---------------------------------------------------------------------------

def test_pop_safe_moves_key():
    src = {'a': 1, 'b': 2}
    dst = {}
    pop_safe('a', src, dst)
    assert dst == {'a': 1}
    assert 'a' not in src


def test_pop_safe_with_new_key():
    src = {'old': 42}
    dst = {}
    pop_safe('old', src, dst, new_key='new')
    assert dst == {'new': 42}
    assert 'old' not in src


def test_pop_safe_missing_key_is_noop():
    src = {'x': 1}
    dst = {}
    pop_safe('missing', src, dst)
    assert dst == {}
    assert src == {'x': 1}


# ---------------------------------------------------------------------------
# serializable
# ---------------------------------------------------------------------------

def test_serializable_is_empty_returns_not_implemented():
    s = serializable()
    result = s.is_empty()
    assert isinstance(result, NotImplementedError)


def test_serializable_save_returns_not_implemented():
    s = serializable()
    result = s.save()
    assert isinstance(result, NotImplementedError)


def test_serializable_load_returns_not_implemented():
    result = serializable.load('anything')
    assert isinstance(result, NotImplementedError)


# ---------------------------------------------------------------------------
# parse_post_data
# ---------------------------------------------------------------------------

def test_parse_post_data_simple():
    result = parse_post_data('name=John&age=30')
    assert result == {'name': 'John', 'age': '30'}


def test_parse_post_data_single():
    result = parse_post_data('key=value')
    assert result == {'key': 'value'}


# ---------------------------------------------------------------------------
# EventEmitter
# ---------------------------------------------------------------------------

def test_event_emitter_on_registers_listener():
    emitter = EventEmitter()
    called = []
    emitter.on('ev', lambda: called.append(1))
    assert len(emitter._listeners['ev']) == 1


def test_event_emitter_once_registers_listener():
    emitter = EventEmitter()
    emitter.once('ev', lambda: None)
    assert len(emitter._listeners_once['ev']) == 1


def test_event_emitter_off_removes_listener():
    emitter = EventEmitter()
    fn = lambda: None
    emitter.on('ev', fn)
    emitter.on('ev', fn)
    emitter.off('ev', fn)
    assert len(emitter._listeners['ev']) == 1


# @pytest.mark.asyncio
# async def test_EventEmitter_on():

#     called = False

#     def is_a_function():
#         nonlocal called
#         called = True

#     is_a_function()

#     assert called is True, "is_a_function has not called."

#     called = False

#     assert called is False, "Failed to reset a variable."

#     event1 = asyncio.Event()
#     event2 = asyncio.Event()

#     emitter = EventEmitter()

#     emitter.on(event1, is_a_function)
#     assert called is False, "The listener is called before the emition of the event."

#     emitter.emit(event2)
#     assert called is False, "The listener is called before the emition of the event."

#     emitter.emit(event1)
#     await asyncio.sleep(0)  # Waits to run a function
#     assert called is True, "The listener is not called after the emition of the event."


# @pytest.mark.asyncio
# async def test_EventEmitter_twice():

#     count = 0

#     def add1():
#         nonlocal count
#         count = count + 1

#     event = asyncio.Event()

#     emitter = EventEmitter()

#     emitter.on(event, add1)
#     emitter.on(event, add1)

#     emitter.emit(event)
#     await asyncio.sleep(0)  # Waits to run a function
#     assert count == 2, "The listener is not called after the emition of the event."


# @pytest.mark.asyncio
# async def test_EventEmitter_parameter():

#     count = 0

#     def add1(n):
#         nonlocal count
#         count = count + n

#     event = asyncio.Event()

#     emitter = EventEmitter()

#     emitter.on(event, add1)

#     emitter.emit(event, 2)
#     await asyncio.sleep(0)  # Waits to run a function
#     assert count == 2, "The listener is not called after the emition of the event."


# @pytest.mark.asyncio
# async def test_EventEmitter_off():

#     count = 0

#     def add1():
#         nonlocal count
#         count = count + 1

#     event = asyncio.Event()

#     emitter = EventEmitter()

#     emitter.on(event, add1)
#     emitter.on(event, add1)
#     emitter.off(event, add1)

#     emitter.emit(event)
#     await asyncio.sleep(0)  # Waits to run a function
#     assert count == 1, "The listener is not called after the emition of the event."
