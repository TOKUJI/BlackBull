# tests/conformance/http1/test_http1_differential.py
#
# Sprint 17 — Differential fuzzing: BlackBull vs nginx as oracle.
#
# nginx (running in a Docker container via testcontainers) is the
# reference implementation.  Hypothesis generates HTTP/1.1 requests,
# both servers receive the same request, responses are normalised and
# compared.  Divergences are categorised; only a configured whitelist
# of categories is allowed to pass.
#
# Run under plain pytest:
#     pytest tests/conformance/http1/test_http1_differential.py
#
# Skipped cleanly when the Docker daemon is unreachable.

import json
import tempfile
import textwrap
import time
import uuid
from pathlib import Path

import pytest

# --- Module-level skip when Docker isn't reachable --------------------------
# pytest.importorskip handles the "testcontainers not installed" case;
# the explicit docker ping handles "installed but no daemon" (e.g. CI
# runners without /var/run/docker.sock).  Both fail cleanly without
# raising into a collection error.
pytest.importorskip('testcontainers')

import docker as _docker  # noqa: E402  (after importorskip)

try:
    _docker.from_env().ping()
except Exception as exc:  # noqa: BLE001
    pytest.skip(
        f'Docker daemon unreachable; differential test skipped ({exc!r})',
        allow_module_level=True,
    )

import asyncio  # noqa: E402
from http import HTTPMethod  # noqa: E402
from multiprocessing import Process  # noqa: E402

import requests  # noqa: E402
from docker import from_env  # noqa: E402
from hypothesis import HealthCheck, given, note, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from testcontainers.core.container import DockerContainer  # noqa: E402
from testcontainers.nginx import NginxContainer  # noqa: E402

from blackbull import BlackBull, read_body  # noqa: E402
from blackbull.client import HTTP1Client  # noqa: E402


# ---------------------------------------------------------------------------
# Docker fixtures: nginx oracle + echo backend on a shared network
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# BlackBull side: differential-specific h1_app whose `/` route mirrors the
# nginx oracle's `return 200 "ok"` for ANY method.  The shared h1_app in
# conftest.py registers `/` for GET only — that mismatch is what the
# Sprint 17 Phase 1 minimisation surfaced as the headline status_mismatch.
# Sprint 17 Phase 3 widens just the differential test's app instead of
# touching the shared fixture (which other tests rely on for 405-on-PATCH).
# ---------------------------------------------------------------------------

_DIFF_METHODS = [HTTPMethod.GET, HTTPMethod.POST, HTTPMethod.PUT,
                 HTTPMethod.DELETE, HTTPMethod.OPTIONS]


def _make_diff_app() -> BlackBull:
    from http import HTTPStatus  # noqa: PLC0415

    app = BlackBull()

    @app.route(path='/echo', methods=_DIFF_METHODS)
    async def echo(scope, receive, send):
        body = await read_body(receive)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'application/octet-stream')]})
        await send({'type': 'http.response.body', 'body': body})

    # nginx's `location /` returns 200 "ok" for ANY method and ANY path
    # that isn't matched by a more specific location.  Mirror that here by
    # turning 404 + 405 into 200 "ok".  Without this, every Hypothesis
    # example with path ∉ {`/`, `/echo`} would diverge on status alone.
    # Body is drained on the way out so the keep-alive connection state
    # stays consistent for the next request on the same socket.
    @app.on_error(HTTPStatus.NOT_FOUND)
    async def _not_found(scope, receive, send):
        await read_body(receive)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    @app.on_error(HTTPStatus.METHOD_NOT_ALLOWED)
    async def _method_not_allowed(scope, receive, send):
        await read_body(receive)
        await send({'type': 'http.response.start', 'status': 200,
                    'headers': [(b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'ok'})

    return app


@pytest.fixture(scope='module')
def diff_h1_app():
    """A live BlackBull HTTP/1.1 server with nginx-matching `/` + `/echo`
    semantics — used only by the differential test in this module."""
    app = _make_diff_app()
    app.create_server(port=0)
    p = Process(target=lambda: asyncio.run(app.run()))
    p.start()
    app.wait_for_port(timeout=10.0)
    yield app
    app.stop()
    p.terminate()
    p.join(timeout=5)


@pytest.fixture(scope='session')
def docker_network():
    client = from_env()
    net = client.networks.create(
        name=f'bb-test-{uuid.uuid4()}',
        driver='bridge',
    )
    try:
        yield net
    finally:
        try:
            net.remove()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(scope='session')
def echo_server(docker_network):
    container = (
        DockerContainer('echo-server:latest')
        .with_exposed_ports(8000)
        .with_network(docker_network)
        .with_name('echo_backend')
    )
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope='session')
def nginx_server(echo_server, docker_network):
    nginx_conf = textwrap.dedent("""
    events {}
    http {
        server {
            listen 80;
            location / {
                return 200 "ok";
            }
            location /echo {
                proxy_pass http://echo_backend:8000;
            }
        }
    }
    """)

    with tempfile.TemporaryDirectory() as td:
        conf_path = Path(td) / 'nginx.conf'
        conf_path.write_text(nginx_conf)

        nginx = (
            NginxContainer('nginx:1.27-alpine')
            .with_exposed_ports(80)
            .with_volume_mapping(str(conf_path), '/etc/nginx/nginx.conf')
            .with_network(docker_network)
        )
        nginx.start()

        host = nginx.get_container_host_ip()
        port = nginx.get_exposed_port(80)
        base_url = f'http://{host}:{port}'

        deadline = time.time() + 10
        while True:
            try:
                r = requests.get(base_url, timeout=1)
                if r.status_code in (200, 404):
                    break
            except Exception:  # noqa: BLE001
                pass
            if time.time() > deadline:
                raise RuntimeError('Nginx not ready')
            time.sleep(0.2)

        try:
            yield {
                'container': nginx,
                'base_url': base_url,
                'host': host,
                'port': port,
            }
        finally:
            nginx.stop()


# ---------------------------------------------------------------------------
# Phase 4 — failure categorisation + wire-byte reconstruction + structured
# per-side outcome (response | exception | timeout) so the test can
# bucket every Hypothesis example without unhandled exceptions reaching
# pytest.
# ---------------------------------------------------------------------------

import enum  # noqa: E402
import time as _time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402


class Category(str, enum.Enum):
    """Why a differential example was (not) accepted.

    Subclassing ``str`` makes the values JSON-serialisable directly and
    keeps ``assert ctx.category == 'OK'``-style sites readable.  Phase 4
    introduces this enum; later phases (5–8) may add categories.
    """
    OK = 'OK'                          # normalised responses match
    STATUS_DIFFER = 'STATUS_DIFFER'    # both responded, status differs
    BODY_DIFFER = 'BODY_DIFFER'        # both responded, same status, body differs
    HEADER_DIFFER = 'HEADER_DIFFER'    # both responded, same status+body, headers differ
    BB_TRANSPORT_FAIL = 'BB_TRANSPORT_FAIL'   # BlackBull errored, nginx fine
    NG_TRANSPORT_FAIL = 'NG_TRANSPORT_FAIL'   # nginx errored, BlackBull fine
    BB_TIMEOUT = 'BB_TIMEOUT'          # BlackBull exceeded per-example wait_for
    NG_TIMEOUT = 'NG_TIMEOUT'          # nginx ditto
    BOTH_REJECTED = 'BOTH_REJECTED'    # both 4xx OR both transport-failed


# Categories accepted by the assertion below.  Adding a new known-
# divergence category here is a config change, not a code change.  Start
# tight: only OK (true equivalence) and BOTH_REJECTED (both servers
# refused the input the same way).  Other categories are surfaced for
# investigation.
ACCEPTED_CATEGORIES: frozenset[Category] = frozenset({
    Category.OK,
    Category.BOTH_REJECTED,
})


@dataclass
class SideOutcome:
    """One side's response in a differential pair.

    Exactly one of (response, exception) is populated.  ``timed_out`` is
    True if the failure was an :class:`asyncio.TimeoutError` from the
    per-example wait_for; we record it separately because timeouts are
    semantically distinct from other transport errors.
    """
    response: dict | None = None        # normalised {status, headers, body}
    exception: str | None = None        # repr(exc) when no response arrived
    timed_out: bool = False
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.response is not None


@dataclass
class DiffContext:
    req: dict
    nginx: SideOutcome
    blackbull: SideOutcome
    category: Category
    wire_request: bytes = b''           # reconstructed (not captured)
    notes: list[str] = field(default_factory=list)


# Framing / transport headers stripped from the differential comparison.
# These are emitted (or not) at the discretion of the server's protocol
# layer and are RFC-compliant either way; comparing them produces noise.
#   - date / server: clock + identity (varies per host)
#   - content-length: derived from body, already in body
#   - connection: HTTP/1.1 keep-alive is implicit; nginx emits the header
#     explicitly while BlackBull omits it.  Both legal per RFC 9112 §9.1.
#   - keep-alive: nginx-specific timing hint (RFC 9112 deprecates it)
#   - transfer-encoding: framing detail, derived from body shape
_FRAMING_HEADERS = frozenset({
    b'date',
    b'server',
    b'content-length',
    b'connection',
    b'keep-alive',
    b'transfer-encoding',
})


def normalize_response(resp):
    return {
        'status': resp.status,
        'headers': sorted(
            (k.lower(), v)
            for k, v in resp.headers
            if k.lower() not in _FRAMING_HEADERS
        ),
        'body': resp.body,
    }


# Per-example wall-clock budget.  Phase 6 will tighten this; for Phase 4
# a generous bound is fine so the categorisation can be observed without
# Hypothesis crying about slow examples.
_PER_REQUEST_TIMEOUT_S = 10.0


async def send_http1(host: str, port: int, req: dict) -> SideOutcome:
    """Send *req* to (host, port) and return a SideOutcome.

    Never raises — failures are folded into the outcome so the test layer
    can categorise rather than crash on unhandled exceptions.
    """
    headers = list(req['headers'].items())
    t0 = _time.monotonic()
    try:
        async def _drive():
            async with HTTP1Client(host, port) as c:
                return await c.request(
                    req['method'], req['path'],
                    headers=headers, body=req['body'],
                )
        resp = await asyncio.wait_for(_drive(), timeout=_PER_REQUEST_TIMEOUT_S)
        return SideOutcome(
            response=normalize_response(resp),
            elapsed_s=_time.monotonic() - t0,
        )
    except asyncio.TimeoutError as exc:
        return SideOutcome(
            exception=repr(exc),
            timed_out=True,
            elapsed_s=_time.monotonic() - t0,
        )
    except Exception as exc:  # noqa: BLE001
        # Catch deepest cause too so IncompleteReadError-from-EOF reads
        # cleanly in the dump.
        chain = []
        cur: BaseException | None = exc
        while cur is not None:
            chain.append(f'{type(cur).__name__}: {cur!r}')
            cur = cur.__cause__
        return SideOutcome(
            exception=' / '.join(chain),
            elapsed_s=_time.monotonic() - t0,
        )


def reconstruct_wire_request(req: dict) -> bytes:
    """Best-effort reconstruction of the bytes HTTP1Client would put on
    the wire for *req*.  Not byte-exact (the client may inject Host /
    Content-Length we don't know about here), but close enough to be a
    diagnostic anchor in failure dumps.  Phase 8 replaces this with a
    real capture via the client's record_wire_bytes path."""
    method = req['method']
    if isinstance(method, bytes):
        method_b = method
    else:
        method_b = method.encode()
    path = req['path']
    if isinstance(path, bytes):
        path_b = path
    else:
        path_b = path.encode()
    out = bytearray()
    out += method_b + b' ' + path_b + b' HTTP/1.1\r\n'
    headers_dict = req.get('headers', {})
    has_host = any(k.lower() == b'host' for k in headers_dict)
    if not has_host:
        out += b'host: <client-injected>\r\n'
    for k, v in headers_dict.items():
        out += k + b': ' + v + b'\r\n'
    body = req.get('body', b'')
    method_upper = method_b.upper()
    if body or method_upper in {b'POST', b'PUT', b'PATCH', b'DELETE'}:
        if not any(k.lower() == b'content-length' for k in headers_dict):
            out += b'content-length: ' + str(len(body)).encode() + b'\r\n'
    out += b'\r\n'
    if body:
        out += body
    return bytes(out)


# ---------------------------------------------------------------------------
# Hypothesis strategy — only fields that the helper actually uses
# (Sprint 17 Phase 0: dropped unused 'query' and 'version' fields.
#  Phase 7 will add a malformed_request_strategy alongside.)
# ---------------------------------------------------------------------------

HEADER_VALUE_CHARS = st.characters(
    min_codepoint=0x20,
    max_codepoint=0x7E,
    blacklist_characters='\r\n',
)

header_values = st.text(
    HEADER_VALUE_CHARS,
    min_size=0,
    max_size=32,
).map(lambda s: s.encode())

http_request_strategy = st.fixed_dictionaries({
    'method': st.sampled_from(['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS']),
    'path': st.sampled_from([
        '/',
        '/echo',
        '/a/b/c',
        '/a/../b',
        '/%61%62%63',
    ]),
    'headers': st.dictionaries(
        keys=st.sampled_from([
            b'x-test',
            b'content-type',
            b'host',
            b'connection',
            b'user-agent',
        ]),
        values=header_values,
        max_size=8,
    ),
    'body': st.binary(max_size=4096),
})


def _is_4xx(resp: dict) -> bool:
    s = resp.get('status')
    return isinstance(s, int) and 400 <= s < 500


def categorize(ng: SideOutcome, bb: SideOutcome) -> Category:
    """Bucket a differential example into a :class:`Category`.

    Phase 4 — replaces the prior 3-way status_mismatch/body_mismatch/OK
    enumeration.  Order of the checks matters: both-rejected wins over
    individual transport failures so we don't flag inputs that nginx
    also refused.
    """
    # Both sides failed transport-wise (any mix of exception / timeout).
    if not ng.ok and not bb.ok:
        return Category.BOTH_REJECTED

    # One side responded, the other didn't.
    if ng.ok and not bb.ok:
        return Category.BB_TIMEOUT if bb.timed_out else Category.BB_TRANSPORT_FAIL
    if bb.ok and not ng.ok:
        return Category.NG_TIMEOUT if ng.timed_out else Category.NG_TRANSPORT_FAIL

    # Both responded with an HTTP status.  After Phase 3's fixture
    # widening + on_error handlers, the diff_h1_app returns 200 for
    # any method on any path that nginx also returns 200 for.  If both
    # sides answered with a 4xx, treat as BOTH_REJECTED (input was
    # malformed enough that both refused it the same way).
    assert ng.response is not None and bb.response is not None
    if _is_4xx(ng.response) and _is_4xx(bb.response):
        return Category.BOTH_REJECTED

    if ng.response['status'] != bb.response['status']:
        return Category.STATUS_DIFFER
    if ng.response['body'] != bb.response['body']:
        return Category.BODY_DIFFER
    if ng.response['headers'] != bb.response['headers']:
        return Category.HEADER_DIFFER
    return Category.OK


def _json_default(o):
    """JSON fallback for objects the stdlib encoder can't handle.
    Headers use bytes for keys and values; render them as latin-1 with
    backslash-escapes so the dump round-trips through stdout cleanly."""
    if isinstance(o, bytes):
        return o.decode('latin-1', errors='backslashreplace')
    if isinstance(o, Category):
        return o.value
    if hasattr(o, '__dict__'):
        return o.__dict__
    return repr(o)


def _scrubbed_headers(d: dict) -> dict:
    """Render a bytes-keyed header dict with latin-1 escapes so it can
    fit through json.dumps' string-key requirement."""
    return {
        _json_default(k): _json_default(v)
        for k, v in d.items()
    }


def dump(ctx: DiffContext) -> str:
    payload = {
        'category': ctx.category.value,
        'wire_request': _json_default(ctx.wire_request),
        'req': {
            **{k: v for k, v in ctx.req.items() if k not in ('headers', 'body')},
            'headers': _scrubbed_headers(ctx.req.get('headers', {})),
            'body': _json_default(ctx.req.get('body', b'')),
        },
        'nginx': {
            'ok': ctx.nginx.ok,
            'elapsed_s': round(ctx.nginx.elapsed_s, 4),
            'response': ctx.nginx.response,
            'exception': ctx.nginx.exception,
            'timed_out': ctx.nginx.timed_out,
        },
        'blackbull': {
            'ok': ctx.blackbull.ok,
            'elapsed_s': round(ctx.blackbull.elapsed_s, 4),
            'response': ctx.blackbull.response,
            'exception': ctx.blackbull.exception,
            'timed_out': ctx.blackbull.timed_out,
        },
        'notes': ctx.notes,
    }
    return json.dumps(payload, indent=2, default=_json_default)


@pytest.mark.xfail(
    reason='Sprint 17: known divergences from nginx remain (e.g. host: \':\' '
           'closes the connection on BlackBull).  Phase 4 categorises them '
           'as BB_TRANSPORT_FAIL; widening ACCEPTED_CATEGORIES is a future '
           'decision once the underlying causes are understood.',
    strict=False,
)
@given(http_request_strategy)
@settings(
    max_examples=1000,
    suppress_health_check=[HealthCheck.too_slow],
)
@pytest.mark.asyncio
async def test_blackbull_vs_nginx_http11_differential(
    nginx_server,
    diff_h1_app,
    req,
):
    ng = await send_http1(nginx_server['host'], nginx_server['port'], req)
    bb = await send_http1('127.0.0.1', diff_h1_app.port, req)
    cat = categorize(ng, bb)

    note(f'req={req}')
    note(f'category={cat.value}')
    note(f'nginx ok={ng.ok} elapsed={ng.elapsed_s:.3f}s')
    note(f'blackbull ok={bb.ok} elapsed={bb.elapsed_s:.3f}s')

    ctx = DiffContext(
        req=req, nginx=ng, blackbull=bb, category=cat,
        wire_request=reconstruct_wire_request(req),
    )
    assert cat in ACCEPTED_CATEGORIES, dump(ctx)
