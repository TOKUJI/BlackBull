"""The shared request corpus for dual-path (native vs compat) assertions.

``BB_FORCE_ASGI_SCOPE=1`` makes the server convert ``Connection -> scope`` and
the app convert it back with ``from_scope`` at dispatch, so both directions of
the ASGI conversion run on every request.  The lane only does its job if the
conversion is *invisible*, which is a claim about a set of request shapes — so
the set lives here, once, and every test that asserts "the two lanes agree"
draws from it rather than re-specifying its own vectors.

Two consumers, two depths:

- ``test_dual_path_identity`` drives every vector's raw bytes straight into
  ``HTTP1Actor`` and asserts the two lanes emit **byte-identical** responses.
- ``test_native_test_server`` replays the client-expressible subset through
  ``NativeTestServer`` over a real socket and asserts the lanes agree on what
  a real HTTP client observes.

Not every vector can be driven by a real client: the corpus deliberately
includes malformed and raw-form requests (obs-fold, a bad header name, an
absolute-form target, ``OPTIONS *``) that exist precisely because a
well-behaved client would never send them.  A vector therefore carries a
:class:`ClientSpec` only when an HTTP client can express it; ``client is None``
marks it raw-drive-only.  For the expressible ones the ``ClientSpec`` is the
definition and the raw bytes are derived from it, so the two drives cannot
drift apart.
"""
import re
from typing import NamedTuple

# The Date header is whole-second wall clock, so it may legitimately differ
# between two runs of the same request.  Nothing else may.
_DATE_RE = re.compile(rb'^date:.*$', re.IGNORECASE | re.MULTILINE)


def normalise(raw: bytes) -> bytes:
    """Blank out the one header that may legitimately differ between runs."""
    return _DATE_RE.sub(b'date: <normalised>', raw)


def _req(line: bytes, *headers: bytes, body: bytes = b'') -> bytes:
    parts = [line, b'Host: localhost', *headers]
    if body:
        parts.append(b'Content-Length: %d' % len(body))
    return b'\r\n'.join(parts) + b'\r\n\r\n' + body


class ClientSpec(NamedTuple):
    """How a real HTTP client would issue this request.

    ``headers`` are ``str`` pairs because that is what an httpx call takes;
    repeated names are allowed (the list is not a mapping).
    """
    method: str
    target: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b''


class Vector(NamedTuple):
    """One corpus entry.

    ``raw`` is what goes on the wire for the byte-identity drive.  ``client``
    is the same request expressed for a real HTTP client, or ``None`` when no
    conformant client can send it.
    """
    raw: bytes
    client: ClientSpec | None = None


def _from_client(spec: ClientSpec, *, version: bytes = b'HTTP/1.1') -> Vector:
    """Build a vector whose raw bytes are *derived* from the client spec."""
    header_lines = [f'{name}: {value}'.encode('latin-1')
                    for name, value in spec.headers]
    raw = _req(b'%s %s %s' % (spec.method.encode(), spec.target.encode(), version),
               *header_lines, body=spec.body)
    return Vector(raw=raw, client=spec)


CORPUS: dict[str, Vector] = {
    # --- ordinary routing -------------------------------------------------
    'get': _from_client(ClientSpec('GET', '/')),
    'get-query': _from_client(ClientSpec('GET', '/?a=1&b=2')),
    'get-encoded-path': _from_client(ClientSpec('GET', '/caf%C3%A9')),
    'post-body': _from_client(ClientSpec('POST', '/echo', body=b'hello')),
    'post-empty-body': _from_client(ClientSpec('POST', '/echo', body=b'')),

    # --- the method rewrite that broke ------------------------------------
    'head': _from_client(ClientSpec('HEAD', '/')),
    'head-with-headers': _from_client(ClientSpec(
        'HEAD', '/', headers=(('Accept', '*/*'), ('User-Agent', 'probe')))),

    # --- misses and rejections --------------------------------------------
    'not-found': _from_client(ClientSpec('GET', '/nope')),
    'method-not-allowed': _from_client(ClientSpec('DELETE', '/')),

    # --- header shapes ----------------------------------------------------
    'many-headers': _from_client(ClientSpec('GET', '/', headers=(
        ('User-Agent', 'Mozilla/5.0 (X11; Linux x86_64)'),
        ('Accept', 'text/html,application/xhtml+xml'),
        ('Accept-Encoding', 'gzip, deflate, br'),
        ('Accept-Language', 'en-US,en;q=0.9'),
        ('Cookie', 'session=8f14e45fceea167a5a36dedd4bea2543'),
        ('Referer', 'http://localhost/index.html')))),
    'repeated-header': _from_client(ClientSpec(
        'GET', '/', headers=(('Accept', 'a'), ('Accept', 'b')))),

    # --- raw-drive only: no conformant client can send these --------------
    # HTTP/1.0 (httpx speaks 1.1+), the server-wide ``*`` target and
    # absolute-form request targets (not expressible through a client API),
    # and four deliberately malformed shapes a client library rejects or
    # rewrites before they reach the wire.
    'get-1.0': Vector(_req(b'GET / HTTP/1.0')),
    'options-asterisk': Vector(_req(b'OPTIONS * HTTP/1.1')),
    'absolute-form': Vector(_req(b'GET http://real.example/ HTTP/1.1')),
    'bad-header-name': Vector(_req(b'GET / HTTP/1.1', b'Bad Name: v')),
    'obs-fold': Vector(_req(b'GET / HTTP/1.1', b' folded: v')),
    'bad-content-length': Vector(_req(b'POST /echo HTTP/1.1',
                                      b'Content-Length: 007')),
    'unsupported-version': Vector(_req(b'GET / HTTP/9.9')),
    'ows-padded-value': Vector(_req(b'GET / HTTP/1.1', b'Accept:    */*   ')),
}

#: Vectors a real HTTP client can issue — the subset the socket-level lane
#: agreement test replays.  The byte-identity guarantee still covers all of
#: ``CORPUS``; this is about what an external client can *observe*.
CLIENT_DRIVABLE: dict[str, ClientSpec] = {
    name: v.client for name, v in CORPUS.items() if v.client is not None
}
