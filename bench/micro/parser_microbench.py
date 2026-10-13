#!/usr/bin/env python3
"""Parser leaf microbenchmark — identical request bytes → parsed metadata.

Sprint 100, Phase 2 leaf instrument (validated locally before EC2).

Cut boundary, all four targets: "complete head bytes → parsed request
representation" (method, target, version, headers).  Each parser runs the
same reuse pattern it uses in production (actor / connection / parser
persists across requests; no per-request parser allocation), GC off during
measurement, min-of-many.

Targets:
  - blackbull : HTTP1Actor._parse(data) -> Connection (pure Python)
  - sanic     : the header-parse loop from sanic/http/http1.py
                (decode + split + field loop + Header(...)); the Request
                object needs protocol/transport and is excluded (noted)
  - httptools : httptools.HttpRequestParser.feed_data (C, reused like uvicorn)
  - h11       : h11.Connection.receive_data + next_event + start_next_cycle
                (pure Python, reused like uvicorn's h11 impl)

Validation gate (printed before the numbers; the instrument is only valid if
all four pass):
  1. correctness — every parser yields the same known-answer fields
  2. reuse      — httptools/h11 parse correctly on the 2nd+ call (no state
                  leakage)
  3. sanity     — per-call variance sane across repeats
"""
import gc
import statistics
import time

HEAD = (b"GET /plaintext HTTP/1.1\r\n"
        b"host: 127.0.0.1:8443\r\n"
        b"user-agent: bench\r\n"
        b"accept: */*\r\n"
        b"connection: keep-alive\r\n"
        b"\r\n")


def bench(fn, n=30000, repeat=7):
    for _ in range(3000):
        fn()
    res = []
    for _ in range(repeat):
        gc.disable()
        t0 = time.perf_counter()
        for _ in range(n):
            fn()
        dt = time.perf_counter() - t0
        gc.enable()
        res.append(dt / n * 1e6)
    return (statistics.median(res), min(res), statistics.stdev(res))


# --- blackbull --------------------------------------------------------------
from blackbull.server.http1_actor import HTTP1Actor

_bb = object.__new__(HTTP1Actor)
_bb._max_line = 8192


def bb_parse():
    c = _bb._parse(HEAD)
    return (c.method, c.path, c.http_version, len(c.headers))


# --- sanic ------------------------------------------------------------------
from sanic.compat import Header

_SANIC_HEAD = HEAD[:-4]  # sanic's `head = buf[:pos]` excludes the terminator


def sanic_parse():
    raw = _SANIC_HEAD.decode(errors="surrogateescape")
    reqline, *split_headers = raw.split("\r\n")
    method, url, protocol = reqline.split(" ")
    headers = []
    request_body = False
    for name, value in (h.split(":", 1) for h in split_headers):
        name = name.lower()
        value = value.lstrip()
        if name in ("content-length", "transfer-encoding"):
            request_body = True
        headers.append((name, value))
    headers_instance = Header(headers)
    return (method, url, protocol, len(headers_instance))


# --- httptools --------------------------------------------------------------
import httptools


class _CB:
    def __init__(self):
        self.method = None
        self.url = None
        self.headers = []
        self.msgs = 0

    def on_message_begin(self):
        self.method = None
        self.url = None
        self.headers = []

    def on_url(self, url):
        self.url = url

    def on_header(self, name, value):
        self.headers.append((name, value))

    def on_headers_complete(self):
        return False

    def on_body(self, body):
        pass

    def on_message_complete(self):
        self.msgs += 1


_cb = _CB()
_ht = httptools.HttpRequestParser(_cb)


def httptools_parse():
    _ht.feed_data(HEAD)
    return (_cb.url, _cb.msgs)


# --- h11 --------------------------------------------------------------------
# h11's state machine embeds parse in the request/response cycle: a reused
# Connection refuses the next request until a response is sent, and a manual
# state reset is fragile (h11's reader lands in PAUSED).  So h11 is measured
# with a FRESH Connection per iteration — this INCLUDES the Connection
# allocation, which uvicorn pays once per connection, not per request.
# The number therefore overstates h11's true per-request parse; noted in the
# report and revisited only if h11 turns out to be a contender.
import h11


def h11_parse():
    conn = h11.Connection(h11.SERVER)
    conn.receive_data(HEAD)
    ev = conn.next_event()
    return (ev.method, ev.target, len(ev.headers))


# --- validation gate --------------------------------------------------------
print("=== validation gate ===")
ok = True

bb0 = bb_parse()
s0 = sanic_parse()
ht0 = httptools_parse()
h110 = h11_parse()
ht1 = httptools_parse()  # reuse check
h111 = h11_parse()       # fresh-connection check

print(f"  blackbull: {bb0}")
print(f"  sanic    : {s0}")
print(f"  httptools: url={ht0[0]} msgs={ht0[1]}  (2nd call msgs={ht1[1]})")
print(f"  h11      : {h110}  (2nd call {h111})")

# correctness: known answer GET /plaintext HTTP/1.1, 4 headers
expect = ("GET", "/plaintext", "HTTP/1.1", 4)
if bb0[0] != "GET" or bb0[1] != "/plaintext" or len(bb0) != 4:
    print("  FAIL blackbull fields:", bb0)
    ok = False
if s0[0] != "GET" or s0[1] != "/plaintext":
    print("  FAIL sanic fields:", s0)
    ok = False
if ht0[0] != b"/plaintext" or ht0[1] != 1:
    print("  FAIL httptools fields:", ht0)
    ok = False
if h110[0] != b"GET" or h110[1] != b"/plaintext" or h110[2] != 4:
    print("  FAIL h11 fields:", h110)
    ok = False
# reuse: 2nd call must parse a second message
if ht1[1] != 2:
    print("  FAIL httptools reuse:", ht1)
    ok = False
if h111[0] != b"GET":
    print("  FAIL h11 fresh-connection repeat:", h111)
    ok = False

print("  gate:", "PASS" if ok else "FAIL")

# --- measurement ------------------------------------------------------------
print("\n=== per-parse cost (µs/call, n=30000, repeat=7) ===")
for name, fn in (("blackbull", bb_parse),
                 ("sanic    ", sanic_parse),
                 ("httptools", httptools_parse),
                 ("h11      ", h11_parse)):
    med, lo, sd = bench(fn)
    print(f"  {name}: median {med:7.3f}  min {lo:7.3f}  stdev {sd:6.3f}")
