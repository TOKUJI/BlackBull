"""Best-effort warm-up before binding and fork.

Never leave a live socket, task or temporary event loop for workers to inherit.
Failures log and fall back to cold startup; the wall-clock budget must bound boot.
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
    """Run best-effort warmup hooks before fork in a temporary, closed event loop.

    Single-worker startup runs hooks on the serving loop instead.
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
    """Run best-effort hooks on the serving loop; log hook failures without preventing startup.
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
    """Warm TLS using in-memory SSL BIOs, without binding a listener.
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
            # Client needs more bytes from the server; server.do_handshake()
            # below (and the next round) writes them into the shared MemoryBIO.
            pass
        try:
            server.do_handshake()
            s_done = True
        except ssl.SSLWantReadError:
            # Server needs more bytes from the client; produced above / next round.
            pass
        if c_done and s_done:
            return
    # Incomplete within the round budget — warm-up is best-effort, so ignore.
