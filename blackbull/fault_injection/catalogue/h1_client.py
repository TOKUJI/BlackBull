"""Named HTTP/1.1 client-side misbehaviour cases.

The last of the grid's four catalogues.  Cells B, C and D shipped named
sets (9, 4 and 11 cases); this cell had **none** — it was reachable only
through the atheris and Hypothesis harnesses, which generate inputs rather
than name them.  That is a real difference in kind: a generated input
tells you *that* something broke, a named case tells you *which known
mistake* you are testing against, and only the second can be cited in a
bug report or parametrized over by a downstream user.

Every case is raw bytes, because on this side the fault *is* the bytes.
A typed `SendRequest` step would be built from the same encoder the
production client uses, and could not emit a fault that encoder has —
the same reasoning that keeps the two fault *servers* off the production
send path.

The cases are drawn from RFC 9112's framing rules and the request-
smuggling literature, so the names line up with what a reader is likely
to be defending against.
"""
from __future__ import annotations

from ..scenario_h1 import (
    ReadResponse, Scenario, SendRawBytes,
)

_READ = ReadResponse(timeout=3.0)


def absent_host() -> Scenario:
    """No Host header.  RFC 9112 §3.2 makes this a 400."""
    return Scenario(name='absent_host', steps=(
        SendRawBytes(b'GET / HTTP/1.1\r\n\r\n'), _READ))


def two_content_lengths() -> Scenario:
    """Two Content-Length headers that disagree (RFC 9112 §6.3).

    The smuggling primitive: a recipient that picks one and a recipient
    that picks the other disagree about where the next request starts.
    """
    return Scenario(name='two_content_lengths', steps=(
        SendRawBytes(b'POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n'
                     b'Content-Length: 6\r\n\r\nhello'), _READ))


def content_length_and_transfer_encoding() -> Scenario:
    """Both framing headers at once — RFC 9112 §6.1 says reject.

    The classic CL.TE / TE.CL desync: whichever one the recipient honours
    determines where it thinks the body ends.
    """
    return Scenario(name='content_length_and_transfer_encoding', steps=(
        SendRawBytes(b'POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n'), _READ))


def duplicate_transfer_encoding() -> Scenario:
    """Two Transfer-Encoding headers, the second not chunked."""
    return Scenario(name='duplicate_transfer_encoding', steps=(
        SendRawBytes(b'POST / HTTP/1.1\r\nHost: x\r\n'
                     b'Transfer-Encoding: chunked\r\n'
                     b'Transfer-Encoding: identity\r\n\r\n0\r\n\r\n'), _READ))


def space_before_header_colon() -> Scenario:
    """``Foo : bar`` — RFC 9112 §5.1 requires rejecting, not trimming.

    Trimming is the defect: a proxy that trims and an origin that rejects
    see different header sets.
    """
    return Scenario(name='space_before_header_colon', steps=(
        SendRawBytes(b'GET / HTTP/1.1\r\nHost: x\r\nFoo : bar\r\n\r\n'), _READ))


def bare_lf_terminators() -> Scenario:
    """Bare LF instead of CRLF (RFC 9112 §2.2 makes recognising it a MAY).

    Implementations legitimately differ, which is exactly why it belongs
    in a catalogue: the case is here to make the difference visible, not
    to assert one answer.
    """
    return Scenario(name='bare_lf_terminators', steps=(
        SendRawBytes(b'GET / HTTP/1.1\nHost: x\n\n'), _READ))


def obs_fold_header() -> Scenario:
    """An obs-fold continuation line (RFC 9112 §5.2 deprecates it)."""
    return Scenario(name='obs_fold_header', steps=(
        SendRawBytes(b'GET / HTTP/1.1\r\nHost: x\r\nFoo: a\r\n b\r\n\r\n'),
        _READ))


def negative_content_length() -> Scenario:
    """``Content-Length: -1`` — not a valid length."""
    return Scenario(name='negative_content_length', steps=(
        SendRawBytes(b'POST / HTTP/1.1\r\nHost: x\r\n'
                     b'Content-Length: -1\r\n\r\n'), _READ))


def chunk_size_not_hex() -> Scenario:
    """A chunk size that is not hexadecimal (RFC 9112 §7.1)."""
    return Scenario(name='chunk_size_not_hex', steps=(
        SendRawBytes(b'POST / HTTP/1.1\r\nHost: x\r\n'
                     b'Transfer-Encoding: chunked\r\n\r\nzz\r\n'), _READ))


def nul_in_header_value() -> Scenario:
    """A NUL byte in a field value — not a valid field-vchar."""
    return Scenario(name='nul_in_header_value', steps=(
        SendRawBytes(b'GET / HTTP/1.1\r\nHost: x\r\nFoo: a\x00b\r\n\r\n'),
        _READ))


def oversized_method_token() -> Scenario:
    """A 2 KiB method token — a head-budget case, not a parse case."""
    return Scenario(name='oversized_method_token', steps=(
        SendRawBytes(b'A' * 2048 + b' / HTTP/1.1\r\nHost: x\r\n\r\n'), _READ))


def trickled_head() -> Scenario:
    """The request head one byte at a time — the slowloris primitive.

    Nothing here is malformed; the fault is the *rate*, which is why the
    server's answer comes from its header deadline rather than its parser.
    """
    return Scenario(name='trickled_head', steps=(
        SendRawBytes(b'GET / HTTP/1.1\r\nHost: x\r\n\r\n', byte_interval=0.05),
        ReadResponse(timeout=5.0)))


def head_never_ends() -> Scenario:
    """Headers that never reach the terminating CRLFCRLF."""
    return Scenario(name='head_never_ends', steps=(
        SendRawBytes(b'GET / HTTP/1.1\r\nHost: x\r\nFoo: bar\r\n'),
        ReadResponse(timeout=5.0)))


def body_shorter_than_declared() -> Scenario:
    """Declares 100 bytes of body and sends 5, then stops."""
    return Scenario(name='body_shorter_than_declared', steps=(
        SendRawBytes(b'POST / HTTP/1.1\r\nHost: x\r\n'
                     b'Content-Length: 100\r\n\r\nshort'),
        ReadResponse(timeout=5.0)))


#: Every case, by name — the shape the other three catalogues use, so a
#: suite can ``parametrize`` over any cell the same way.
CATALOGUE = {
    'absent_host': absent_host,
    'two_content_lengths': two_content_lengths,
    'content_length_and_transfer_encoding': content_length_and_transfer_encoding,
    'duplicate_transfer_encoding': duplicate_transfer_encoding,
    'space_before_header_colon': space_before_header_colon,
    'bare_lf_terminators': bare_lf_terminators,
    'obs_fold_header': obs_fold_header,
    'negative_content_length': negative_content_length,
    'chunk_size_not_hex': chunk_size_not_hex,
    'nul_in_header_value': nul_in_header_value,
    'oversized_method_token': oversized_method_token,
    'trickled_head': trickled_head,
    'head_never_ends': head_never_ends,
    'body_shorter_than_declared': body_shorter_than_declared,
}

__all__ = ['CATALOGUE', *sorted(CATALOGUE)]
