"""Unit tests for the MQTT tap layer (Sprint 54).

Covers the ``{name}`` capture compilation/binding, the shared ``run_taps``
dispatch path, and the decoupled bounded :class:`TapActor` (drop-newest).
"""
import asyncio
import contextlib

import pytest

from blackbull.mqtt import Message, TapActor
from blackbull.mqtt.tap import Tap, compile_tap, compile_taps, run_taps

asyncio_test = pytest.mark.asyncio


def _msg(topic, payload=b'x'):
    return Message(topic=topic, payload=payload)


# -- {name} capture compilation / binding -----------------------------------

def test_compile_plain_filter_has_no_captures():
    tap = compile_tap('sensors/+/temperature', lambda m: None)
    assert tap.match_filter == 'sensors/+/temperature'
    assert tap.captures == ()


def test_compile_named_segment_becomes_plus_and_records_name():
    tap = compile_tap('sensors/{room}/temperature', lambda m: None)
    assert tap.match_filter == 'sensors/+/temperature'
    assert tap.captures == ((1, 'room'),)


def test_bind_returns_captured_values_on_match():
    tap = compile_tap('sensors/{room}/{metric}', lambda m: None)
    assert tap.bind('sensors/kitchen/humidity') == {'room': 'kitchen',
                                                     'metric': 'humidity'}


def test_bind_returns_none_on_mismatch():
    tap = compile_tap('sensors/{room}/temperature', lambda m: None)
    assert tap.bind('other/kitchen/temperature') is None


def test_bind_empty_dict_when_no_captures():
    tap = compile_tap('sensors/#', lambda m: None)
    assert tap.bind('sensors/a/b') == {}


def test_compile_taps_accepts_tuples_and_tap_objects():
    async def cb(m):
        return None

    pre = compile_tap('a/{x}', cb)
    taps = compile_taps([('b/+', cb), pre])
    assert all(isinstance(t, Tap) for t in taps)
    assert taps[1] is pre


# -- run_taps ---------------------------------------------------------------

@asyncio_test
async def test_run_taps_injects_captures_as_kwargs():
    seen = {}

    async def handler(msg, room, metric):
        seen['args'] = (msg.topic, room, metric)

    await run_taps([compile_tap('sensors/{room}/{metric}', handler)],
                   _msg('sensors/bed/co2'))
    assert seen['args'] == ('sensors/bed/co2', 'bed', 'co2')


@asyncio_test
async def test_run_taps_calls_captureless_handler_with_message_only():
    seen = []

    async def handler(msg):
        seen.append(msg.topic)

    await run_taps([compile_tap('sensors/#', handler)], _msg('sensors/a'))
    assert seen == ['sensors/a']


@asyncio_test
async def test_run_taps_skips_non_matching_and_isolates_exceptions():
    hits = []

    async def boom(msg):
        raise RuntimeError('tap blew up')

    async def ok(msg):
        hits.append(msg.topic)

    await run_taps([compile_tap('a/#', boom), compile_tap('sensors/#', ok)],
                   _msg('sensors/temp'))
    assert hits == ['sensors/temp']  # boom did not match; ok ran despite isolation


# -- TapActor (decoupled, bounded, drop-newest) -----------------------------

@contextlib.asynccontextmanager
async def _running(actor):
    task = asyncio.create_task(actor.run())
    try:
        yield actor
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(task)


@asyncio_test
async def test_tap_actor_dispatches_offered_messages():
    seen = []

    async def handler(msg):
        seen.append(msg.topic)

    async with _running(TapActor([('sensors/#', handler)])) as tap:
        tap.offer(_msg('sensors/a/temp'))
        tap.offer(_msg('other/topic'))   # filtered out by run_taps
        for _ in range(20):
            await asyncio.sleep(0)
            if seen:
                break
    assert seen == ['sensors/a/temp']
    assert tap.dropped == 0


@asyncio_test
async def test_tap_actor_drops_newest_on_overflow_and_counts():
    async def handler(msg):
        return None

    # No consumer task running, so the bounded inbox fills and overflows.
    tap = TapActor([('#', handler)], queue_size=2)
    for i in range(5):
        tap.offer(_msg(f'sensors/{i}'))
    assert tap._inbox.qsize() == 2   # first two accepted
    assert tap.dropped == 3          # remaining three dropped (newest)
