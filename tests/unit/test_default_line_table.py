"""The shared default line table must be indistinguishable from parsing.

The per-connection cache cannot help the first request on a connection.  A
small table of header lines whose value set is fixed by a specification can,
because those bytes are the same for every client on every deployment — so
they are validated once at import and shared process-wide.

Shared state on the parse path is exactly where a wrong entry would be most
damaging, so the table is held to two rules, and both are asserted here:

1. **Every entry maps to what parsing that line actually produces.**  Not to
   what a human wrote next to it.
2. **No framing-relevant name may appear.**  Framing must be read from the
   request every time, never from a shared table.

A third rule is about honesty rather than safety: entries must be justified by
a specification's value set, not by their frequency in a packet capture.  That
one cannot be asserted mechanically — the closest proxy is rule 2 plus the
exclusion of client-, user- and deployment-specific names, which is checked.
"""
import pytest

from blackbull.server.http1_actor import (
    _DEFAULT_LINES,
    _FRAMING_NAMES,
    _LINE_CACHE_MAX_LINE,
    HTTP1Actor,
)


def _actor() -> HTTP1Actor:
    a = HTTP1Actor.__new__(HTTP1Actor)
    a._ssl = False
    return a


def test_table_is_not_empty():
    assert len(_DEFAULT_LINES) > 20


@pytest.mark.parametrize('line', sorted(_DEFAULT_LINES),
                         ids=lambda ln: ln.decode('latin-1'))
def test_entry_matches_what_parsing_that_line_produces(line):
    """Rule 1 — differential against the parser itself, on a cold actor."""
    raw = (b'GET /x HTTP/1.1\r\nHost: example.com\r\n' + line + b'\r\n\r\n')
    conn = _actor()._parse(raw)
    pairs = list(iter(conn.headers))
    assert _DEFAULT_LINES[line] in pairs, (
        f'{line!r} maps to {_DEFAULT_LINES[line]!r} but parsing it yields '
        f'{[p for p in pairs if p[0] != b"host"]!r}')


@pytest.mark.parametrize('line', sorted(_DEFAULT_LINES),
                         ids=lambda ln: ln.decode('latin-1'))
def test_no_framing_header_is_pre_seeded(line):
    """Rule 2 — a shared table is the last place framing should come from."""
    name = _DEFAULT_LINES[line][0]
    assert name not in _FRAMING_NAMES


def test_no_client_or_deployment_specific_names():
    """Values that differ per browser build, per user or per deployment must
    not be seeded — seeding those would be tuning to whatever was captured."""
    forbidden = {
        b'user-agent', b'accept-language', b'sec-ch-ua', b'cookie', b'referer',
        b'origin', b'authorization', b'if-none-match', b'if-modified-since',
        b'host',
    }
    seeded = {pair[0] for pair in _DEFAULT_LINES.values()}
    assert not (seeded & forbidden)


def test_every_entry_respects_the_per_line_cap():
    assert all(len(line) <= _LINE_CACHE_MAX_LINE for line in _DEFAULT_LINES)


def test_table_is_small_enough_to_be_shared():
    """It is process-wide, so it should stay a table, not a database."""
    total = sum(len(k) + len(v[0]) + len(v[1]) for k, v in _DEFAULT_LINES.items())
    assert total < 8192, f'{total} B of shared table is more than intended'


def test_first_request_on_a_connection_hits_the_table():
    """The point of the whole thing: no warm-up needed for a spec line."""
    cold = _actor()
    conn = cold._parse(
        b'GET /x HTTP/1.1\r\nHost: example.com\r\n'
        b'Sec-Fetch-Dest: image\r\nConnection: keep-alive\r\n\r\n')
    pairs = list(iter(conn.headers))
    assert (b'sec-fetch-dest', b'image') in pairs
    # Served from the shared table, so the per-connection cache stayed empty
    # of them — they would be found there on every later request anyway.
    assert b'Sec-Fetch-Dest: image' not in (cold._line_cache or {})


def test_a_near_miss_on_a_seeded_line_is_validated_normally():
    """One changed byte must miss the table and go through the full checks."""
    cold = _actor()
    with pytest.raises(Exception):
        cold._parse(b'GET /x HTTP/1.1\r\nHost: example.com\r\n'
                    b'Sec-Fetch-Dest: ima\x01ge\r\n\r\n')


def test_table_is_never_mutated_by_serving_traffic():
    """It is shared across connections; a write would be cross-connection bleed."""
    before = dict(_DEFAULT_LINES)
    actor = _actor()
    for i in range(20):
        actor._parse(b'GET /x HTTP/1.1\r\nHost: example.com\r\n'
                     b'Sec-Fetch-Mode: cors\r\nX-Learn-%d: v\r\n\r\n' % i)
    assert _DEFAULT_LINES == before
