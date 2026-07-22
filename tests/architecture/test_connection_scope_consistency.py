"""Phase 2 — architecture guard: ASGI scope dicts are built in one place only.

Proposal §4.4: no code outside ``blackbull/connection.py`` may construct an
ASGI scope dict literal. All scope construction must go through
``Connection.as_scope()``. This prevents future code from bypassing
``Connection`` and hand-rolling a scope dict (the drift hazard §4 exists to
prevent).

The guard is a source scan. During the Sprint 79 migration (phases 3–5) the
actors still build scope dicts; those sites are listed in
``_MIGRATION_ALLOWLIST`` and removed from it as each phase lands, so the
allowlist shrinks to empty by Phase 5. An empty allowlist with the scan
passing is the end-state proof.
"""
import ast
import pathlib

import blackbull


_PKG_ROOT = pathlib.Path(blackbull.__file__).parent

# Files still allowed to construct a raw ``{'type': 'http'|'websocket', ...}``
# scope dict. At Phase 5 close this is the end state: only ``connection.py``,
# the single sanctioned native→ASGI conversion point, may build a scope literal.
# Every other producer (H1/H2 parsers, the H2 push path, TestClient) now goes
# through ``Connection.as_scope()``.
_MIGRATION_ALLOWLIST = {
    'connection.py',            # the sanctioned conversion point
}


def _rel(p: pathlib.Path) -> str:
    return p.relative_to(_PKG_ROOT).as_posix()


def _has_scope_dict_literal(source: str) -> bool:
    """True if *source* contains a real dict literal whose first key is the
    constant ``'http'`` or ``'websocket'`` under a ``'type'`` key.

    Uses the AST so docstrings, comments, and example code are never matched —
    only genuine ``ast.Dict`` construction nodes.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == 'type'
                    and isinstance(value, ast.Constant)
                    and value.value in ('http', 'websocket')):
                return True
    return False


def test_no_scope_dict_literal_outside_connection():
    offenders = []
    for path in _PKG_ROOT.rglob('*.py'):
        rel = _rel(path)
        if rel in _MIGRATION_ALLOWLIST:
            continue
        if _has_scope_dict_literal(path.read_text(encoding='utf-8')):
            offenders.append(rel)
    assert not offenders, (
        'ASGI scope dict literals must be built only via Connection.as_scope(); '
        f'found hand-rolled scope literals in: {sorted(offenders)}. '
        'Either route the construction through Connection or (during migration) '
        'add the file to _MIGRATION_ALLOWLIST with a phase note.')


def test_migration_allowlist_entries_exist():
    """Guard against the allowlist rotting: every entry must name a real file."""
    for rel in _MIGRATION_ALLOWLIST:
        assert (_PKG_ROOT / rel).exists(), f'_MIGRATION_ALLOWLIST names a missing file: {rel}'


def test_native_baseline_request_never_materializes_scope_dict():
    """Sprint 80 exit invariant: a baseline request served by BlackBull's own
    dispatch — a bare :class:`Connection` handed to ``app`` (native path), no
    user ``app.use`` middleware, a handler taking neither ``scope`` nor
    ``Request`` — must never build an ASGI scope dict at all. BlackBull is a
    native-Connection framework: the dispatch pipeline threads ``conn`` end to
    end and only the ``BB_FORCE_ASGI_SCOPE`` / external-ASGI boundary calls
    ``as_scope``/``to_dispatch_scope``/``_scope_contents``. If any
    framework-internal hot-path code starts deriving a scope from the
    Connection, one of those methods fires and this fails."""
    import asyncio
    from blackbull import BlackBull
    from blackbull.headers import Headers
    from blackbull.connection import Connection

    app = BlackBull()

    @app.route(path='/')
    async def _hello():
        return b'ok'

    # Trip-wire: record any scope-derivation call on the Connection.
    built: list[str] = []
    spied = ('as_scope', 'to_dispatch_scope', '_scope_contents')
    originals = {name: getattr(Connection, name) for name in spied}

    def _make_spy(name, orig):
        def _spy(self, *a, **kw):
            built.append(name)
            return orig(self, *a, **kw)
        return _spy

    for name, orig in originals.items():
        setattr(Connection, name, _make_spy(name, orig))

    try:
        conn = Connection(type='http', http_version='1.1', method='GET', scheme='http',
                          path='/', raw_path=b'/', query_string=b'', root_path='',
                          headers=Headers([(b'host', b'localhost')]),
                          client=('127.0.0.1', 5), server=('localhost', 80), extensions={})
        sent = []

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(ev):
            sent.append(ev)

        # Native entry: hand the Connection straight to the app (as the actor does).
        asyncio.run(app(conn, receive, send))
    finally:
        for name, orig in originals.items():
            setattr(Connection, name, orig)

    assert built == [], (
        'Sprint 80 regression: a native baseline request derived an ASGI scope '
        f'dict via {built!r}. Some framework-internal code built a scope instead '
        'of reading the Connection on the hot path.')
    start = [e for e in sent if e.get('type') == 'http.response.start']
    assert start and start[0]['status'] == 200
