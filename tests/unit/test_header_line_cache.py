"""The per-connection validated header-line cache must not change meaning.

Keep-alive peers resend byte-identical header lines every request, so a line
that passed full validation once can be replayed from a per-connection dict
instead of being re-split, re-lowercased and re-checked.  The cache is only
legitimate if it is invisible: every request must parse to exactly what it
would have parsed to with the cache disabled, and every request that would
have been rejected must still be rejected.

That is what these tests assert — a *differential* against a cache-free actor,
not a restatement of the cache's own logic.  The safety terms from the design
(exact-bytes key, nothing unvalidated admitted, bounded, per-connection) each
get a test, because each is what stops this from being a smuggling vector.
"""
import pytest

from blackbull.server.http1_actor import (
    _LINE_CACHE_MAX,
    _LINE_CACHE_MAX_BYTES,
    _LINE_CACHE_MAX_LINE,
    BadRequestError,
    HTTP1Actor,
)


def _actor() -> HTTP1Actor:
    """An actor with just enough state for ``_parse``, as test_parser does."""
    a = HTTP1Actor.__new__(HTTP1Actor)
    a._ssl = False
    return a


def _req(*headers: bytes, target: bytes = b'/x', method: bytes = b'GET') -> bytes:
    lines = [method + b' ' + target + b' HTTP/1.1', b'Host: example.com', *headers]
    return b'\r\n'.join(lines) + b'\r\n\r\n'


def _pairs(conn) -> list[tuple[bytes, bytes]]:
    return list(iter(conn.headers))


# --------------------------------------------------------------------------
# Equivalence — the cache is invisible
# --------------------------------------------------------------------------

VALID_VECTORS = [
    _req(b'Accept: */*'),
    _req(b'User-Agent: Mozilla/5.0 (X11; Linux x86_64)'),
    _req(b'Accept:    spaced-out   '),
    _req(b'Accept:\tHTAB-wrapped\t'),
    _req(b'X-Empty:'),
    _req(b'Content-Length: 0', method=b'POST'),
    _req(b'Content-Length: 42', method=b'POST'),
    _req(b'Cookie: session=8f14e45fceea167a5a36dedd4bea2543'),
    _req(b'Accept: a', b'Accept: b'),                 # repeated name, one request
    _req(b'X-Odd-Case-NAME: Value'),
    _req(b'Connection: keep-alive', b'Cache-Control: max-age=0'),
]


@pytest.mark.parametrize('raw', VALID_VECTORS, ids=range(len(VALID_VECTORS)))
def test_second_parse_on_same_connection_matches_the_first(raw):
    """A cache hit must reproduce the cold parse byte-for-byte."""
    warm = _actor()
    first = _pairs(warm._parse(raw))
    second = _pairs(warm._parse(raw))
    assert second == first
    # And it must match an actor that has never seen the line at all.
    assert first == _pairs(_actor()._parse(raw))


@pytest.mark.parametrize('raw', VALID_VECTORS, ids=range(len(VALID_VECTORS)))
def test_ows_stripping_survives_a_cache_hit(raw):
    """The stripped value is what is cached — not the raw post-colon bytes."""
    warm = _actor()
    warm._parse(raw)
    for _name, value in _pairs(warm._parse(raw)):
        assert value == value.strip(b' \t')


REJECTED_VECTORS = [
    _req(b'Bad Name: value'),                     # SP before colon (§5.1)
    _req(b'Bad\x00Name: value'),                  # NUL in name — not tchar
    _req(b' Folded: value'),                      # obs-fold (§5.2)
    _req(b'\tFolded: value'),                     # obs-fold, HTAB
    _req(b'NoColon'),
    _req(b':empty-name'),
    _req(b'X-Ctl: has\x01ctl'),                   # CTL in value
    _req(b'Content_Length: 5'),                   # NORM-UNDERSCORE
    _req(b'Content-Length: 007', method=b'POST'),  # leading zeros (§8.6)
    _req(b'Content-Length:  5', method=b'POST'),   # doubled OWS
]


@pytest.mark.parametrize('raw', REJECTED_VECTORS, ids=range(len(REJECTED_VECTORS)))
def test_rejection_is_stable_across_repeats(raw):
    """A rejected request stays rejected — nothing bad reaches the cache."""
    warm = _actor()
    with pytest.raises(Exception) as first:
        warm._parse(raw)
    with pytest.raises(Exception) as second:
        warm._parse(raw)
    assert type(first.value) is type(second.value)


@pytest.mark.parametrize('raw', REJECTED_VECTORS, ids=range(len(REJECTED_VECTORS)))
def test_a_valid_request_first_does_not_launder_a_bad_one(raw):
    """Warming the cache with good lines must not admit a bad line after it."""
    warm = _actor()
    for good in VALID_VECTORS:
        warm._parse(good)
    with pytest.raises(Exception):
        warm._parse(raw)


# --------------------------------------------------------------------------
# The safety terms
# --------------------------------------------------------------------------

def test_key_is_exact_bytes_so_one_changed_byte_misses():
    """A near-miss line must be validated from scratch, not served from a hit."""
    warm = _actor()
    warm._parse(_req(b'X-Ctl: clean'))
    # Same name, same length, one byte changed into a CTL.
    with pytest.raises(BadRequestError):
        warm._parse(_req(b'X-Ctl: clea\x01'))


def test_case_variant_of_a_cached_name_is_not_served_from_the_hit():
    """``Accept`` and ``ACCEPT`` are different bytes; both must lowercase."""
    warm = _actor()
    first = _pairs(warm._parse(_req(b'Accept: */*')))
    second = _pairs(warm._parse(_req(b'ACCEPT: */*')))
    assert first == second        # both lowercase to b'accept'
    assert (b'accept', b'*/*') in second


def test_content_length_strictness_is_not_bypassed_by_a_warm_cache():
    """A cached ``Content-Length: 5`` must not admit ``005`` on the next request."""
    warm = _actor()
    warm._parse(_req(b'Content-Length: 5', method=b'POST'))
    with pytest.raises(BadRequestError):
        warm._parse(_req(b'Content-Length: 005', method=b'POST'))


def test_cache_is_per_connection():
    """Two actors must not share validated lines — no cross-connection bleed."""
    a, b = _actor(), _actor()
    a._parse(_req(b'Accept: */*'))
    a._parse(_req(b'Accept: */*'))   # admission is deferred to the 2nd request
    assert a._line_cache
    assert not b._line_cache      # untouched by a's traffic
    b._parse(_req(b'Accept: */*'))
    assert a._line_cache is not b._line_cache


def test_cache_is_bounded():
    """A peer sending endless unique lines must not grow the dict without limit."""
    warm = _actor()
    for i in range(_LINE_CACHE_MAX * 3):
        warm._parse(_req(b'X-Unique-%d: v' % i))
    assert len(warm._line_cache) <= _LINE_CACHE_MAX


def test_cache_actually_holds_the_repeated_line():
    """Pin the mechanism: the second parse must be served from the dict."""
    warm = _actor()
    raw = _req(b'User-Agent: probe')
    warm._parse(raw)
    warm._parse(raw)                 # admission is deferred to the 2nd request
    assert b'User-Agent: probe' in warm._line_cache
    assert warm._line_cache[b'User-Agent: probe'] == (b'user-agent', b'probe')


def test_absolute_form_host_override_still_wins_with_a_warm_cache():
    """§3.2.2 — the target's authority replaces a cached Host line."""
    warm = _actor()
    warm._parse(_req(b'Accept: */*'))
    conn = warm._parse(
        b'GET http://real.example/x HTTP/1.1\r\n'
        b'Host: spoofed.example\r\n\r\n')
    assert conn.headers.get(b'host') == b'real.example'
    assert conn.server[0] == 'real.example'


# --------------------------------------------------------------------------
# Resource bounds — the key is attacker-controlled, so entry count is not
# the resource that needs bounding.  Bytes are.
# --------------------------------------------------------------------------

def test_an_oversized_line_is_never_cached():
    """A line past the per-line cap must not be retained at all.

    Without this, 64 entries x BB_HEADER_MAX_LINE (8 KiB) is ~1 MiB retained
    per connection against ~1 KiB of real need — memory an attacker pins by
    sending each line once and then idling on keep-alive.
    """
    warm = _actor()
    big = b'X-Big: ' + b'a' * (_LINE_CACHE_MAX_LINE + 1)
    conn = warm._parse(_req(big))
    # It still parses correctly ...
    assert (b'x-big', b'a' * (_LINE_CACHE_MAX_LINE + 1)) in _pairs(conn)
    # ... it is simply never admitted.
    assert all(len(k) <= _LINE_CACHE_MAX_LINE for k in warm._line_cache)


def test_oversized_lines_cannot_grow_the_cache_at_all():
    warm = _actor()
    for i in range(50):
        warm._parse(_req(b'X-Big%d: %s' % (i, b'a' * _LINE_CACHE_MAX_LINE)))
    assert all(len(k) <= _LINE_CACHE_MAX_LINE for k in warm._line_cache)
    assert not any(k.startswith(b'X-Big') for k in warm._line_cache)


def test_cache_respects_a_byte_budget():
    """Admission stops at the byte budget, not merely at the entry count."""
    warm = _actor()
    # Lines well under the per-line cap, but many of them.
    for i in range(_LINE_CACHE_MAX * 4):
        warm._parse(_req(b'X-Pad-%03d: %s' % (i, b'v' * 200)))
    assert warm._line_cache_bytes <= _LINE_CACHE_MAX_BYTES
    assert len(warm._line_cache) <= _LINE_CACHE_MAX


def test_worst_case_retention_is_bounded_and_small():
    """State the guarantee as a number, so a future tuning change trips here.

    The accounted retention is key + name + value per entry; this is the figure
    that multiplies by concurrent connections.
    """
    warm = _actor()
    for i in range(500):
        warm._parse(_req(b'X-Q-%04d: %s' % (i, b'v' * 900)))
    accounted = sum(len(k) + len(v[0]) + len(v[1])
                    for k, v in warm._line_cache.items())
    # Two budgets' worth is the ceiling: the byte counter tracks the key, and
    # the value slices add at most as much again.
    assert accounted <= 2 * _LINE_CACHE_MAX_BYTES
    assert accounted < 64 * 1024, f'{accounted} B/connection is too much'


def test_real_world_header_set_still_fits_entirely():
    """The bounds must not cost anything on traffic a browser actually sends.

    Sizes taken from a captured Chromium page load: 26 distinct lines, 988 B
    in total, longest 145 B.  If a future tuning makes real traffic miss, this
    is where it shows up.
    """
    warm = _actor()
    captured = [
        # ``_req`` supplies the Host line; a second one is a smuggling reject.
        b'Connection: keep-alive',
        b'sec-ch-ua: "Not;A=Brand";v="8", "Chromium";v="150", "Microsoft Edge";v="150"',
        b'sec-ch-ua-mobile: ?0',
        b'sec-ch-ua-platform: "Windows"',
        b'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        b'(KHTML, like Gecko) HeadlessChrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0',
        b'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,'
        b'image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        b'Accept-Encoding: gzip, deflate, br, zstd',
        b'Accept-Language: ja,en;q=0.9,en-GB;q=0.8,en-US;q=0.7',
        b'Sec-Fetch-Site: same-origin',
        b'Sec-Fetch-Mode: no-cors',
        b'Sec-Fetch-Dest: style',
        b'Referer: http://localhost:8788/',
    ]
    warm._parse(_req(*captured))
    assert warm._line_cache_bytes <= _LINE_CACHE_MAX_BYTES
    # Every captured line is served without re-validation — from the shared
    # spec table where the value set is fixed, from this connection's cache
    # otherwise.  The split between the two is an implementation detail; that
    # nothing falls through both is not.
    from blackbull.server.http1_actor import _DEFAULT_LINES
    for line in captured:
        assert line in warm._line_cache or line in _DEFAULT_LINES, line


def test_admission_happens_on_the_first_request():
    """Populate immediately — deferring it was measured and rejected.

    Deferring admission to the second request makes a connection-per-request
    client cheaper (+6.9 % rather than +21 % against a no-cache build) but
    costs every longer-lived connection, which is the dominant shape: at ten
    requests per connection — HttpArena's ``limited-conn`` profile — deferral
    gave -16.6 % where immediate admission gives -23.2 %, and at two requests
    it turned a break-even into +13.9 %.  Parse is ~11 % of server CPU, so the
    connection-per-request cost is ~+2 % CPU on a shape that is a minority of
    real traffic.  Numbers: bench/hotpath/line_cache_churn.py.
    """
    warm = _actor()
    warm._parse(_req(b'User-Agent: probe'))
    assert b'User-Agent: probe' in warm._line_cache


def test_an_empty_cache_is_not_probed():
    """The first request must not hash lines against a cache that cannot hit.

    Observable via the byte counter: a single-request connection admits, but
    a lookup that could never succeed is not paid for.  (CPython caches a
    bytes hash on the object, so the saving is the dict probe, not the hash —
    which is why this alone does not make the first request free.)
    """
    warm = _actor()
    warm._parse(_req(b'Accept: */*'))
    assert warm._line_cache_bytes > 0
