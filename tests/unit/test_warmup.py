"""Tests for the pre-fork warm-up hook (``@app.on_warmup`` + core warm-up).

Warm-up runs an app's registered hooks once, in the master, before it binds a
socket or forks workers, so every worker inherits the warmed heap via COW.  The
mechanism lives in :mod:`blackbull.server.warmup`; the public API is
:meth:`BlackBull.on_warmup` and :meth:`BlackBull.warm_request`.
"""
from __future__ import annotations

import asyncio
import gc
import pathlib
import ssl
import time

import pytest

from blackbull import BlackBull, Connection, Headers
from blackbull.server import warmup as warmup_mod
from blackbull.server.warmup import run_warmup, warmup_inline, warm_tls


@pytest.fixture(autouse=True)
def _thaw_after():
    """Warm-up calls ``gc.freeze()``; un-freeze so it can't leak into other tests."""
    yield
    gc.unfreeze()


def _server_ssl_context() -> ssl.SSLContext:
    cert = pathlib.Path(__file__).parent.parent / 'cert.pem'
    key = pathlib.Path(__file__).parent.parent / 'key.pem'
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.set_alpn_protocols(['h2', 'http/1.1'])
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_on_warmup_registers_hook():
    app = BlackBull()

    @app.on_warmup
    async def warm(_app):
        pass

    assert app._warmup_hooks == [warm]
    # decorator returns the function unchanged
    assert warm.__name__ == 'warm'


def test_no_hooks_is_noop_and_creates_no_loop(monkeypatch):
    app = BlackBull()
    # If run_warmup tried to build a loop with no hooks, this would raise.
    monkeypatch.setattr(asyncio, 'new_event_loop',
                        lambda: pytest.fail('should not create a loop'))
    run_warmup(app, None)  # no hooks → immediate return


# ---------------------------------------------------------------------------
# run_warmup — sync, pre-fork entry (temporary loop)
# ---------------------------------------------------------------------------

def test_run_warmup_runs_each_hook_once():
    app = BlackBull()
    calls: list[str] = []

    @app.on_warmup
    async def a(_app):
        calls.append('a')

    @app.on_warmup
    async def b(_app):
        calls.append('b')

    run_warmup(app, None)
    assert calls == ['a', 'b']  # once each, registration order


def test_run_warmup_passes_app_to_hook():
    app = BlackBull()
    seen: list = []

    @app.on_warmup
    async def warm(a):
        seen.append(a)

    run_warmup(app, None)
    assert seen == [app]


def test_run_warmup_closes_temp_loop_and_leaves_no_current_loop():
    app = BlackBull()
    captured: list = []

    @app.on_warmup
    async def warm(_app):
        captured.append(asyncio.get_running_loop())

    run_warmup(app, None)
    # The loop the hook ran on must be closed (not inheritable across fork).
    assert captured and captured[0].is_closed()


def test_run_warmup_freezes_gc():
    app = BlackBull()

    @app.on_warmup
    async def warm(_app):
        pass

    gc.unfreeze()
    assert gc.get_freeze_count() == 0
    run_warmup(app, None)
    assert gc.get_freeze_count() > 0


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warmup_hook_exception_is_swallowed_and_others_run():
    app = BlackBull()
    calls: list[str] = []

    @app.on_warmup
    async def boom(_app):
        calls.append('boom')
        raise RuntimeError('warm-up blew up')

    @app.on_warmup
    async def after(_app):
        calls.append('after')

    # Must not raise; the second hook still runs.
    await warmup_inline(app, None)
    assert calls == ['boom', 'after']


@pytest.mark.asyncio
async def test_warmup_respects_budget(monkeypatch):
    monkeypatch.setenv('BB_WARMUP_BUDGET_S', '0.05')
    app = BlackBull()
    finished = False

    @app.on_warmup
    async def slow(_app):
        nonlocal finished
        await asyncio.sleep(5)
        finished = True

    t0 = time.monotonic()
    await warmup_inline(app, None)  # must not hang for 5s
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0
    assert finished is False  # cancelled at the budget


# ---------------------------------------------------------------------------
# warm_request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warm_request_invokes_app_n_times():
    app = BlackBull()
    hits = {'n': 0, 'bodies': []}

    @app.route(path='/warm', methods=['POST'])
    async def warm(body: bytes):
        hits['n'] += 1
        hits['bodies'].append(body)
        return b'ok'

    conn = Connection(method='POST', path='/warm', raw_path=b'/warm',
                      headers=Headers([(b'content-type', b'application/octet-stream')]))
    await app.warm_request(conn, body=b'payload', n=5)

    assert hits['n'] == 5
    assert hits['bodies'] == [b'payload'] * 5


@pytest.mark.asyncio
async def test_warm_request_from_warmup_hook():
    app = BlackBull()
    count = {'n': 0}

    @app.route(path='/ping')
    async def ping():
        count['n'] += 1
        return 'pong'

    @app.on_warmup
    async def warm(a):
        conn = Connection(method='GET', path='/ping', raw_path=b'/ping',
                          headers=Headers([]))
        await a.warm_request(conn, n=3)

    await warmup_inline(app, None)
    assert count['n'] == 3


# ---------------------------------------------------------------------------
# warm_tls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warm_tls_completes_handshakes():
    ctx = _server_ssl_context()
    # Should complete N in-memory handshakes with no socket and no error.
    await warm_tls(ctx, n=8)


@pytest.mark.asyncio
async def test_warm_tls_none_context_is_noop():
    await warm_tls(None, n=8)  # no context → nothing to do


@pytest.mark.asyncio
async def test_warmup_inline_calls_warm_tls_when_context_present(monkeypatch):
    app = BlackBull()

    @app.on_warmup
    async def warm(_app):
        pass

    seen = {'n': None}

    async def fake_warm_tls(ctx, *, n):
        seen['n'] = n

    monkeypatch.setattr(warmup_mod, 'warm_tls', fake_warm_tls)
    ctx = _server_ssl_context()
    await warmup_inline(app, ctx)
    assert seen['n'] is not None and seen['n'] > 0


@pytest.mark.asyncio
async def test_warmup_inline_skips_warm_tls_without_context(monkeypatch):
    app = BlackBull()

    @app.on_warmup
    async def warm(_app):
        pass

    called = {'v': False}

    async def fake_warm_tls(ctx, *, n):
        called['v'] = True

    monkeypatch.setattr(warmup_mod, 'warm_tls', fake_warm_tls)
    await warmup_inline(app, None)
    assert called['v'] is False
