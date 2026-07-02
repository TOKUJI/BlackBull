"""Pre-fork warm-up — run an app's warm-up hooks *once* in the master before it
binds a listening socket or forks workers, so every worker is born warm.

Motivation
----------
BlackBull runs one asyncio event loop per worker; that loop services both
``accept()`` and request processing.  A **cold** CPU-bound coroutine (a fresh
gRPC codec encoding thousands of streams, or a burst of cold TLS handshakes)
can hold the loop long enough that the ``accept`` reader callback never runs,
the kernel listen backlog overflows, and a colocated load generator's redial
storm turns a cold-start transient into an ``ECONNREFUSED`` collapse.  The
empirical tell is that the *second* run of the same process is always clean —
warmth, not structure, is the differentiator.

The fix is to reach that warmth *before the listening socket exists*.  Warm-up
registered via :meth:`BlackBull.on_warmup` runs here, once, in the master:

* **Before bind + fork** (multi-worker): forked workers inherit the warmed heap
  via copy-on-write.  PEP 659's adaptive specialization lives in the code
  objects on the heap, so it survives ``fork()``; ``gc.collect()`` +
  ``gc.freeze()`` keep the warmed pages COW-shared instead of being dirtied by
  the first post-fork collection.
* **Before serving** (single-worker): the one process is warm before it binds.

Safety
------
Warm-up is best-effort and must never crash the master: every failure is logged
and swallowed, degrading to today's cold start.  It uses only in-process ASGI
drives and in-memory (:class:`ssl.MemoryBIO`) handshakes — it never creates a
socket, a live connection, or a lingering event loop that a subsequent
``fork()`` could inherit (the classic ``preload_app`` hazard).  The temporary
loop used by :func:`run_warmup` is closed before it returns.
"""
import asyncio
import gc
import logging
import os
import time

logger = logging.getLogger(__name__)

# Wall-clock upper bound so a pathological hook can never stall boot.  Hooks
# self-limit their own volume; this is only a safety cap.
_DEFAULT_BUDGET_S = 60.0
# In-memory TLS handshakes to prime the OpenSSL / RSA / ALPN path when a TLS
# context is present (protocol-agnostic transport warm-up).
_DEFAULT_TLS_N = 64


def _budget_s() -> float:
    raw = os.environ.get('BB_WARMUP_BUDGET_S')
    if raw is None:
        return _DEFAULT_BUDGET_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_BUDGET_S


def _tls_n() -> int:
    raw = os.environ.get('BB_WARMUP_TLS_N')
    if raw is None:
        return _DEFAULT_TLS_N
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_TLS_N


def _name(fn) -> str:
    return getattr(fn, '__name__', repr(fn))


def run_warmup(app, ssl_context=None) -> None:
    """Synchronous pre-fork entry point (called from ``serve`` before bind/fork).

    No-op when *app* registered no :meth:`BlackBull.on_warmup` hooks.  Otherwise
    runs the hooks (plus the built-in TLS warm-up when *ssl_context* is set) in a
    **temporary** event loop that is closed before returning, so a subsequent
    ``fork()`` never inherits a live loop.  Ends with ``gc.collect()`` +
    ``gc.freeze()`` to keep the warmed pages copy-on-write-shared across fork.
    """
    hooks = getattr(app, '_warmup_hooks', None)
    if not hooks:
        return
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(warmup_inline(app, ssl_context))
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


async def warmup_inline(app, ssl_context=None) -> None:
    """Async warm-up used by the single-worker path (runs on the serving loop).

    Same behaviour as :func:`run_warmup` minus the temporary-loop management:
    the single-worker process never forks, so warming on the loop that will go
    on to serve is both correct and cheaper.  Never raises.
    """
    hooks = getattr(app, '_warmup_hooks', None)
    if not hooks:
        return
    budget = _budget_s()
    if budget <= 0:
        return
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(_run_hooks(app, ssl_context, hooks), budget)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning('warm-up hit the %.0fs budget; proceeding to bind', budget)
    except Exception as exc:  # pragma: no cover - defensive; hooks catch their own
        logger.warning('warm-up failed (continuing cold): %r', exc)
    _freeze()
    logger.info('warm-up complete (%d hook(s), %.2fs)',
                len(hooks), time.monotonic() - t0)


async def _run_hooks(app, ssl_context, hooks) -> None:
    for hook in hooks:
        try:
            await hook(app)
        except Exception:
            logger.warning('warm-up hook %s failed (skipping)', _name(hook),
                           exc_info=True)
    # Built-in transport warm-up: prime the TLS handshake path.  Protocol-
    # agnostic (it warms OpenSSL/RSA/ALPN, not any application protocol) and
    # only runs when the listener will terminate TLS.
    if ssl_context is not None:
        try:
            await warm_tls(ssl_context, n=_tls_n())
        except Exception:  # pragma: no cover - defensive
            logger.warning('TLS warm-up failed (skipping)', exc_info=True)


def _freeze() -> None:
    gc.collect()
    try:
        gc.freeze()
    except Exception:  # pragma: no cover - platform without gc.freeze
        pass


async def warm_tls(ssl_context, *, n: int = _DEFAULT_TLS_N) -> None:
    """Prime the TLS handshake path with *n* in-memory handshakes (no socket).

    Drives full handshakes between the server *ssl_context* and a throwaway
    client context over paired :class:`ssl.MemoryBIO` buffers, faulting in the
    OpenSSL / RSA / ALPN code the TLS benchmark profiles hit cold on run-1.
    Self-contained: creates no listener and no file descriptor.
    """
    if ssl_context is None or n <= 0:
        return
    import ssl  # noqa: PLC0415
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE
    try:
        # Match the server's advertised ALPN so negotiation is exercised too.
        client_ctx.set_alpn_protocols(['h2', 'http/1.1'])
    except NotImplementedError:  # pragma: no cover - OpenSSL without ALPN
        pass
    for _ in range(n):
        _handshake_once(ssl_context, client_ctx)
        # Yield so the wall-clock budget (asyncio.wait_for) can pre-empt a long
        # run of handshakes instead of blocking the loop.
        await asyncio.sleep(0)


def _handshake_once(server_ctx, client_ctx) -> None:
    """Complete one client<->server TLS handshake entirely in memory."""
    import ssl  # noqa: PLC0415
    c2s = ssl.MemoryBIO()  # client -> server bytes
    s2c = ssl.MemoryBIO()  # server -> client bytes
    # wrap_bio(incoming, outgoing): client reads from s2c, writes to c2s; the
    # server mirrors it.  Sharing the two BIOs needs no manual pumping.
    client = client_ctx.wrap_bio(s2c, c2s, server_side=False,
                                 server_hostname='localhost')
    server = server_ctx.wrap_bio(c2s, s2c, server_side=True)
    for _ in range(24):  # bounded rounds; a healthy handshake needs a handful
        c_done = s_done = False
        try:
            client.do_handshake()
            c_done = True
        except ssl.SSLWantReadError:
            pass
        try:
            server.do_handshake()
            s_done = True
        except ssl.SSLWantReadError:
            pass
        if c_done and s_done:
            return
    # Incomplete within the round budget — warm-up is best-effort, so ignore.
