"""Seam timing for the Sprint 100 Phase 2 F1/F2/F3 forks.

Three request-path seams, all timed with ``time.thread_time()`` so the sums
are CPU units — matching the harness's CPU-µs/req denominator from ``/proc``
(review: wall-clock resp-half mixed units with the CPU total; thread_time
restores unit consistency):

* ``resp`` — response transmit: ``HTTP1Sender.__call__`` for BlackBull
  (render + flush), ``sanic.response.BaseHTTPResponse.send`` for sanic.
* ``handler`` — the app-handler region, registered per-app via
  ``register_handler_timing(app)``.  BlackBull uses the
  ``before_handler``/``after_handler`` lifecycle events (blocking=True so the
  bracket is exact); sanic uses the ``http.handler.before``/``http.handler.after``
  signals.  Per-request pairing is keyed by ``id(conn)`` / ``id(request)`` —
  valid at B1 (at most one in-flight request per connection; a pipelined lane
  would collide and must not use this instrument).
* ``parse`` — the F3 fork: bytes-in-buffer → parsed-request-ready, armed by
  ``BB_PARSE_TIMING_OUT``.  BlackBull brackets the synchronous
  ``HTTP1Actor._parse`` (entry→return; builds the ``Connection`` from a
  complete head) PLUS the synchronous head-delimiter scan
  (``ReadBuffer.find_head_end``) — the scope correction from the F3 review:
  sanic's bracket includes its head scan, so BB's must too.  sanic brackets
  ``Http.http1_request_header`` (entry→return), which contains the head-scan
  loop and the Request construction.  It also CONTAINS two inline
  ``http.lifecycle.*`` signal dispatches in source, but sanic's TouchUp
  ``RemoveDispatch`` AST visitor deletes them at startup when no listeners
  are registered (verified: the rebuilt method's ``co_names`` has no
  ``dispatch``; the bench app registers none), so sanic's parse has no
  dispatch cost here — the ``parse_dispatch_*`` ledger reads 0 and doubles
  as the stripping proof.  sanic 25.12.1 parses with pure Python (httptools
  is vestigial metadata) — no C-parser undercount vs BB's pure-Python parse.
  PARKING RULE: ``thread_time()`` accumulates the single event-loop thread's
  CPU for ALL connections while parked (the original ``read_head``→``_parse``
  bracket measured 8–11 ms/req on EC2), so the seam must never cross a
  parking await.  BB's ``_parse``/``find_head_end`` are synchronous and
  cannot park.  sanic's ``http1_request_header`` CAN park
  (``await self._receive_more()`` on a short head, http1.py:193) — it was
  clean at B1 c=256 only because the buffer is never empty there; that is a
  property of the operating point, not a seam guarantee.  Seam quality is
  proven by the whole-run ``parse_max_us`` (sub-ms = no parking; a parked
  bracket shows in milliseconds).

A SIGUSR1 handler writes a cumulative snapshot of all seams to
``BB_TIMING_SNAP`` (``seq=... seam=.. calls=.. sum_ms=.. ... handler_calls=..
handler_sum_ms=.. parse_calls=.. parse_sum_ms=..``), so the harness can diff
per-scenario sums at exact scenario boundaries.  Writer threads additionally
dump a running summary to ``BB_RESP_TIMING_OUT`` and ``BB_PARSE_TIMING_OUT``
every ``BB_RESP_TIMING_INTERVAL`` seconds (the resp prefix keeps the F1
format so the F1 analysis section still parses it).

F2 caveat (record in the fork record): for BlackBull's simplified handlers the
send runs *inside* the router wrapper, so BB's handler bracket includes the
resp seam; the analysis subtracts ``resp`` to get handler logic.  For sanic
the send is outside the handler call, so its bracket is logic-only.  The F2
primary cut (front = total − resp − handler_logic) is defined so both stacks
land on the same quantity: read+parse+dispatch (+ machinery + post-handler
glue).  F3 then bisects front further: parse = read+parse, remainder =
dispatch + machinery.

v2 (after the F2 run): the original event/signal bracket (BB
``before_handler``/``after_handler`` events; sanic ``http.handler.*``
signals) measured ~3.7 µs/req (BB) / ~9.7 µs/req (sanic) of ASYMMETRIC
per-request overhead that collapsed the BB−sanic delta (+7.43 → +1.18).  The
handler region is therefore timed with a cheap direct call wrapper instead
(BB: ``Router.__getitem__`` patched to wrap the resolved handler, cached +
route hooks copied; sanic: ``app.router.get`` patched to wrap the returned
handler) — ~0.5 µs/req, like the resp seam.

Env-gated; off by default.
"""
import inspect
import os
import signal
import threading
import time

_OUT = os.environ.get("BB_RESP_TIMING_OUT", "")
_SNAP = os.environ.get("BB_TIMING_SNAP", "")
_POUT = os.environ.get("BB_PARSE_TIMING_OUT", "")
_DOUT = os.environ.get("BB_DISPATCH_TIMING_OUT", "")
_ROUT = os.environ.get("BB_READ_TIMING_OUT", "")
# Multi-worker (SO_REUSEPORT / serve's os.cpu_count() default) safety: every
# worker would truncate the same file with mode "w" and race its contents;
# scope each worker's file by pid.
if _OUT and os.environ.get("BB_WORKERS", "1") != "1":
    _OUT = f"{_OUT}.{os.getpid()}"
if _SNAP and os.environ.get("BB_WORKERS", "1") != "1":
    _SNAP = f"{_SNAP}.{os.getpid()}"
if _POUT and os.environ.get("BB_WORKERS", "1") != "1":
    _POUT = f"{_POUT}.{os.getpid()}"
if _DOUT and os.environ.get("BB_WORKERS", "1") != "1":
    _DOUT = f"{_DOUT}.{os.getpid()}"
if _ROUT and os.environ.get("BB_WORKERS", "1") != "1":
    _ROUT = f"{_ROUT}.{os.getpid()}"
_INTERVAL = float(os.environ.get("BB_RESP_TIMING_INTERVAL", "5"))

_lock = threading.RLock()
_r_cnt = 0
_r_sum = 0.0
_h_cnt = 0
_h_sum = 0.0
_p_cnt = 0
_p_sum = 0.0
_p_scan_cnt = 0
_p_scan_sum = 0.0
_n_cnt = 0
_n_sum = 0.0
# F4 app-dispatch seam: BB ``BlackBull.__call__`` / sanic ``handle_request``
# (async brackets; corrected with the async null I_a).
_d_cnt = 0
_d_sum = 0.0
_d_max = 0.0
_na_cnt = 0
_na_sum = 0.0
# F5 read-path seam: the transport read callbacks.  BB is a BufferedProtocol
# → TWO callbacks per read (get_buffer + buffer_updated, each creates/drops a
# memoryview); sanic is a plain Protocol → ONE callback (data_received, no
# memoryview).  All sync (no parking).
_gb_cnt = 0
_gb_sum = 0.0
_bu_cnt = 0
_bu_sum = 0.0
_dr_cnt = 0
_dr_sum = 0.0
# Head-scan bucketing: split find_head_end calls by buffer state at entry
# (empty ⇒ the check-then-wait first call, nearly free; data ⇒ the real scan).
_se_cnt = 0
_se_sum = 0.0
_pd_cnt = 0
_pd_sum = 0.0
_seam = ""
_seam_max = 0.0
_parse_max = 0.0
_pd_max = 0.0
# sanic parse-internal dispatch attribution: 1 while ``http1_request_header``
# is on the stack (event-loop thread only — no lock needed).
_in_sanic_parse = 0
# Null seam (F3 review fix): a noop wrapped with the identical thread_time
# bracket, called once per request.  Its measured dt IS the wrapper's
# inside-window inflation I (the clock-read/call-setup share that every
# bracket includes by construction).  Armed per stack (sync for BB, async for
# sanic) so I matches the segments it corrects.
_null_wrapped = None
# F4: async null for the app-dispatch brackets (both stacks' dispatch entry
# is async, so I_a (≈1.09 µs) is the dispatch-bracket inflation).
_null_wrapped_async = None
_started = False
_snap_seq = 0


def _record_resp(dt: float) -> None:
    global _r_cnt, _r_sum, _seam_max
    with _lock:
        _r_cnt += 1
        _r_sum += dt
        if dt > _seam_max:
            _seam_max = dt


def _record_handler(dt: float) -> None:
    global _h_cnt, _h_sum
    with _lock:
        _h_cnt += 1
        _h_sum += dt


def _record_parse(dt: float) -> None:
    global _p_cnt, _p_sum, _parse_max
    with _lock:
        _p_cnt += 1
        _p_sum += dt
        if dt > _parse_max:
            _parse_max = dt


def _record_parse_scan(dt: float) -> None:
    """BB head-delimiter scan (``find_head_end``, per scan call).  Folds into
    the parse total (scope correction) WITHOUT counting as a request, so
    ``parse_calls`` stays per-request; the scan-only sum AND call count are
    kept for transparency and for the null-seam correction (a segment with K
    wrapper calls is corrected by K × I, where I = null-seam inflation)."""
    global _p_sum, _p_scan_sum, _p_scan_cnt
    with _lock:
        _p_sum += dt
        _p_scan_sum += dt
        _p_scan_cnt += 1


def _record_null(dt: float) -> None:
    """Null-seam: the noop's measured dt = the wrapper's inside-window
    inflation I (per call).  I = null_sum / null_calls."""
    global _n_cnt, _n_sum
    with _lock:
        _n_cnt += 1
        _n_sum += dt


def _record_dispatch(dt: float) -> None:
    """F4 app-dispatch bracket (``BlackBull.__call__`` / ``handle_request``,
    once per request; async bracket → corrected with I_a)."""
    global _d_cnt, _d_sum, _d_max
    with _lock:
        _d_cnt += 1
        _d_sum += dt
        if dt > _d_max:
            _d_max = dt


def _record_null_async(dt: float) -> None:
    """F4 async null: I_a for the async dispatch brackets (both stacks)."""
    global _na_cnt, _na_sum
    with _lock:
        _na_cnt += 1
        _na_sum += dt


def _record_get_buffer(dt: float) -> None:
    global _gb_cnt, _gb_sum
    with _lock:
        _gb_cnt += 1
        _gb_sum += dt


def _record_buffer_updated(dt: float) -> None:
    global _bu_cnt, _bu_sum
    with _lock:
        _bu_cnt += 1
        _bu_sum += dt


def _record_data_received(dt: float) -> None:
    global _dr_cnt, _dr_sum
    with _lock:
        _dr_cnt += 1
        _dr_sum += dt


def _record_scan_empty(dt: float) -> None:
    """Head-scan bucket: the call ran with an EMPTY buffer (available==0)."""
    global _se_cnt, _se_sum
    with _lock:
        _se_cnt += 1
        _se_sum += dt


def _record_parse_dispatch(dt: float) -> None:
    """sanic parse-internal signal dispatch (``http.lifecycle.*`` inside
    ``http1_request_header``).  Ledgered separately so the parse/construct
    portion = parse − parse_dispatch."""
    global _pd_cnt, _pd_sum, _pd_max
    with _lock:
        _pd_cnt += 1
        _pd_sum += dt
        if dt > _pd_max:
            _pd_max = dt


def _summary() -> str:
    """Cumulative summary.  The resp prefix keeps the F1 line format so the
    F1 analysis section's regex (``seam=.. calls=.. sum_ms=..``) still
    parses; handler/parse fields are appended."""
    with _lock:
        r_mean = _r_sum / _r_cnt if _r_cnt else 0.0
        return (
            f"seam={_seam} calls={int(_r_cnt)} sum_ms={_r_sum * 1e3:.3f} "
            f"mean_us={r_mean * 1e6:.3f} max_us={_seam_max * 1e6:.3f} "
            f"handler_calls={int(_h_cnt)} handler_sum_ms={_h_sum * 1e3:.3f} "
            f"parse_calls={int(_p_cnt)} parse_sum_ms={_p_sum * 1e3:.3f} "
            f"parse_max_us={_parse_max * 1e6:.3f} "
            f"parse_scan_calls={int(_p_scan_cnt)} "
            f"parse_scan_sum_ms={_p_scan_sum * 1e3:.3f} "
            f"null_calls={int(_n_cnt)} null_sum_ms={_n_sum * 1e3:.3f} "
            f"parse_dispatch_calls={int(_pd_cnt)} "
            f"parse_dispatch_sum_ms={_pd_sum * 1e3:.3f} "
            f"parse_dispatch_max_us={_pd_max * 1e6:.3f} "
            f"dispatch_calls={int(_d_cnt)} "
            f"dispatch_sum_ms={_d_sum * 1e3:.3f} "
            f"dispatch_max_us={_d_max * 1e6:.3f} "
            f"null_a_calls={int(_na_cnt)} null_a_sum_ms={_na_sum * 1e3:.3f} "
            f"get_buffer_calls={int(_gb_cnt)} get_buffer_sum_ms={_gb_sum * 1e3:.3f} "
            f"buffer_updated_calls={int(_bu_cnt)} buffer_updated_sum_ms={_bu_sum * 1e3:.3f} "
            f"data_received_calls={int(_dr_cnt)} data_received_sum_ms={_dr_sum * 1e3:.3f} "
            f"scan_empty_calls={int(_se_cnt)} scan_empty_sum_ms={_se_sum * 1e3:.3f}"
        )


def _snap_handler(signum: int, frame) -> None:  # noqa: ARG001
    global _snap_seq
    _snap_seq += 1
    try:
        with open(_SNAP, "w") as fh:
            fh.write(f"seq={_snap_seq} {_summary()}\n")
    except OSError:
        pass


def _writer() -> None:
    while True:
        time.sleep(_INTERVAL)
        try:
            with open(_OUT, "w") as fh:
                fh.write(_summary() + "\n")
        except OSError:
            pass


def _pwriter() -> None:
    """Writer for the F3 parse-seam sum file (same summary line; the
    analysis reads the parse_* fields)."""
    while True:
        time.sleep(_INTERVAL)
        try:
            with open(_POUT, "w") as fh:
                fh.write(_summary() + "\n")
        except OSError:
            pass


def _dwriter() -> None:
    """Writer for the F4 app-dispatch sum file (same summary line; the
    analysis reads the dispatch_* / null_a_* fields)."""
    while True:
        time.sleep(_INTERVAL)
        try:
            with open(_DOUT, "w") as fh:
                fh.write(_summary() + "\n")
        except OSError:
            pass


def _rwriter() -> None:
    """Writer for the F5 read-path sum file (same summary line; the analysis
    reads the get_buffer_* / buffer_updated_* / data_received_* fields)."""
    while True:
        time.sleep(_INTERVAL)
        try:
            with open(_ROUT, "w") as fh:
                fh.write(_summary() + "\n")
        except OSError:
            pass


def _wrap_resp(cls: type, orig_name: str) -> None:
    orig = getattr(cls, orig_name)

    async def wrapper(*a, **k):
        t0 = time.thread_time()
        try:
            return await orig(*a, **k)
        finally:
            _record_resp(time.thread_time() - t0)

    wrapper.__name__ = getattr(orig, "__name__", orig_name)
    setattr(cls, orig_name, wrapper)


# Cheap direct handler wrappers (v2).  Caches keyed by id(handler); the
# router holds the function objects stably, so id() is stable per route.
_bwrap: dict = {}
_swrap: dict = {}


def _wrap_bb_handler(fn):
    """BlackBull: wrap the router-resolved handler ``function``.  For
    simplified handlers this is the router's (conn, receive, send) wrapper,
    which runs logic + conversion + send — so the bracket includes the resp
    seam (the analysis subtracts it for handler logic).  Cached per fn;
    route hooks are copied so the dispatcher still finds them."""
    w = _bwrap.get(id(fn))
    if w is not None:
        return w

    async def wrapper(conn, receive, send, _fn=fn):
        t0 = time.thread_time()
        try:
            return await _fn(conn, receive, send)
        finally:
            _record_handler(time.thread_time() - t0)

    wrapper.__name__ = getattr(fn, "__name__", "handler")
    from blackbull.router import _copy_route_hooks

    _copy_route_hooks(wrapper, fn)
    _bwrap[id(fn)] = wrapper
    return wrapper


def _wrap_sanic_handler(handler):
    """Sanic: wrap the route handler call ``handler(request, **match_info)``
    — logic only (sanic sends the response after the handler returns, in
    http1.py).  Cached per handler; sync and async handlers both handled."""
    w = _swrap.get(id(handler))
    if w is not None:
        return w

    async def wrapper(request, **match_info):
        t0 = time.thread_time()
        r = handler(request, **match_info)
        if inspect.isawaitable(r):
            r = await r
        _record_handler(time.thread_time() - t0)
        return r

    wrapper.__name__ = getattr(handler, "__name__", "handler")
    _swrap[id(handler)] = wrapper
    return wrapper


def _arm_null(*, sync: bool) -> None:
    """Arm the null seam: a noop wrapped with the identical thread_time
    bracket, called once per request from the parse wrappers.  Its measured
    dt = the wrapper's inside-window inflation I (the first clock-read +
    call-setup share every bracket includes).  ``sync`` matches the segment
    wrappers it corrects (BB's ``_parse``/``find_head_end`` are sync; sanic's
    ``http1_request_header`` is async)."""
    global _null_wrapped

    if sync:
        def _timed_null():
            t0 = time.thread_time()
            try:
                return None
            finally:
                _record_null(time.thread_time() - t0)

        _null_wrapped = _timed_null
    else:
        async def _timed_null_async():
            t0 = time.thread_time()
            try:
                return None
            finally:
                _record_null(time.thread_time() - t0)

        _null_wrapped = _timed_null_async


def _arm_null_async() -> None:
    """F4 async null: I_a for the app-dispatch brackets.  Both stacks' dispatch
    entry (``BlackBull.__call__`` / sanic ``handle_request``) is an async
    function, so its inside-window inflation matches an ASYNC noop (≈1.09 µs),
    not the sync null the parse/scan brackets use (≈1.06 µs)."""
    global _null_wrapped_async

    async def _timed_null_async():
        t0 = time.thread_time()
        try:
            return None
        finally:
            _record_null_async(time.thread_time() - t0)

    _null_wrapped_async = _timed_null_async


def _wrap_parse_bb(actor_cls: type, reader_cls: type) -> None:
    """BlackBull F3 parse seam (scope-corrected): bracket the synchronous
    ``HTTP1Actor._parse`` (entry→return) PLUS the synchronous head-delimiter
    scan ``ReadBuffer.find_head_end`` (per call, folded into the parse total).

    The original design also patched ``BufferReader.read_head`` (entry) so
    the bracket covered bytes-scan → parsed-ready, but that is UNMEASURABLE
    with ``thread_time()`` under concurrency: ``read_head`` parks on
    ``wait_for_data()`` when the head is not fully buffered, and while
    parked the single event-loop thread runs OTHER connections — all of
    whose CPU accumulates in the calling thread's ``thread_time()``.  The
    EC2 F3 run measured parse ≈ 8–11 ms/req (vs sanic 5.95 µs) — one full
    connection cycle of foreign CPU.  ``_parse`` and ``find_head_end`` are
    synchronous and cannot park, so the bracket is exact.  The scope
    correction (add the scan) makes BB's parse comparable to sanic's, whose
    ``http1_request_header`` includes its own head scan.  When the null seam
    is armed it is called once per request BEFORE the ``_parse`` bracket, so
    its own cost lands outside the measured window."""
    _parse = actor_cls._parse

    def _timed_parse(self, *a, **k):
        if _null_wrapped is not None:
            _null_wrapped()
        t0 = time.thread_time()
        try:
            return _parse(self, *a, **k)
        finally:
            _record_parse(time.thread_time() - t0)

    _timed_parse.__name__ = getattr(_parse, "__name__", "_parse")
    actor_cls._parse = _timed_parse

    _find_head_end = reader_cls.find_head_end

    def _timed_find_head_end(self, *a, **k):
        empty = self.available == 0
        t0 = time.thread_time()
        try:
            return _find_head_end(self, *a, **k)
        finally:
            dt = time.thread_time() - t0
            _record_parse_scan(dt)
            if empty:
                _record_scan_empty(dt)

    _timed_find_head_end.__name__ = getattr(
        _find_head_end, "__name__", "find_head_end")
    reader_cls.find_head_end = _timed_find_head_end


def _wrap_parse_sanic(http_cls: type, app_cls: type) -> None:
    """Sanic F3 parse seam (scope-corrected): bracket
    ``Http.http1_request_header`` entry→return, and ledger any inline signal
    dispatch inside it as ``parse_dispatch_*``.

    The Request object is constructed inside this method (verified in the
    gate: ``request_class(...)`` at line ~252, published to ``self.request``
    at ~294), so the bracket is bytes-in-buffer → parsed-ready.  The parser
    is pure Python in 25.12.1 (httptools is vestigial metadata).

    DISPATCH STRIPPING (verified 2026-08-12): ``http1_request_header``
    contains two inline ``await self.dispatch(...)`` calls
    (``http.lifecycle.read_head`` ~205, ``http.lifecycle.request`` ~261,
    dispatched via ``self.dispatch = self.protocol.app.dispatch`` at line
    84).  But ``Http.__touchup__`` includes ``http1_request_header`` and
    sanic's ``RemoveDispatch`` AST visitor DELETES those dispatch statements
    at startup when no ``http.lifecycle.*`` listeners are registered — the
    rebuilt method's ``__code__.co_names`` has no ``dispatch`` (verified).
    The bench app registers none, so sanic's parse has NO dispatch cost; the
    ``parse_dispatch_*`` ledger correctly reads 0 and doubles as the proof
    that the stripping happened.  The ``Sanic.dispatch`` wrap here is
    therefore a safeguard: it only records if a future app registers
    ``http.lifecycle.*`` (in which case TouchUp keeps the dispatch and the
    analysis must subtract it)."""
    global _in_sanic_parse
    _http1_request_header = http_cls.http1_request_header

    async def _timed_http1_request_header(self, *a, **k):
        global _in_sanic_parse
        if _null_wrapped is not None:
            await _null_wrapped()
        _in_sanic_parse += 1
        t0 = time.thread_time()
        try:
            return await _http1_request_header(self, *a, **k)
        finally:
            _record_parse(time.thread_time() - t0)
            _in_sanic_parse -= 1

    _timed_http1_request_header.__name__ = getattr(
        _http1_request_header, "__name__", "http1_request_header")
    http_cls.http1_request_header = _timed_http1_request_header

    _dispatch = app_cls.dispatch

    async def _timed_dispatch(self, *a, **k):
        if _in_sanic_parse:
            t0 = time.thread_time()
            try:
                return await _dispatch(self, *a, **k)
            finally:
                _record_parse_dispatch(time.thread_time() - t0)
        return await _dispatch(self, *a, **k)

    _timed_dispatch.__name__ = getattr(_dispatch, "__name__", "dispatch")
    app_cls.dispatch = _timed_dispatch


def _wrap_dispatch_bb(app_cls: type) -> None:
    """BlackBull F4 app-dispatch seam: bracket ``BlackBull.__call__``
    entry→return.  Called exactly once per request by ``RequestActor.run``
    (native Connection path; ``await self._app(target, receive, send)``) after
    the parse seam and outside the actor's keep-alive loop, so it does not
    overlap parse, resp, or hlog and cannot park on ``wait_for_data()`` for
    the body-less B1 workload (the receive is only touched by the handler,
    which the plaintext handler never awaits).  It contains the whole
    app-level dispatch: middleware chain (empty for the bench app), scheme
    dispatch, router lookup (inside hlog), request guard, handler (inside
    hlog), the send (inside resp), and the terminal-event guards.  Async
    bracket → corrected with the async null I_a.  Armed at import for BB
    (BlackBull never re-execs ``__call__`` from source)."""
    _call = app_cls.__call__

    async def _timed_call(self, conn, receive, send):
        if _null_wrapped_async is not None:
            await _null_wrapped_async()
        t0 = time.thread_time()
        try:
            return await _call(self, conn, receive, send)
        finally:
            _record_dispatch(time.thread_time() - t0)

    _timed_call.__name__ = getattr(_call, "__name__", "__call__")
    app_cls.__call__ = _timed_call


def _wrap_dispatch_sanic(app_cls: type) -> None:
    """Sanic F4 app-dispatch seam: bracket ``Sanic.handle_request``
    entry→return.  Bound once per protocol as ``request_handler``
    (http_protocol.py:67) and invoked from ``Http.http1`` right after
    ``http1_request_header`` (the parse seam), so parse and dispatch are
    sequential and non-overlapping.  Contains router lookup (inside hlog),
    handler (inside hlog), and the response send (inside resp).  For the
    body-less B1 GET the handler never awaits ``receive`` and the send is the
    already-measured-clean ``BaseHTTPResponse.send``, so the bracket does not
    park.  ``handle_request`` IS in ``Sanic.__touchup__``, so like the parse
    seam this patch must run AFTER ``TouchUp.run`` (deferred via
    ``register_parse_timing``'s before_server_start listener).  Async bracket
    → corrected with I_a."""
    _handle_request = app_cls.handle_request

    async def _timed_handle_request(self, request):
        if _null_wrapped_async is not None:
            await _null_wrapped_async()
        t0 = time.thread_time()
        try:
            return await _handle_request(self, request)
        finally:
            _record_dispatch(time.thread_time() - t0)

    _timed_handle_request.__name__ = getattr(
        _handle_request, "__name__", "handle_request")
    app_cls.handle_request = _timed_handle_request


def _wrap_read_bb(reader_cls: type) -> None:
    """BlackBull F5 read-path seam: bracket the two BufferedProtocol
    transport callbacks ``get_buffer`` + ``buffer_updated`` (called by the
    uvloop transport per read; each creates/drops a memoryview — the
    zero-copy design's per-read cost).  Both are sync and cannot park, so
    the brackets are exact; corrected with I_sync per call.  This measures
    the 2-callback-vs-1 (sanic ``data_received``) transport-shape cost that
    the review hypothesises sits inside machinery.  Armed at import (the
    methods are not TouchUp'd)."""
    _get_buffer = reader_cls.get_buffer

    def _timed_get_buffer(self, sizehint):
        t0 = time.thread_time()
        try:
            return _get_buffer(self, sizehint)
        finally:
            _record_get_buffer(time.thread_time() - t0)

    _timed_get_buffer.__name__ = getattr(_get_buffer, "__name__", "get_buffer")
    reader_cls.get_buffer = _timed_get_buffer

    _buffer_updated = reader_cls.buffer_updated

    def _timed_buffer_updated(self, nbytes):
        t0 = time.thread_time()
        try:
            return _buffer_updated(self, nbytes)
        finally:
            _record_buffer_updated(time.thread_time() - t0)

    _timed_buffer_updated.__name__ = getattr(
        _buffer_updated, "__name__", "buffer_updated")
    reader_cls.buffer_updated = _timed_buffer_updated


def _wrap_read_sanic(proto_cls: type) -> None:
    """Sanic F5 read-path seam: bracket the single asyncio.Protocol callback
    ``data_received`` (one callback, no memoryview — the transport shape
    BlackBull's BufferedProtocol is compared against).  Sync, cannot park.
    Not in ``HttpProtocol.__touchup__``, but patched via the same
    before_server_start listener for consistency.  Corrected with I_sync per
    call."""
    _data_received = proto_cls.data_received

    def _timed_data_received(self, data):
        t0 = time.thread_time()
        try:
            return _data_received(self, data)
        finally:
            _record_data_received(time.thread_time() - t0)

    _timed_data_received.__name__ = getattr(
        _data_received, "__name__", "data_received")
    proto_cls.data_received = _timed_data_received


def register_parse_timing(app) -> None:
    """Arm the F3 parse seam for a sanic app (``BB_PARSE_TIMING_OUT`` set).

    sanic 25.12.1 re-execs ``Http.http1_request_header`` from source at
    ``_startup`` (``TouchUp.run``, ``Http.__touchup__``) and replaces the
    class attribute with the rebuilt function — ``BaseScheme.build`` execs
    ``getsource(method)`` and looks up ``exec_locals[method.__name__]``, so
    an import-time closure wrapper (source name ≠ method name) breaks startup
    with KeyError.  The patch is therefore deferred to a
    ``before_server_start`` listener, which runs after ``TouchUp.run`` and is
    never re-exec'd.  Also arms the F4 app-dispatch seam (``Sanic.
    handle_request``, likewise in ``Sanic.__touchup__``) when
    BB_DISPATCH_TIMING_OUT is set, and the F5 read-path seam
    (``data_received``) when BB_READ_TIMING_OUT is set.  No-op unless one
    of the seam's output envs is set for a sanic server."""
    if not _POUT and not _DOUT and not _ROUT:
        return
    if not (os.environ.get("BB_PARSE_TIMING_SERVER", "") == "sanic"
            or os.environ.get("BB_DISPATCH_TIMING_SERVER", "") == "sanic"
            or os.environ.get("BB_READ_TIMING_SERVER", "") == "sanic"):
        return

    async def _arm(_, loop):  # noqa: ARG001  (before_server_start signature)
        from sanic.http import Http

        _wrap_parse_sanic(Http, type(app))
        if os.environ.get("BB_NULL_SEAM"):
            _arm_null(sync=False)
        # F4 app-dispatch seam: ``Sanic.handle_request`` is also in
        # ``Sanic.__touchup__``, so it is patched here (after TouchUp.run)
        # rather than at import.
        if _DOUT:
            _wrap_dispatch_sanic(type(app))
            if os.environ.get("BB_NULL_SEAM"):
                _arm_null_async()
        # F5 read-path seam: ``data_received`` (single asyncio.Protocol
        # callback; not in HttpProtocol.__touchup__ but patched here for
        # consistency).
        if _ROUT:
            from sanic.server.protocols.http_protocol import HttpProtocol

            _wrap_read_sanic(HttpProtocol)

    app.listener("before_server_start")(_arm)


def register_handler_timing(app) -> None:
    """Install the cheap handler-region timing wrappers (v2).

    BlackBull: patch ``Router.__getitem__`` so every resolved handler is
    wrapped with a cached timer (route hooks copied) — the router's own cache
    then returns the timed wrapper for all subsequent requests, so the only
    per-request cost is the wrapper call (~0.5 µs/req).  Sanic: patch
    ``app.router.get`` to wrap the returned handler the same way.

    The v1 event/signal bracket added ~3.7 µs/req (BB) / ~9.7 µs/req (sanic)
    of asymmetric overhead that collapsed the F2 delta; this is why the
    direct wrapper replaces it.  No-op unless a timing env is set.
    """
    if not (_OUT or _SNAP):
        return
    server = os.environ.get("BB_RESP_TIMING_SERVER", "")
    if server == "blackbull":
        from blackbull.router import Router

        _orig_getitem = Router.__getitem__

        def _timed_getitem(self, key):
            return _wrap_bb_handler(_orig_getitem(self, key))

        Router.__getitem__ = _timed_getitem
    elif server == "sanic":
        _orig_get = app.router.get

        def _timed_get(path, method, host):
            route, handler, kwargs = _orig_get(path, method, host)
            return route, _wrap_sanic_handler(handler), kwargs

        app.router.get = _timed_get


def activate() -> None:
    global _started, _seam
    if (not _OUT and not _SNAP and not _POUT and not _DOUT and not _ROUT) or _started:
        return
    # The app files declare their server (``BB_RESP_TIMING_SERVER`` /
    # ``BB_PARSE_TIMING_SERVER`` / ``BB_DISPATCH_TIMING_SERVER`` /
    # ``BB_READ_TIMING_SERVER``); both libraries are importable in either
    # process, so import-probing cannot choose the seam.  ``blackbull`` ->
    # HTTP1Sender.__call__ (H/1 render + flush); ``sanic`` ->
    # BaseHTTPResponse.send (transport write).
    server = os.environ.get("BB_RESP_TIMING_SERVER", "") or \
        os.environ.get("BB_PARSE_TIMING_SERVER", "") or \
        os.environ.get("BB_DISPATCH_TIMING_SERVER", "") or \
        os.environ.get("BB_READ_TIMING_SERVER", "")
    if server == "blackbull":
        from blackbull.server.sender import HTTP1Sender

        _wrap_resp(HTTP1Sender, "__call__")
        _seam = "bb"
    elif server == "sanic":
        from sanic.response import BaseHTTPResponse

        _wrap_resp(BaseHTTPResponse, "send")
        _seam = "sanic"
    else:
        # Fallback auto-detect (no app declaration).
        try:
            from blackbull.server.sender import HTTP1Sender

            _wrap_resp(HTTP1Sender, "__call__")
            _seam = "bb"
        except ImportError:
            try:
                from sanic.response import BaseHTTPResponse

                _wrap_resp(BaseHTTPResponse, "send")
                _seam = "sanic"
            except ImportError:
                return
    # F3 parse seam (bytes-delivered → parsed-request-ready).  Armed by
    # BB_PARSE_TIMING_OUT; follows the same class-level wrap pattern.  BB is
    # patched here at import (no source-rewrite machinery in BlackBull);
    # sanic's ``Http.http1_request_header`` is in ``Http.__touchup__``, so
    # its patch is deferred to ``register_parse_timing`` (before_server_start
    # listener, after TouchUp.run) — never patched at import.
    if _POUT:
        if server == "blackbull":
            from blackbull.server.read_buffer import ReadBuffer
            from blackbull.server.http1_actor import HTTP1Actor

            _wrap_parse_bb(HTTP1Actor, ReadBuffer)
            if os.environ.get("BB_NULL_SEAM"):
                _arm_null(sync=True)
    # F4 app-dispatch seam (``BlackBull.__call__``).  Armed by
    # BB_DISPATCH_TIMING_OUT.  BB is patched here at import (BlackBull never
    # re-execs ``__call__``); sanic's ``Sanic.handle_request`` is in
    # ``Sanic.__touchup__``, so its patch is deferred to
    # ``register_parse_timing`` (before_server_start listener).  Both
    # brackets are async → corrected with the async null I_a.
    if _DOUT:
        if server == "blackbull":
            from blackbull.app import BlackBull

            _wrap_dispatch_bb(BlackBull)
            if os.environ.get("BB_NULL_SEAM"):
                _arm_null_async()
    # F5 read-path seam (``get_buffer`` + ``buffer_updated``).  Armed by
    # BB_READ_TIMING_OUT.  BB is patched here at import (not TouchUp'd);
    # sanic's ``data_received`` is patched in ``register_parse_timing``'s
    # listener (not in ``HttpProtocol.__touchup__`` either, done there for
    # consistency).
    if _ROUT:
        if server == "blackbull":
            from blackbull.server.read_buffer import ReadBuffer

            _wrap_read_bb(ReadBuffer)
    if _SNAP:
        try:
            # Main-thread only; the instrument is activated at import in the
            # main process.  BB/sanic only use SIGTERM/SIGINT, no conflict.
            signal.signal(signal.SIGUSR1, _snap_handler)
        except (ValueError, OSError):
            pass
    if _OUT:
        threading.Thread(target=_writer, daemon=True).start()
    if _POUT:
        threading.Thread(target=_pwriter, daemon=True).start()
    if _DOUT and _DOUT != _POUT:
        threading.Thread(target=_dwriter, daemon=True).start()
    if _ROUT and _ROUT not in (_POUT, _DOUT):
        threading.Thread(target=_rwriter, daemon=True).start()
    _started = True


activate()
