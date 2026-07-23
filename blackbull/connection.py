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

__all__ = ['Connection', 'ClientDisconnected', 'CONNECTION_STASH_KEY',
           'disconnected', 'mark_disconnected']


def disconnected(target) -> bool:
    """True if the client disconnected mid-request. Accepts either a native
    :class:`Connection` (the ``app(conn, …)`` path) or an ASGI ``scope`` dict
    (the ``BB_FORCE_ASGI_SCOPE`` / external-server path)."""
    if isinstance(target, Connection):
        return target._disconnected
    return bool(target.get('_disconnected'))


def mark_disconnected(target) -> None:
    """Record a mid-request client disconnect on a :class:`Connection` or an
    ASGI ``scope`` dict (idempotent — caller guards on :func:`disconnected`)."""
    if isinstance(target, Connection):
        target._disconnected = True
    else:
        target['_disconnected'] = True


#: Envelope key under which a protocol actor stashes the typed :class:`Connection`
#: it parsed, so the self-hosted dispatch path (dispatcher, router, handlers, and
#: ``TrustedProxy``) reads it back with **zero** re-conversion (Sprint 79 Phase 5).
#: A single named constant instead of a bare ``'_connection'`` literal repeated
#: across the actors, app, router, and proxy: one typo on a write-side would
#: otherwise silently miss the stash and force a redundant ``from_scope`` on the
#: hot path, invisible to tests.
CONNECTION_STASH_KEY = '_connection'

#: Shared, never-mutated ASGI info sub-dict for the native dispatch scope
#: (Sprint 80 Tier-1b). ``as_scope()`` still emits a fresh copy for the external
#: compat boundary; only the self-hosted ``to_asgi_scope`` fast path reuses
#: this constant to save one dict allocation per request. ASGI consumers read
#: ``scope['asgi']['version']`` but never mutate the sub-dict.
_DISPATCH_ASGI_INFO = {'version': '3.0', 'spec_version': '2.2'}


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
    _FieldSpec('_path_params',   None, None, None),   # lazy; exposed via the path_params property
    _FieldSpec('connection_id',  None, None, None),   # opaque id, set at accept
    _FieldSpec('_asterisk_form', None, None, None),   # H1 OPTIONS * marker
    _FieldSpec('_body',          None, None, None),   # body cache
    _FieldSpec('_body_read',     None, None, None),   # body-drained flag
    _FieldSpec('_cookies',       None, None, None),   # parsed-cookies cache
    _FieldSpec('_receive',       None, None, None),   # ASGI receive channel
    _FieldSpec('_disconnected',  None, None, None),   # client-disconnect flag (actor→app)
]


#: The scope-mapped registry entries, computed **once** at module load — the
#: ASGI round-trip subset of ``_CONNECTION_FIELDS``. Rebuilding this filtered
#: list on every ``as_scope()``/``from_scope()`` call was a measurable per-request
#: cost on the self-hosted hot path (Sprint 80 Tier-1), so it is a constant.
_SCOPE_FIELDS: list[_FieldSpec] = [s for s in _CONNECTION_FIELDS if s.scope_key is not None]


def _scope_fields() -> list[_FieldSpec]:
    """Registry entries that participate in the ASGI scope round-trip."""
    return _SCOPE_FIELDS


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
    # Native path (Sprint 80): the actor/app hands the typed Connection straight
    # through, so ``scope`` *is* the Connection — return it, nothing to build.
    if isinstance(scope, Connection):
        return scope, False
    conn = scope.get(CONNECTION_STASH_KEY) if isinstance(scope, dict) else None
    if conn is not None:
        return conn, False
    conn = Connection.from_scope(scope, receive)
    if isinstance(scope, dict):
        scope[CONNECTION_STASH_KEY] = conn
    return conn, True


def bind_receive_channel(scope, receive) -> None:
    """Bind the **raw** body-receive channel onto the request's Connection so
    lazy ``conn.body()`` / ``request.body()`` drain the right stream once.

    Called by the protocol actor with the *unwrapped* recipient — never with a
    disconnect-detecting wrapper. Storing the wrapper would form a per-request
    reference cycle: ``conn._receive`` → wrapper → (closure captures) ``conn``.
    The refcount of a cyclic group never reaches zero when the request's local
    refs drop, so reclamation is deferred to the generational cyclic GC — whose
    periodic pauses were the v0.60.0 tail-latency regression (see
    ``.claude/planning/research/v0600-regression-investigation.md`` §6). The raw
    recipient does **not** reference ``conn`` (HTTP/1.1 keeps only the path
    string; HTTP/2 keeps none), so ``conn`` → recipient is an acyclic chain that
    refcounting frees the instant the request ends.

    Idempotent — binds only when unset, so the external-ASGI path (uvicorn /
    ``httpx.ASGITransport``), where :meth:`Connection.from_scope` already bound
    the host's own receive channel, keeps that binding.
    """
    conn = scope if isinstance(scope, Connection) else (
        scope.get(CONNECTION_STASH_KEY) if isinstance(scope, dict) else None)
    if conn is not None and conn._receive is None:
        conn._receive = receive


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
    # Lazy: no dict is allocated until the router actually matches path params
    # (Sprint 80 Tier-1b). No-param routes — the framework-bound hot profiles
    # (baseline/pipelined/limited-conn) — never touch it, saving one dict per
    # request. Exposed via the ``path_params`` property below; excluded from
    # equality (a transient routing detail, never an identity input).
    # Values are ``Any``: the router's type converters coerce matched segments
    # to the annotated type (``{id:int}`` → ``int``), so the dict is not str→str.
    _path_params: dict[str, Any] | None = field(default=None, compare=False, repr=False)
    root_path: str = ''
    type: str = 'http'
    extensions: dict[str, dict] = field(default_factory=dict)
    connection_id: str = ''

    # -- internal (not part of the ASGI scope; excluded from equality) -----
    _asterisk_form: bool = field(default=False, repr=False)
    _body: bytes | None = field(default=None, compare=False, repr=False)
    _body_read: bool = field(default=False, compare=False, repr=False)
    _cookies: dict[str, str] | None = field(default=None, compare=False, repr=False)
    # The **raw** ``receive`` channel (recipient), bound by the actor via
    # ``bind_receive_channel`` / by ``from_scope`` on the external path; only
    # ever a receive callable or ``None`` (before body access is wired). Must
    # stay the unwrapped recipient — a disconnect-detecting wrapper captures
    # ``conn`` and would form a per-request reference cycle (v0.60.0 regression).
    _receive: Callable | None = field(default=None, compare=False, repr=False)
    # Set by the actor's disconnect-detecting receive wrapper when the client
    # drops mid-request; read by ``BlackBull.__call__`` to skip the terminal
    # ``request_completed`` event. Lives on the Connection (not the scope) so the
    # native ``app(conn, …)`` path shares it across the actor↔app boundary.
    _disconnected: bool = field(default=False, compare=False, repr=False)

    # ---- path params (lazy — allocated on first access) ------------------

    @property
    def path_params(self) -> dict[str, Any]:
        """Matched URL path params, set by the router (values are converter-
        coerced, hence ``Any``). The backing dict is created lazily on first
        access so no-param routes allocate nothing."""
        if self._path_params is None:
            self._path_params = {}
        return self._path_params

    @path_params.setter
    def path_params(self, value: dict[str, Any]) -> None:
        self._path_params = value

    # ---- ASGI conversion — generated from _CONNECTION_FIELDS --------------

    def as_scope(self) -> dict:
        """Generate a fresh ASGI 3.0 scope dict (the single native→ASGI point).

        Mutations of the returned dict do not affect the Connection, except
        that ``state`` and ``extensions`` are shared by reference so a
        buffering middleware's writes reach the handler.
        """
        scope: dict = {'asgi': {'version': '3.0', 'spec_version': '2.2'}}
        for spec in _SCOPE_FIELDS:
            scope[spec.scope_key] = spec.to_scope(getattr(self, spec.attr))
        return scope

    @classmethod
    def from_scope(cls, scope: dict, receive: Any = None) -> 'Connection':
        """Build a Connection from an external ASGI scope (the single
        ASGI→native point). Unknown keys are ignored; missing optional keys
        fall back to the field defaults."""
        kwargs: dict[str, Any] = {}
        for spec in _SCOPE_FIELDS:
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

    def to_asgi_scope(self, *, force_asgi: bool = False) -> dict:
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

        Sprint 80 Tier-1: the default (``force_asgi=False``) native path builds
        the scope by **direct attribute access**, placing the rich ``Headers``
        object straight in (no ``list(headers)`` that the old code computed via
        the registry and then immediately discarded), and skips the per-field
        function-call indirection of ``as_scope()``. The ``force_asgi`` dual-path
        conformance lane (§4.3) keeps the full ``as_scope`` → ``from_scope`` →
        ``as_scope`` round-trip so the compat conversion is still exercised.
        """
        if force_asgi:
            # Dual-path conformance lane (§4.3): emit a **pure** ASGI scope — the
            # exact shape an external server (uvicorn) delivers — with NO stashed
            # Connection. The server's job ends at ``Connection → scope``; the app
            # then does ``scope → Connection`` via ``from_scope`` at dispatch,
            # exercising *both* conversion directions on every request. ``headers``
            # stays the ASGI list-of-tuples form (``as_scope``); the app normalizes
            # it to :class:`Headers` at its entry, as it does for real uvicorn input.
            scope = self.as_scope()
            if self._asterisk_form:
                scope['_asterisk_form'] = True
            return scope

        # Native hot path — direct build, Headers object placed as-is.
        scope = self._scope_contents()
        scope[CONNECTION_STASH_KEY] = self
        return scope

    def _scope_contents(self) -> dict:
        """The ASGI scope key/values derived from this Connection, **without**
        the stashed-Connection key. Direct attribute access (no registry
        indirection); ``state``/``extensions`` are shared by reference so a
        buffering middleware's writes reach the handler; ``headers`` is the rich
        :class:`Headers` object (internal ``.get()`` callers want it). The one
        place a dispatch scope's key/values are assembled, used by
        ``to_asgi_scope`` for the ``BB_FORCE_ASGI_SCOPE`` / external boundary."""
        client = self.client
        server = self.server
        scope = {
            'asgi': _DISPATCH_ASGI_INFO,
            'type': self.type,
            'http_version': self.http_version,
            'method': self.method,
            'scheme': self.scheme,
            'path': self.path,
            'raw_path': self.raw_path,
            'query_string': self.query_string,
            'root_path': self.root_path,
            'headers': self.headers,
            'client': list(client) if client is not None else None,
            'server': list(server) if server is not None else None,
            'state': self.state,
            'extensions': self.extensions,
        }
        if self._asterisk_form:
            scope['_asterisk_form'] = True
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

