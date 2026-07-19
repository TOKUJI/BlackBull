"""Phase 2 — `Connection` core + the drift-prevention harness (Sprint 79).

`Connection` (blackbull/connection.py) is the single internal request
representation; the ASGI scope dict is a *derived* view produced by
`as_scope()` and consumed by `from_scope()`. Both conversions are generated
from the `_CONNECTION_FIELDS` registry — the single source of truth. These
tests pin:

* the registry covers every dataclass field (adding a field without a
  registry entry is a failure — proposal §4.2 / §9.5, NON-NEGOTIABLE);
* `Connection → as_scope() → from_scope()` is identity (§4.1);
* `from_scope() → as_scope()` preserves every ASGI-mandated key (§4.1);
* body/json/text/cookies behave like the old `Request` (single drain).
"""
import dataclasses
from http import HTTPStatus  # noqa: F401 (kept for parity with sibling tests)

import pytest

from blackbull.connection import (
    Connection, _CONNECTION_FIELDS, _scope_fields, ClientDisconnected,
)
from blackbull.headers import Headers


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _stub_receive(events=None):
    """A receive() returning the given ASGI events then a final empty body."""
    queue = list(events or [{'type': 'http.request', 'body': b'', 'more_body': False}])

    async def receive():
        return queue.pop(0) if queue else {'type': 'http.disconnect'}

    return receive


def _sample_connection() -> Connection:
    return Connection(
        method='POST',
        path='/api/items',
        raw_path=b'/api/items',
        query_string=b'q=hello&page=2',
        http_version='1.1',
        scheme='https',
        headers=Headers([(b'host', b'example.com'),
                         (b'content-type', b'application/json')]),
        client=('203.0.113.7', 54321),
        server=('10.0.0.1', 443),
        root_path='',
    )


# The ASGI keys as_scope() must always emit for an http scope.
_ASGI_MANDATED_HTTP_SCOPE_KEYS = {
    'type', 'asgi', 'http_version', 'method', 'scheme', 'path', 'raw_path',
    'query_string', 'headers', 'client', 'server',
}


# ---------------------------------------------------------------------------
# Field registry — the non-negotiable drift guard (§4.2 / §9.5)
# ---------------------------------------------------------------------------

class TestFieldRegistry:
    def test_every_dataclass_field_has_a_registry_entry(self):
        registry_attrs = {spec.attr for spec in _CONNECTION_FIELDS}
        dataclass_attrs = {f.name for f in dataclasses.fields(Connection)}
        missing = dataclass_attrs - registry_attrs
        assert not missing, (
            f'Connection fields with no _CONNECTION_FIELDS entry: {sorted(missing)}. '
            f'Every field must be registered (proposal §4.2 / §9.5).')

    def test_no_orphan_registry_entries(self):
        registry_attrs = {spec.attr for spec in _CONNECTION_FIELDS}
        dataclass_attrs = {f.name for f in dataclasses.fields(Connection)}
        orphans = registry_attrs - dataclass_attrs
        assert not orphans, f'_CONNECTION_FIELDS entries for non-fields: {sorted(orphans)}'

    def test_scope_mapped_entries_have_conversion_pair(self):
        for spec in _scope_fields():
            assert callable(spec.to_scope), f'{spec.attr}: to_scope not callable'
            assert callable(spec.from_scope), f'{spec.attr}: from_scope not callable'

    def test_connection_only_fields_are_absent_from_scope(self):
        """path_params, connection_id, and internal fields carry scope_key=None
        and must not appear in as_scope() output (proposal §3.1 omits them)."""
        scope = _sample_connection().as_scope()
        for spec in _CONNECTION_FIELDS:
            if spec.scope_key is None:
                assert spec.attr not in scope, (
                    f'{spec.attr} is Connection-only but leaked into the scope')


# ---------------------------------------------------------------------------
# Round-trip fidelity (§4.1)
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_connection_to_scope_and_back_is_identity(self):
        conn = _sample_connection()
        conn2 = Connection.from_scope(conn.as_scope(), _stub_receive())
        assert conn == conn2

    def test_roundtrip_with_none_client_server(self):
        conn = Connection(
            method='GET', path='/', raw_path=b'/', query_string=b'',
            http_version='2', scheme='http',
            headers=Headers([(b'host', b'x')]),
            client=None, server=None)
        conn2 = Connection.from_scope(conn.as_scope(), _stub_receive())
        assert conn == conn2
        assert conn2.client is None and conn2.server is None

    def test_roundtrip_preserves_headers_as_Headers(self):
        conn = _sample_connection()
        conn2 = Connection.from_scope(conn.as_scope(), _stub_receive())
        assert isinstance(conn2.headers, Headers)
        assert conn2.headers.get(b'content-type') == b'application/json'

    def test_scope_to_connection_and_back_preserves_mandated_keys(self):
        conn = _sample_connection()
        scope = conn.as_scope()
        scope2 = Connection.from_scope(scope, _stub_receive()).as_scope()
        for key in _ASGI_MANDATED_HTTP_SCOPE_KEYS:
            assert key in scope2, f'as_scope() dropped mandated key {key!r}'
            assert scope2[key] == scope[key], f'{key} changed across round-trip'


# ---------------------------------------------------------------------------
# as_scope() shape
# ---------------------------------------------------------------------------

class TestAsScope:
    def test_headers_serialized_as_list_of_pairs(self):
        scope = _sample_connection().as_scope()
        assert isinstance(scope['headers'], list)
        assert (b'host', b'example.com') in scope['headers']

    def test_client_server_as_list(self):
        scope = _sample_connection().as_scope()
        assert scope['client'] == ['203.0.113.7', 54321]
        assert scope['server'] == ['10.0.0.1', 443]

    def test_asgi_version_dict_present(self):
        scope = _sample_connection().as_scope()
        assert scope['asgi'] == {'version': '3.0', 'spec_version': '2.2'}

    def test_returned_scope_is_fresh_allocation(self):
        conn = _sample_connection()
        s1, s2 = conn.as_scope(), conn.as_scope()
        assert s1 is not s2

    def test_state_is_shared_by_reference(self):
        """Middleware mutating scope['state'] must be visible to the handler —
        state is the shared per-request grab-bag."""
        conn = _sample_connection()
        scope = conn.as_scope()
        scope['state']['x'] = 1
        assert conn.state['x'] == 1


# ---------------------------------------------------------------------------
# Body / json / text / cookies (single drain, matches old Request)
# ---------------------------------------------------------------------------

class TestBody:
    @pytest.mark.asyncio
    async def test_body_reads_once_and_caches(self):
        drained = {'n': 0}

        async def receive():
            drained['n'] += 1
            return {'type': 'http.request', 'body': b'payload', 'more_body': False}

        conn = _sample_connection()
        conn._receive = receive
        assert await conn.body() == b'payload'
        assert await conn.body() == b'payload'   # cached
        assert drained['n'] == 1

    @pytest.mark.asyncio
    async def test_json(self):
        conn = _sample_connection()
        conn._receive = _stub_receive([
            {'type': 'http.request', 'body': b'{"a": 1}', 'more_body': False}])
        assert await conn.json() == {'a': 1}

    @pytest.mark.asyncio
    async def test_text(self):
        conn = _sample_connection()
        conn._receive = _stub_receive([
            {'type': 'http.request', 'body': b'h\xc3\xa9llo', 'more_body': False}])
        assert await conn.text() == 'héllo'

    @pytest.mark.asyncio
    async def test_body_disconnect_raises(self):
        conn = _sample_connection()
        conn._receive = _stub_receive([{'type': 'http.disconnect'}])
        with pytest.raises(ClientDisconnected):
            await conn.body()

    def test_cookies_parsed_once(self):
        conn = Connection(
            method='GET', path='/', raw_path=b'/', query_string=b'',
            http_version='1.1', scheme='http',
            headers=Headers([(b'cookie', b'sid=abc; theme=dark')]))
        assert conn.cookies == {'sid': 'abc', 'theme': 'dark'}
        assert conn.cookies is conn.cookies   # cached (same object)


# ---------------------------------------------------------------------------
# Equality excludes caches / receive
# ---------------------------------------------------------------------------

class TestEquality:
    def test_receive_and_body_cache_excluded_from_eq(self):
        a = _sample_connection()
        b = _sample_connection()
        a._receive = _stub_receive()
        b._receive = _stub_receive()
        a._body = b'x'
        assert a == b, 'receive/body cache must not affect equality'
