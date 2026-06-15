"""Unit tests for the cap-hit observability helper.

Covers the helper module in isolation:

- ``log_cap_hit`` emits one ``WARNING`` record on the
  ``blackbull.caps`` logger with the declared structured fields.
- Without a counter every call emits.
- With a counter the first call per ``(counter, cap)`` emits; later
  calls are silent and increment the suppression tally.
- ``CapHitCounter.flush`` emits one summary per suppressed cap.
- Records honour ``logger.setLevel`` (ERROR silences fully).

Per-site cap-rejection tests live alongside their owning module.
"""
import logging

import pytest

from blackbull.server.cap_log import log_cap_hit, CapHitCounter


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def cap_log_caplog(caplog):
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    return caplog


# ----------------------------------------------------------------------
# log_cap_hit — bare emission
# ----------------------------------------------------------------------

def test_log_cap_hit_emits_one_warning_record(cap_log_caplog):
    log_cap_hit('ws_max_frame_payload', requested=4_194_304, limit=1_048_576,
                peer=('127.0.0.1', 54321), scope_path='/ws', protocol='ws')

    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert len(records) == 1
    r = records[0]
    assert r.levelno == logging.WARNING
    assert r.cap == 'ws_max_frame_payload'
    assert r.requested == 4_194_304
    assert r.limit == 1_048_576
    assert r.peer == ('127.0.0.1', 54321)
    assert r.scope_path == '/ws'
    assert r.protocol == 'ws'


def test_log_cap_hit_message_names_the_cap(cap_log_caplog):
    log_cap_hit('header_max_line', requested=10_000, limit=8_192)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert 'header_max_line' in records[0].getMessage()


def test_log_cap_hit_no_counter_logs_every_call(cap_log_caplog):
    for _ in range(5):
        log_cap_hit('header_max_total', requested=100_000, limit=65_536)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert len(records) == 5


def test_log_cap_hit_silenced_when_logger_disabled(cap_log_caplog):
    cap_log_caplog.set_level(logging.ERROR, logger='blackbull.caps')
    log_cap_hit('compression_max_inflight', requested=5, limit=4)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert records == []


# ----------------------------------------------------------------------
# CapHitCounter — first-hit-then-summary rate-limiter
# ----------------------------------------------------------------------

def test_counter_first_hit_logs_subsequent_silent(cap_log_caplog):
    counter = CapHitCounter()
    log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=counter)
    log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=counter)
    log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=counter)

    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert len(records) == 1
    assert records[0].cap == 'ws_max_frame_payload'


def test_counter_different_caps_each_log_once(cap_log_caplog):
    counter = CapHitCounter()
    log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=counter)
    log_cap_hit('header_max_line', 10_000, 8_192, counter=counter)
    log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=counter)

    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert len(records) == 2
    caps = {r.cap for r in records}
    assert caps == {'ws_max_frame_payload', 'header_max_line'}


def test_counter_flush_emits_summary_per_suppressed_cap(cap_log_caplog):
    counter = CapHitCounter()
    for _ in range(100):
        log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=counter)
    cap_log_caplog.clear()  # discard the first-hit emission

    counter.flush(peer=('127.0.0.1', 54321), protocol='ws')

    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert len(records) == 1
    r = records[0]
    assert r.cap == 'ws_max_frame_payload'
    assert r.suppressed == 99   # 100 calls = 1 first-hit + 99 suppressed
    assert r.peer == ('127.0.0.1', 54321)
    assert r.protocol == 'ws'


def test_counter_flush_omits_caps_with_no_suppression(cap_log_caplog):
    counter = CapHitCounter()
    # Cap hit exactly once — first-hit already named it, no summary needed.
    log_cap_hit('header_max_line', 10_000, 8_192, counter=counter)
    cap_log_caplog.clear()

    counter.flush()

    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert records == []


def test_counter_flush_clears_state(cap_log_caplog):
    counter = CapHitCounter()
    for _ in range(3):
        log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=counter)
    counter.flush()
    cap_log_caplog.clear()

    # After flush, counter is fresh — next hit logs as first-hit again.
    log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=counter)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert len(records) == 1


def test_counter_independent_across_instances(cap_log_caplog):
    a = CapHitCounter()
    b = CapHitCounter()
    log_cap_hit('header_max_line', 10_000, 8_192, counter=a)
    log_cap_hit('header_max_line', 10_000, 8_192, counter=b)
    # Both first-hit records emit — distinct counters.
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert len(records) == 2


def test_counter_flush_idempotent(cap_log_caplog):
    counter = CapHitCounter()
    for _ in range(5):
        log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=counter)
    cap_log_caplog.clear()
    counter.flush()
    counter.flush()
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert len(records) == 1   # second flush is a no-op


# ----------------------------------------------------------------------
# CapHitCounter.bind() — contextvar binding
# ----------------------------------------------------------------------

def test_bind_makes_counter_ambient(cap_log_caplog):
    counter = CapHitCounter()
    with counter.bind():
        # No explicit counter= kwarg — picks up from contextvar.
        log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576)
        log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    # First call emits; second is suppressed because the same counter
    # is active for both.
    assert len(records) == 1


def test_bind_unbinds_on_exit(cap_log_caplog):
    counter = CapHitCounter()
    with counter.bind():
        log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576)
    cap_log_caplog.clear()

    # After bind() exits, a hit without a counter logs every time.
    log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576)
    log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert len(records) == 2


def test_explicit_counter_overrides_ambient(cap_log_caplog):
    ambient = CapHitCounter()
    override = CapHitCounter()
    with ambient.bind():
        # Override wins — first hit on override emits; ambient stays untouched.
        log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=override)
        log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=override)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert len(records) == 1
    # Ambient counter was never touched — flush emits nothing.
    cap_log_caplog.clear()
    ambient.flush()
    assert [r for r in cap_log_caplog.records if r.name == 'blackbull.caps'] == []


def test_bind_inherited_by_child_task(cap_log_caplog):
    import asyncio
    counter = CapHitCounter()

    async def child():
        log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576)
        log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576)

    async def parent():
        with counter.bind():
            async with asyncio.TaskGroup() as tg:
                tg.create_task(child())

    asyncio.run(parent())
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    # Child task inherits parent's context — both calls go through the
    # same counter, so only the first emits.
    assert len(records) == 1
