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

from blackbull.server.cap_log import (
    log_cap_hit, CapHitCounter, _LazyCapHitCounter,
)


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
                peer=('127.0.0.1', 54321), scope_path='/ws', protocol='ws',
                connection_id='abc12345')

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
    assert r.connection_id == 'abc12345'


def test_log_cap_hit_connection_id_inherits_from_counter(cap_log_caplog):
    counter = CapHitCounter()
    log_cap_hit('ws_max_frame_payload', 1, 2, counter=counter)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    # Counter auto-generates an id; emission inherits it.
    assert records[0].connection_id == counter.connection_id


def test_log_cap_hit_no_counter_no_explicit_id_is_none(cap_log_caplog):
    log_cap_hit('header_max_line', 10_000, 8_192)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert records[0].connection_id is None


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
    # Disable threshold so all 99 post-first hits stay queued for flush.
    counter = CapHitCounter(flush_threshold=0)
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
    # connection_id must match the counter's stored id so first-hit /
    # intermediate / graceful records all correlate downstream.
    assert r.connection_id == counter.connection_id


def test_counter_auto_generates_unique_connection_ids():
    a = CapHitCounter()
    b = CapHitCounter()
    assert a.connection_id != b.connection_id
    assert isinstance(a.connection_id, str)
    # _gen_connection_id delegates to conn_id.new_connection_id:
    # 12-hex process prefix + 8-hex sequence (collision-free in-process;
    # the old 4-byte urandom form collided at churn scale).
    assert len(a.connection_id) == 20
    assert all(c in '0123456789abcdef' for c in a.connection_id)


def test_counter_accepts_explicit_connection_id():
    counter = CapHitCounter(connection_id='peer-7-conn-3')
    assert counter.connection_id == 'peer-7-conn-3'


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
            # Cancel any pending dirty-flush timer before the loop closes
            # so its eventual cancellation doesn't race with loop teardown.
            counter.flush()

    asyncio.run(parent())
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    # Child task inherits parent's context — both calls go through the
    # same counter, so only the first emits.  Flush after the child
    # runs records the 1 suppressed hit as a summary.
    assert len([r for r in records if not getattr(r, 'suppressed', None)]) == 1


# ----------------------------------------------------------------------
# Dirty-flush triggers (Fix 2 — RST resilience)
# ----------------------------------------------------------------------

def test_dirty_flush_threshold_emits_intermediate_summary(cap_log_caplog):
    counter = CapHitCounter(flush_threshold=10, flush_interval=0)
    for _ in range(11):
        log_cap_hit('ws_max_frame_payload', 4_000_000, 1_048_576, counter=counter)

    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    # 1 first-hit + 1 intermediate summary at the threshold boundary.
    assert len(records) == 2
    first, summary = records
    assert getattr(first, 'suppressed', None) is None      # first-hit record
    assert summary.suppressed == 10                        # 10 suppressed hits
    assert summary.cap == 'ws_max_frame_payload'
    assert 'connection still open' in summary.getMessage()
    assert summary.connection_id == counter.connection_id


def test_dirty_flush_threshold_resets_and_resumes(cap_log_caplog):
    counter = CapHitCounter(flush_threshold=5, flush_interval=0)
    for _ in range(11):    # 1 first-hit + 10 suppressed -> 2 thresholds fire (at 5 and 10)
        log_cap_hit('ws_max_frame_payload', 1, 2, counter=counter)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    # 1 first-hit + 2 intermediate summaries (at the 5th and 10th
    # suppressed hits).  Each summary's suppressed == 5.
    summaries = [r for r in records if 'still open' in r.getMessage()]
    assert len(summaries) == 2
    assert all(s.suppressed == 5 for s in summaries)


@pytest.mark.asyncio
async def test_dirty_flush_interval_emits_after_interval(cap_log_caplog):
    import asyncio as _asyncio
    counter = CapHitCounter(flush_threshold=0, flush_interval=0.05)
    for _ in range(3):
        log_cap_hit('ws_max_frame_payload', 1, 2, counter=counter)
    # Now wait past the interval so the background timer fires.
    await _asyncio.sleep(0.15)

    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    summaries = [r for r in records if 'still open' in r.getMessage()]
    assert len(summaries) == 1
    assert summaries[0].suppressed == 2
    assert summaries[0].connection_id == counter.connection_id


def test_dirty_flush_disabled_triggers(cap_log_caplog):
    counter = CapHitCounter(flush_threshold=0, flush_interval=0)
    for _ in range(200):
        log_cap_hit('ws_max_frame_payload', 1, 2, counter=counter)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    # Exactly one first-hit record; no intermediate summaries.
    assert len(records) == 1
    assert getattr(records[0], 'suppressed', None) is None

    counter.flush()
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    # Now the graceful flush summary fires with the full count.
    summaries = [r for r in records if getattr(r, 'suppressed', None) is not None]
    assert len(summaries) == 1
    assert summaries[0].suppressed == 199


@pytest.mark.asyncio
async def test_dirty_flush_threshold_cancels_interval_timer(cap_log_caplog):
    import asyncio as _asyncio
    # Threshold low, interval long enough we'd see it if it weren't cancelled.
    counter = CapHitCounter(flush_threshold=5, flush_interval=0.1)
    for _ in range(6):    # 1 first-hit + 5 suppressed -> threshold fires
        log_cap_hit('ws_max_frame_payload', 1, 2, counter=counter)

    # Threshold fired and reset counts.  Sleep past the interval — no
    # second summary should fire because nothing is pending and the
    # timer was cancelled.
    await _asyncio.sleep(0.2)

    summaries = [
        r for r in cap_log_caplog.records
        if r.name == 'blackbull.caps' and 'still open' in r.getMessage()
    ]
    assert len(summaries) == 1


@pytest.mark.asyncio
async def test_flush_cancels_pending_interval_timer(cap_log_caplog):
    import asyncio as _asyncio
    counter = CapHitCounter(flush_threshold=0, flush_interval=0.1)
    log_cap_hit('ws_max_frame_payload', 1, 2, counter=counter)
    log_cap_hit('ws_max_frame_payload', 1, 2, counter=counter)   # timer arms
    counter.flush()
    cap_log_caplog.clear()

    # Sleep past the original interval — the cancelled timer must not fire.
    await _asyncio.sleep(0.2)
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert records == []


# ----------------------------------------------------------------------
# _LazyCapHitCounter — defer construction (and os.urandom id) to first hit
# ----------------------------------------------------------------------

def test_lazy_holder_starts_unmaterialised():
    holder = _LazyCapHitCounter()
    assert holder.counter is None


def test_lazy_holder_no_urandom_until_first_hit(monkeypatch, cap_log_caplog):
    """A healthy (no-cap) connection draws no os.urandom connection id."""
    import blackbull.server.cap_log as cap_log
    calls = 0
    real = cap_log._gen_connection_id

    def counted():
        nonlocal calls
        calls += 1
        return real()

    monkeypatch.setattr(cap_log, '_gen_connection_id', counted)

    holder = _LazyCapHitCounter()
    with holder.bind():
        pass                      # no log_cap_hit → no cap fired
    holder.flush(peer=('127.0.0.1', 1234))

    assert calls == 0
    assert holder.counter is None


def test_lazy_holder_materialises_on_first_hit(cap_log_caplog):
    holder = _LazyCapHitCounter()
    with holder.bind():
        log_cap_hit('ws_max_frame_payload', 1, 2)
    assert holder.counter is not None
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert len(records) == 1
    # The materialised counter minted a connection id for the emitted record.
    assert records[0].connection_id == holder.counter.connection_id


def test_lazy_holder_flush_is_noop_when_unmaterialised(cap_log_caplog):
    holder = _LazyCapHitCounter()
    holder.flush(peer=('127.0.0.1', 1234))   # must not raise
    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    assert records == []


def test_lazy_holder_rate_limits_and_summarises_like_eager(cap_log_caplog):
    """First hit emits; later hits suppress; flush summarises — same as eager."""
    holder = _LazyCapHitCounter(flush_threshold=0, flush_interval=0)
    with holder.bind():
        for _ in range(4):       # 1 first-hit + 3 suppressed
            log_cap_hit('ws_max_frame_payload', 1, 2)
    holder.flush(peer=('127.0.0.1', 1234))

    records = [r for r in cap_log_caplog.records if r.name == 'blackbull.caps']
    first = [r for r in records if getattr(r, 'suppressed', None) is None]
    summaries = [r for r in records if getattr(r, 'suppressed', None) is not None]
    assert len(first) == 1
    assert len(summaries) == 1
    assert summaries[0].suppressed == 3


def test_lazy_holder_shared_across_tasks(cap_log_caplog):
    """A child task hitting the cap materialises the holder the parent flushes."""
    import asyncio as _asyncio

    async def main():
        holder = _LazyCapHitCounter()
        with holder.bind():
            async with _asyncio.TaskGroup() as tg:
                async def child():
                    log_cap_hit('ws_max_frame_payload', 1, 2)
                tg.create_task(child())
            # Parent sees the counter the child materialised.
            assert holder.counter is not None
        holder.flush(peer=('127.0.0.1', 1234))

    _asyncio.run(main())
