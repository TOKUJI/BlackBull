"""The native `Connection` interface — BlackBull's single internal request
representation (Sprint 79, proposal `native-connection-interface.md`).

BlackBull is a multi-protocol server that owns both sides of the wire on its
self-hosted path; the ASGI ``scope`` dict is therefore an *internal
data-format choice*, not an interoperability contract — and a poor one
(untyped, string-keyed, carrying private ``_``-prefixed keys). This module
makes a typed :class:`Connection` the internal model; the ASGI scope becomes
a **derived** view produced by :meth:`Connection.as_scope` and consumed by
:meth:`Connection.from_scope`, used only where external compatibility needs
it (uvicorn, ``httpx.ASGITransport``/TestClient, third-party ASGI middleware).

The `_CONNECTION_FIELDS` registry is the single source of truth from which
both conversions are generated: adding a field to :class:`Connection` without
a registry entry is a test failure (proposal §4.2 / §9.5), which mechanically
prevents the two representations from drifting apart.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple

from .headers import Headers
from .request import read_body, parse_cookies, ClientDisconnected, _json_or_none

__all__ = ['Connection', 'ClientDisconnected', 'CONNECTION_STASH_KEY']


#: Envelope key under which a protocol actor stashes the typed :class:`Connection`
#: it parsed, so the self-hosted dispatch path (dispatcher, router, handlers, and
#: ``TrustedProxy``) reads it back with **zero** re-conversion (Sprint 79 Phase 5).
#: A single named constant instead of a bare ``'_connection'`` literal repeated
#: across the actors, app, router, and proxy: one typo on a write-side would
#: otherwise silently miss the stash and force a redundant ``from_scope`` on the
#: hot path, invisible to tests.
CONNECTION_STASH_KEY = '_connection'


# ---------------------------------------------------------------------------
# Field registry — the single source of truth (proposal §4.2, NON-NEGOTIABLE)
# ---------------------------------------------------------------------------

class _FieldSpec(NamedTuple):
    """One `Connection` field's relationship to the ASGI scope.

    ``scope_key is None`` marks a **Connection-only** field (``path_params``,
    ``connection_id``, and the private caches) — present on the object but not
    part of the derived ASGI scope, so the conversions skip it.
    """
    attr: str
    scope_key: str | None
    to_scope: Callable[[Any], Any] | None
    from_scope: Callable[[Any], Any] | None


def _identity(v: Any) -> Any:
    return v


def _headers_to_scope(h: Headers) -> list:
    return list(h)                       # Headers iterates as (name, value) pairs


def _headers_from_scope(v: Any) -> Headers:
    return v if isinstance(v, Headers) else Headers(v)


def _tuple_to_list(v):
    return list(v) if v is not None else None


def _list_to_tuple(v):
    return tuple(v) if v is not None else None


# Order here is also the emission order of as_scope() (minus the leading
# generated ``asgi`` key). Keep it stable.
_CONNECTION_FIELDS: list[_FieldSpec] = [
    # -- scope-mapped: round-trip through as_scope() / from_scope() ----------
    _FieldSpec('type',         'type',         _identity, _identity),
    _FieldSpec('http_version', 'http_version', _identity, _identity),
    _FieldSpec('method',       'method',       _identity, _identity),
    _FieldSpec('scheme',       'scheme',       _identity, _identity),
    _FieldSpec('path',         'path',         _identity, _identity),
    _FieldSpec('raw_path',     'raw_path',     _identity, _identity),
    _FieldSpec('query_string', 'query_string', _identity, _identity),
    _FieldSpec('root_path',    'root_path',    _identity, _identity),
    _FieldSpec('headers',      'headers',      _headers_to_scope, _headers_from_scope),
    _FieldSpec('client',       'client',       _tuple_to_list, _list_to_tuple),
    _FieldSpec('server',       'server',       _tuple_to_list, _list_to_tuple),
    _FieldSpec('state',        'state',        _identity, _identity),
    _FieldSpec('extensions',   'extensions',   _identity, _identity),
    # -- Connection-only: not part of the ASGI scope (scope_key=None) --------
    _FieldSpec('path_params',    None, None, None),   # set by the router
    _FieldSpec('connection_id',  None, None, None),   # opaque id, set at accept
    _FieldSpec('_asterisk_form', None, None, None),   # H1 OPTIONS * marker
    _FieldSpec('_body',          None, None, None),   # body cache
    _FieldSpec('_body_read',     None, None, None),   # body-drained flag
    _FieldSpec('_cookies',       None, None, None),   # parsed-cookies cache
    _FieldSpec('_receive',       None, None, None),   # ASGI receive channel
]


def _scope_fields() -> list[_FieldSpec]:
    """Registry entries that participate in the ASGI scope round-trip."""
    return [s for s in _CONNECTION_FIELDS if s.scope_key is not None]


def stashed_connection(scope, receive) -> tuple['Connection', bool]:
    """Return the typed :class:`Connection` for this request, plus whether it
    was freshly built.

    The one *scope → Connection* accessor shared by the dispatcher
    (``app._connection_of``) and the router (``router._conn_of``), Sprint 79.
    BlackBull's own protocol actors stash the ``Connection`` they parsed on the
    scope envelope under :data:`CONNECTION_STASH_KEY`, so the self-hosted path
    reads it back with **no** re-conversion. Under an external ASGI server
    (uvicorn, ``httpx.ASGITransport``) there is no stash, so build one via
    :meth:`Connection.from_scope` — the single ASGI→native point — and stash it
    so later accessors in the same request reuse the one object.

    Returns ``(conn, built)`` where *built* is ``True`` only on the external
    path (no prior stash). Callers layer their own post-processing on a freshly
    built conn — the dispatcher links ``scope['state']`` to ``conn.state``; the
    router seeds ``path_params`` from an input scope key — because those needs
    differ by call site.
    """
    conn = scope.get(CONNECTION_STASH_KEY) if isinstance(scope, dict) else None
    if conn is not None:
        return conn, False
    conn = Connection.from_scope(scope, receive)
    if isinstance(scope, dict):
        scope[CONNECTION_STASH_KEY] = conn
    return conn, True


# ---------------------------------------------------------------------------
# The Connection object
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Connection:
    """One HTTP (or WebSocket) request — the single internal representation.

    Built by the protocol actor, consumed by the router, dispatcher,
    middleware, and handlers. The ASGI ``scope`` dict is a *derived* view
    (:meth:`as_scope`). ``Request`` is a deprecated alias of this class.
    """

    # -- request identity (always set by the parser) ----------------------
    method: str
    path: str
    raw_path: bytes
    headers: Headers                     # always a Headers; never a raw list

    # -- identity present in any conformant ASGI http scope ----------------
    query_string: bytes = b''
    http_version: str = '1.1'
    scheme: str = 'http'

    # -- transport (filled after parse) -----------------------------------
    # ASGI 3.0 allows the port to be ``None`` (e.g. a Unix-socket server, or
    # httpx.ASGITransport which sends ``server: ['testserver', None]``), so the
    # port element is ``int | None``.
    client: tuple[str, int | None] | None = None
    server: tuple[str, int | None] | None = None

    # -- mutable per-request state ----------------------------------------
    state: dict[str, Any] = field(default_factory=dict)
    path_params: dict[str, str] = field(default_factory=dict)
    root_path: str = ''
    type: str = 'http'
    extensions: dict[str, dict] = field(default_factory=dict)
    connection_id: str = ''

    # -- internal (not part of the ASGI scope; excluded from equality) -----
    _asterisk_form: bool = field(default=False, repr=False)
    _body: bytes | None = field(default=None, compare=False, repr=False)
    _body_read: bool = field(default=False, compare=False, repr=False)
    _cookies: dict[str, str] | None = field(default=None, compare=False, repr=False)
    # The ASGI ``receive`` channel, bound by the actor / ``from_scope``; only
    # ever a receive callable or ``None`` (before body access is wired).
    _receive: Callable | None = field(default=None, compare=False, repr=False)

    # ---- ASGI conversion — generated from _CONNECTION_FIELDS --------------

    def as_scope(self) -> dict:
        """Generate a fresh ASGI 3.0 scope dict (the single native→ASGI point).

        Mutations of the returned dict do not affect the Connection, except
        that ``state`` and ``extensions`` are shared by reference so a
        buffering middleware's writes reach the handler.
        """
        scope: dict = {'asgi': {'version': '3.0', 'spec_version': '2.2'}}
        for spec in _scope_fields():
            scope[spec.scope_key] = spec.to_scope(getattr(self, spec.attr))
        return scope

    @classmethod
    def from_scope(cls, scope: dict, receive: Any = None) -> 'Connection':
        """Build a Connection from an external ASGI scope (the single
        ASGI→native point). Unknown keys are ignored; missing optional keys
        fall back to the field defaults."""
        kwargs: dict[str, Any] = {}
        for spec in _scope_fields():
            if spec.scope_key in scope:
                kwargs[spec.attr] = spec.from_scope(scope[spec.scope_key])
        # A conformant ASGI http/websocket scope always carries method, path,
        # and headers, but default them so ``from_scope`` is total and never
        # raises on a partial scope (e.g. a hand-built one in a unit test, or a
        # minimal lifespan-adjacent scope).
        kwargs.setdefault('method', 'GET')
        kwargs.setdefault('path', '')
        if 'headers' not in kwargs:
            kwargs['headers'] = Headers([])
        # ASGI ``raw_path`` is optional; default it to the encoded path.
        if 'raw_path' not in kwargs:
            kwargs['raw_path'] = kwargs.get('path', '').encode('utf-8')
        conn = cls(**kwargs)
        conn._receive = receive
        return conn

    def to_dispatch_scope(self, *, force_asgi: bool = False) -> dict:
        """Materialize the ASGI scope the dispatch pipeline consumes, with this
        typed :class:`Connection` stashed on it for zero-reconversion reads.

        The single canonical *Connection → dispatch-ready scope* bridge shared
        by the H/1.1 ``run()`` and H/2 ``_conn_to_scope`` seams (Sprint 79) —
        they used to hand-roll the same five steps:

        1. derive the ASGI scope via :meth:`as_scope` (the one native→ASGI point);
        2. when ``force_asgi`` (the §4.3 ``BB_FORCE_ASGI_SCOPE`` dual-path lane),
           round-trip through :meth:`from_scope` so both the derived scope *and*
           the Connection the consumers read are rebuilt from scratch on every
           request, keeping the compat conversion from bitrotting.  ``_asterisk_form``
           is Connection-only (not in the scope), so carry it across the rebuild;
        3. restore ``scope['headers']`` to the rich :class:`Headers` object
           (``as_scope`` emits the ASGI ``list[tuple]`` form; internal ``.get()``
           callers want the object);
        4. re-expose the H/1.1 ``_asterisk_form`` OPTIONS marker on the envelope;
        5. stash the Connection under :data:`CONNECTION_STASH_KEY`.

        Protocol-specific augmentation (the websocket-only ``subprotocols`` key)
        is layered on by the caller *after* this returns — it is not a
        :class:`Connection` field (proposal §2.1).
        """
        scope = self.as_scope()
        conn = self
        if force_asgi:
            rebuilt = Connection.from_scope(scope)
            rebuilt._asterisk_form = self._asterisk_form
            conn = rebuilt
            scope = conn.as_scope()
        scope['headers'] = conn.headers
        if conn._asterisk_form:
            scope['_asterisk_form'] = True
        scope[CONNECTION_STASH_KEY] = conn
        return scope

    # ---- body access (single-drain cache) --------------------------------

    async def body(self) -> bytes:
        """Return the complete request body, draining ``receive`` at most once.

        Repeated calls (and :meth:`json` / :meth:`text`) return the cached
        bytes. A mid-body disconnect raises :class:`ClientDisconnected`.
        """
        if not self._body_read:
            self._body = await read_body(self._receive)
            self._body_read = True
        return self._body

    async def json(self) -> Any:
        """Parse the cached body as JSON (``None`` on empty/invalid input)."""
        return _json_or_none(await self.body())

    async def text(self, encoding: str = 'utf-8') -> str:
        """Decode the cached body as text (``errors='replace'``)."""
        return (await self.body()).decode(encoding, errors='replace')

    @property
    def cookies(self) -> dict[str, str]:
        """Cookies from the ``Cookie`` header, parsed once and cached."""
        if self._cookies is None:
            self._cookies = parse_cookies({'headers': self.headers})
        return self._cookies
