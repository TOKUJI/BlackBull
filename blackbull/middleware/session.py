"""Signed-cookie session middleware (HMAC-SHA256).

The session payload lives entirely in the cookie sent to the client; the
server keeps no per-session state.  Every value the client could otherwise
tamper with is HMAC-signed by a secret the server alone knows, so:

* a client that modifies the cookie sees the next request treated as a
  fresh empty session (signature check fails),
* the server never reads, writes, or replays anything else — no DB hit,
  no Redis round-trip, no in-process dict to keep consistent across
  workers,
* sessions survive worker restarts / horizontal scaling for free
  (any worker that knows the secret can validate any cookie).

The trade-offs compared with server-side session stores:

* Cookie size is bounded (browsers cap to ~4 KiB, often less in
  practice).  Sessions storing large objects don't fit.
* No server-side invalidation: revoking a cookie before it expires
  requires either rotating the secret (kills every session) or a
  separate revocation list.  Acceptable for many apps.

Wire format (Base64URL-encoded payload + "." + hex MAC)::

    <urlsafe-b64-no-padding(json-bytes)>.<hex-hmac-sha256(json-bytes)>

The cookie is dispatched on every response that touched ``scope['session']``
in a way that modified it (see :class:`_SessionDict`); unmodified sessions
trigger no Set-Cookie header, so a 304 / cache-friendly response stays
cache-friendly.

Usage::

    from blackbull.middleware import Session

    app.use(Session())                       # reads BB_SESSION_SECRET
    app.use(Session(secret=b'\\x...'))       # explicit secret

    @app.route(path='/')
    async def index(scope, receive, send):
        scope['session']['user'] = 'alice'             # writes Set-Cookie
        await send({'type': 'http.response.start', ...})
        await send({'type': 'http.response.body', ...})
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

from ..server.constants import ASGIEvent
from .utils import middleware

logger = logging.getLogger(__name__)


class _SessionDict(dict):
    """``dict`` subclass that flips a flag the moment it's mutated.

    The session middleware uses ``_modified`` to decide whether to emit
    ``Set-Cookie`` on the response; an untouched session — even one we
    successfully read off the request — leaves the response alone.
    """
    __slots__ = ('_modified',)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._modified = False

    def __setitem__(self, key, value):
        self._modified = True
        super().__setitem__(key, value)

    def __delitem__(self, key):
        self._modified = True
        super().__delitem__(key)

    def clear(self):
        if self:
            self._modified = True
        super().clear()

    def pop(self, key, *default):
        if key in self:
            self._modified = True
        return super().pop(key, *default)

    def popitem(self):
        self._modified = True
        return super().popitem()

    def update(self, *args, **kwargs):
        # Only flip the flag if the update actually changes contents.
        before = dict(self)
        super().update(*args, **kwargs)
        if self != before:
            self._modified = True

    def setdefault(self, key, default=None):
        if key not in self:
            self._modified = True
        return super().setdefault(key, default)


# RFC 6265 §4.1.2 — SameSite values.  ``None`` (Python None) means we
# don't emit the attribute at all (browser defaults to ``Lax`` in modern
# browsers anyway).
_VALID_SAMESITE = ('Strict', 'Lax', 'None')


@middleware
class Session:
    """ASGI middleware that maintains a signed-cookie session.

    Parameters
    ----------
    secret:
        The HMAC key.  Bytes or str.  When omitted, ``BB_SESSION_SECRET``
        is read from the environment.  A missing / empty secret raises at
        construction — no insecure default.
    cookie_name:
        Name of the cookie carrying the session payload.  Default
        ``'session'``.
    max_age:
        Cookie ``Max-Age`` in seconds.  When set, the cookie is signed
        with a server-side timestamp; values older than ``max_age``
        seconds are treated as expired (empty session).  ``None`` means
        a session cookie that lives only as long as the browser is open.
    secure:
        Set the ``Secure`` attribute so the cookie is only sent over
        HTTPS.  Default ``True``; set ``False`` for local-only dev.
    httponly:
        Set the ``HttpOnly`` attribute (JavaScript can't read the
        cookie).  Default ``True``.
    samesite:
        ``Strict`` / ``Lax`` / ``None``.  Default ``'Lax'``.
    path:
        Cookie ``Path``.  Default ``/``.
    """
    # 4 KiB is the practical browser cap on a single cookie; we warn
    # (don't reject) above this so a slowly-growing session doesn't fail
    # silently in production.
    _COOKIE_WARN_SIZE = 4000

    def __init__(
        self,
        secret: bytes | str | None = None,
        *,
        cookie_name: str = 'session',
        max_age: int | None = None,
        secure: bool = True,
        httponly: bool = True,
        samesite: str | None = 'Lax',
        path: str = '/',
    ):
        if secret is None:
            secret = os.environ.get('BB_SESSION_SECRET')
        if not secret:
            raise RuntimeError(
                'Session middleware requires a secret.  Either pass '
                'secret=... to the constructor or set the BB_SESSION_SECRET '
                'environment variable.  Generate one with e.g. '
                '``python -c "import secrets; print(secrets.token_urlsafe(32))"``.'
            )
        if samesite is not None and samesite not in _VALID_SAMESITE:
            raise ValueError(
                f'samesite must be one of {_VALID_SAMESITE} or None; '
                f'got {samesite!r}')
        self._secret = secret if isinstance(secret, bytes) else secret.encode()
        self._cookie_name = cookie_name
        self._cookie_name_b = cookie_name.encode()
        self._max_age = max_age
        self._secure = secure
        self._httponly = httponly
        self._samesite = samesite
        self._path = path

    async def __call__(self, scope, receive, send, call_next):
        # Only http and websocket scopes carry cookies; everything else
        # (lifespan, etc.) passes through untouched.
        if scope.get('type') not in ('http', 'websocket'):
            await call_next(scope, receive, send)
            return

        session = _SessionDict(self._load(scope))
        scope['session'] = session

        # We intercept http.response.start to inject Set-Cookie when (and
        # only when) the session was modified during request handling.
        # WebSocket scopes get a read-only session view — there's no
        # response.start to mutate.
        if scope.get('type') == 'websocket':
            await call_next(scope, receive, send)
            return

        async def wrapped_send(event):
            # The ``@middleware`` class decorator normalises ``call_next`` so
            # any Response / JSONResponse from the handler arrives here as a
            # plain dict event.
            if (event.get('type') == ASGIEvent.HTTP_RESPONSE_START
                    and session._modified):
                headers = list(event.get('headers', []))
                cookie = self._make_cookie(session)
                if len(cookie) > self._COOKIE_WARN_SIZE:
                    logger.warning(
                        'session cookie is %d bytes — most browsers cap at '
                        '~4 KiB.  Consider moving large state to a server-side '
                        'store.', len(cookie))
                headers.append((b'set-cookie', cookie))
                event = {**event, 'headers': headers}
            await send(event)

        await call_next(scope, receive, wrapped_send)

    # ----- read --------------------------------------------------------

    def _load(self, scope) -> dict:
        cookie_header = self._cookie_header(scope)
        if not cookie_header:
            return {}
        cookies = _parse_cookie_header(cookie_header)
        raw = cookies.get(self._cookie_name_b)
        if raw is None:
            return {}
        return self._decode(raw)

    @staticmethod
    def _cookie_header(scope) -> bytes:
        for name, value in scope.get('headers', []):
            if name.lower() == b'cookie':
                return value
        return b''

    def _decode(self, raw: bytes) -> dict:
        """Verify the HMAC and decode the JSON payload.  Empty dict on any failure."""
        payload_b64, sep, mac_hex = raw.partition(b'.')
        if not sep or not payload_b64 or not mac_hex:
            return {}
        try:
            # Re-add base64 padding (we stripped it on encode) and decode.
            pad = b'=' * (-len(payload_b64) % 4)
            payload = base64.urlsafe_b64decode(payload_b64 + pad)
        except (ValueError, binascii_error_class()):
            return {}
        expected = hmac.new(self._secret, payload, hashlib.sha256).hexdigest().encode()
        # Constant-time comparison so we don't leak length / prefix info.
        if not hmac.compare_digest(expected, mac_hex):
            return {}
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        # When max_age is configured, the payload carries a timestamp we
        # signed; reject if too old.  Removed before returning so apps
        # don't see ``_ts`` in their session dict.
        if self._max_age is not None:
            ts = data.pop('_ts', None)
            if not isinstance(ts, (int, float)) or time.time() - ts > self._max_age:
                return {}
        return data

    # ----- write -------------------------------------------------------

    def _encode(self, session: dict) -> bytes:
        data = dict(session)
        if self._max_age is not None:
            data['_ts'] = int(time.time())
        payload = json.dumps(data, separators=(',', ':'), sort_keys=True).encode()
        mac = hmac.new(self._secret, payload, hashlib.sha256).hexdigest().encode()
        payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b'=')
        return payload_b64 + b'.' + mac

    def _make_cookie(self, session: dict) -> bytes:
        """Build the Set-Cookie value, including all attribute flags."""
        if session:
            value = self._encode(session)
        else:
            # An emptied session → tell the browser to drop the cookie.
            # Max-Age=0 + Expires in the past = robust deletion across browsers.
            value = b''
        parts = [self._cookie_name_b + b'=' + value]
        parts.append(b'Path=' + self._path.encode())
        if not session:
            # Tombstone: zero lifetime so the browser deletes it.
            parts.append(b'Max-Age=0')
        elif self._max_age is not None:
            parts.append(b'Max-Age=' + str(self._max_age).encode())
        if self._secure:
            parts.append(b'Secure')
        if self._httponly:
            parts.append(b'HttpOnly')
        if self._samesite is not None:
            parts.append(b'SameSite=' + self._samesite.encode())
        return b'; '.join(parts)


# ---------------------------------------------------------------------------
# Cookie header parsing (RFC 6265 §4.2.1 — tolerant of real-world clients)
# ---------------------------------------------------------------------------

def _parse_cookie_header(header: bytes) -> dict[bytes, bytes]:
    """Decode a ``Cookie:`` request header into ``{name: value}`` pairs.

    Strict RFC 6265 grammar disallows whitespace in cookie values; in
    practice clients send arbitrary opaque bytes here and the server is
    expected to be lenient.  We split on ``;`` then on the first ``=``,
    trim surrounding whitespace, and return the leftmost occurrence of
    each name (per §5.4 step 11).
    """
    out: dict[bytes, bytes] = {}
    for piece in header.split(b';'):
        piece = piece.strip()
        if not piece or b'=' not in piece:
            continue
        name, _, value = piece.partition(b'=')
        name = name.strip()
        if name not in out:
            out[name] = value.strip()
    return out


def binascii_error_class():
    """Return ``binascii.Error`` lazily — avoids a top-level import."""
    import binascii  # noqa: PLC0415
    return binascii.Error
