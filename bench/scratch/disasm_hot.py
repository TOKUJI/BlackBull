#!/usr/bin/env python3
"""Print bytecode instruction counts for the hot-path functions.

Run twice (PYTHONPATH = base tree, then treat tree) and diff.  Deterministic:
the instruction stream is the code, independent of this box's throughput noise.
"""
import dis

from blackbull.server import http1_actor, http2_actor, recipient, sender


def count(fn):
    return len(list(dis.get_instructions(fn)))


TARGETS = [
    ("http2._frame_loop", http2_actor.HTTP2Actor._frame_loop),
    ("http2._on_headers_frame", http2_actor.HTTP2Actor._on_headers_frame),
    ("http2._spawn_stream_task", http2_actor.HTTP2Actor._spawn_stream_task),
    ("http2._make_stream_recipient", http2_actor.HTTP2Actor._make_stream_recipient),
    ("http2._declared_body_over_cap", getattr(http2_actor.HTTP2Actor, "_declared_body_over_cap", None)),
    ("http2._refuse_oversized_declared_body", getattr(http2_actor.HTTP2Actor, "_refuse_oversized_declared_body", None)),
    ("HTTP2Recipient.__init__", recipient.HTTP2Recipient.__init__),
    ("HTTP2Recipient.mark_end_of_stream_on_headers", recipient.HTTP2Recipient.mark_end_of_stream_on_headers),
    ("HTTP2Sender.__init__", sender.HTTP2Sender.__init__),
    ("http1.run", http1_actor.HTTP1Actor.run),
    ("http1._dispatch_request", http1_actor.HTTP1Actor._dispatch_request),
    ("_validate_message_framing", http1_actor._validate_message_framing),
    ("HTTP1Recipient.needs_drain", recipient.HTTP1Recipient.needs_drain),
    ("HTTP1Recipient.must_close", getattr(recipient.HTTP1Recipient, "must_close", None)),
]

for name, fn in TARGETS:
    if fn is None:
        print(f"{name}: <absent>")
        continue
    if isinstance(fn, property):
        fn = fn.fget
    code = fn.__code__
    n = count(fn)
    # reachable per-call work: LOAD_ATTR/LOAD_METHOD/LOAD_GLOBAL count as a rough
    # "does this touch the header store / env / clock" fingerprint.
    attr = sum(1 for i in dis.get_instructions(fn) if i.opname in ("LOAD_ATTR", "LOAD_METHOD", "LOAD_GLOBAL"))
    print(f"{name}: {n:4d} instrs, {len(code.co_code):4d} bytes, {attr} load_attr/global")
