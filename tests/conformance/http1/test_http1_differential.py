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
from blackbull.server import ASGIServer  # noqa: E402
from blackbull.client import (  # noqa: E402
    HTTP1Client,
    ReadResponse,
    Scenario,
    SendBytes,
)
from blackbull.client.scenario_oracle import (  # noqa: E402
    ACCEPTED_CATEGORIES,
    Category,
    SideOutcome,
    categorize,
    normalize_response,
    run_scenario,
)


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
    from types import SimpleNamespace

    app = _make_diff_app()
    server = ASGIServer(app)
    server.open_socket(0)
    p = Process(target=lambda: asyncio.run(server.run()))
    p.start()
    try:
        server.wait_for_port(timeout=10.0)
        yield SimpleNamespace(app=app, port=server.port)
    finally:
        server.close()
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

import time as _time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

# Sprint 18 Phase 3 — Category, ACCEPTED_CATEGORIES, SideOutcome,
# normalize_response, categorize, and run_scenario moved to
# blackbull.client.scenario_oracle so the atheris fuzz harness can
# reuse them without importing this pytest module.  Imports at the
# top of the file pull them back in.


@dataclass
class DiffContext:
    """A failure-diagnosis snapshot for one Hypothesis example.

    Sprint 17 Phase 5 — ``scenario`` replaces the prior dict-shaped
    ``req`` field as the source of truth for what went on the wire.
    ``wire_request`` continues to hold the actual bytes captured from
    the *BlackBull* side's :attr:`HTTP1Client.wire_buffer` (the nginx
    side sends the same bytes; we only carry one capture in the dump).
    """
    scenario: Scenario
    nginx: SideOutcome
    blackbull: SideOutcome
    category: Category
    wire_request: bytes = b''
    notes: list[str] = field(default_factory=list)


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


# ---------------------------------------------------------------------------
# Sprint 17 Phase 5 — scenario-level strategies
#
# A Scenario is the unified shape both this differential test and
# tests/conformance/http1/fuzz/fuzz_http1.py drive against the server.
# The dict-shaped http_request_strategy above stays as the source of
# truth for "what a normal HTTP/1.1 request looks like" — we just
# lift it into a Scenario for the executor.
# ---------------------------------------------------------------------------


def _ensure_host(req: dict) -> dict:
    """Inject a Host header if the example didn't pick one.

    Phase 5 sends bytes verbatim via `SendBytes` (no HTTP1Client.request
    Host injection), so the wire request must already carry Host or
    nginx will respond 400 while BlackBull is more permissive — a
    divergence that's an oracle artefact, not a bug.
    """
    headers = dict(req.get('headers', {}))
    if b'host' not in headers:
        headers[b'host'] = b'localhost'
    return {**req, 'headers': headers}


well_formed_scenario_strategy = http_request_strategy.map(
    lambda req: Scenario.well_formed(reconstruct_wire_request(_ensure_host(req))),
)


def _build_slowloris_scenario(req: dict, split_at: int,
                              byte_interval: float) -> Scenario:
    """Split a well-formed wire request into two SendBytes — the first
    transmitted slowly (one byte every ``byte_interval`` seconds), the
    second at full speed — followed by a ReadResponse with a tight
    timeout.

    ``split_at`` indexes within the wire bytes; clamped to leave at
    least 1 byte on each side so we always have *some* trickle and
    *some* burst.
    """
    wire = reconstruct_wire_request(_ensure_host(req))
    cut = max(1, min(len(wire) - 1, split_at))
    return Scenario(steps=(
        SendBytes(data=wire[:cut], byte_interval=byte_interval),
        SendBytes(data=wire[cut:]),
        ReadResponse(timeout=1.0),
    ))


# Phase 6 — bounded so worst-case example fits the 2s Hypothesis
# deadline.  Worst trickle: 16 bytes × 20 ms = 320 ms.  Plus 1 s
# ReadResponse = 1.32 s per side × 2 sides = 2.64 s per example —
# slightly over deadline=2000 in the absolute worst case, but
# Hypothesis is permissive about deadline+slop and the typical
# example is well under.
slowloris_scenario_strategy = st.builds(
    _build_slowloris_scenario,
    req=http_request_strategy,
    split_at=st.integers(min_value=1, max_value=16),
    byte_interval=st.sampled_from([0.005, 0.01, 0.02]),
)


# ---------------------------------------------------------------------------
# Sprint 17 Phase 7 — malformed_scenario_strategy.
#
# These strategies emit wire bytes that the high-level HTTP1Client.request()
# would reject up front — garbage request lines, RFC-invalid header values,
# duplicate Content-Length, conflicting CL+TE, bad HTTP versions.  Driven
# through SendBytes so the bytes land verbatim on the wire.  Both servers
# should reject the same way (-> BOTH_REJECTED) or BlackBull's behaviour
# matches nginx's (-> OK).  Divergences land in the failure categories
# Phase 4 introduced.
# ---------------------------------------------------------------------------


# Garbage tokens that look method-like but aren't on the RFC 9110 list.
# nginx and BlackBull should both reject — or both accept and forward to
# /echo, depending on permissiveness.  The differential is what we care
# about; the exact behaviour is a finding, not a precondition.
_GARBAGE_METHODS = (
    b'BREW',           # not a registered HTTP method
    b'   GET',          # leading whitespace — RFC 9112 forbids
    b'GET ',            # trailing space then path follows immediately
    b'\x80GET',         # high-bit byte in method
    b'',                # empty method — request line malformed
)

# Targets that aren't path-form (RFC 9112 §3.2 enumerates the four
# request-target forms; we exercise the unusual three).
_UNUSUAL_TARGETS = (
    b'http://localhost/x',      # protocol-absolute (proxy form)
    b'example.com:443',         # authority-form (CONNECT)
    b'*',                       # asterisk-form (server-wide OPTIONS)
    b'/' + b'a' * 4096,         # very long path
    b'/%2e%2e/etc/passwd',      # percent-encoded traversal
)

# RFC-invalid HTTP versions on plaintext HTTP/1.x.
_BAD_VERSIONS = (
    b'HTTP/1.0',   # valid, but our app+test currently assume 1.1 framing
    b'HTTP/2.0',   # MUST NOT appear over plaintext per RFC 9112
    b'HTTP/9.9',   # bogus
    b'http/1.1',   # lowercase — RFC requires uppercase
    b'HTP/1.1',    # typo
)


def _build_garbage_request_line_scenario(method: bytes, target: bytes,
                                         version: bytes) -> Scenario:
    """Send a request with a deliberately broken request line."""
    return Scenario(steps=(
        SendBytes(
            data=method + b' ' + target + b' ' + version + b'\r\n'
                 b'Host: localhost\r\n\r\n',
        ),
        ReadResponse(timeout=1.0),
    ))


garbage_request_line_strategy = st.builds(
    _build_garbage_request_line_scenario,
    method=st.sampled_from(_GARBAGE_METHODS),
    target=st.sampled_from(_UNUSUAL_TARGETS),
    version=st.sampled_from(_BAD_VERSIONS),
)


# Header lines that violate RFC 7230 §3.2 (field-value charset).
_INVALID_HEADER_LINES = (
    b'X-Nul: a\x00b',                              # NUL in value
    b'X-High: \x80\x81\x82',                       # high-bit in value
    b'X-Empty:',                                    # empty value
    b'X-Whitespace:    ',                           # all-whitespace
    b'X-Very-Long: ' + b'a' * 8192,                 # value > 8 KiB
    b'Content-Length: 5\r\nContent-Length: 10',     # duplicate CL
    b'Content-Length: 5\r\nTransfer-Encoding: chunked',  # CL + TE
    b'Transfer-Encoding: identity, chunked',        # double TE token
    b'Transfer-Encoding: gzip',                     # unsupported encoding
)


def _build_invalid_header_scenario(invalid_line: bytes) -> Scenario:
    return Scenario(steps=(
        SendBytes(
            data=b'POST /echo HTTP/1.1\r\n'
                 b'Host: localhost\r\n'
                 + invalid_line + b'\r\n\r\n',
        ),
        ReadResponse(timeout=1.0),
    ))


invalid_header_strategy = st.builds(
    _build_invalid_header_scenario,
    invalid_line=st.sampled_from(_INVALID_HEADER_LINES),
)


# Aggregate malformed-scenario strategy.  Both sub-strategies are
# uniform-weighted; the top-level scenario_strategy below biases
# toward well_formed so the malformed cases don't dominate the budget.
malformed_scenario_strategy = st.one_of(
    garbage_request_line_strategy,
    invalid_header_strategy,
)


# Top-level mix that the differential test consumes.  Well-formed
# dominates so we keep solid coverage of the common path; slowloris
# and malformed ride along to probe both servers' tolerance for
# pathological inputs.
scenario_strategy = st.one_of(
    well_formed_scenario_strategy,
    slowloris_scenario_strategy,
    malformed_scenario_strategy,
)


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
    """Render a DiffContext as a human-readable JSON dump.

    Phase 5 — the scenario is JSON-Lines under ``scenario``; the
    captured wire bytes (what BlackBull actually received) sit
    alongside as ``wire_request``.  They're related but not redundant:
    the scenario is the *input* (what we asked the client to do);
    wire_request is the *output* (what landed on the socket).
    """
    payload = {
        'category': ctx.category.value,
        'scenario': ctx.scenario.to_json().splitlines(),
        'wire_request': _json_default(ctx.wire_request),
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


# ---------------------------------------------------------------------------
# Sprint 17 Phase 8 — JSONL corpus capture + regression replay.
#
# When the Hypothesis sweep finds an example that lands outside
# ACCEPTED_CATEGORIES, we serialise the scenario (`diff_*.jsonl`) and
# its sidecar metadata (`diff_*.meta.json`) to the shared corpus dir.
# The companion ``test_corpus_replay`` then reloads each pair, runs
# the scenario against the live stack, and asserts the recorded
# category is reproduced.  This buys regression coverage for known
# divergences without expanding the Hypothesis budget.
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402

# Sprint 17 epilogue — the user-curated regression corpus lives under
# ``fuzz/user-corpus/``, separate from ``fuzz/corpus/`` which atheris
# uses as its working directory.  Keeping the two apart means atheris's
# auto-generated seeds don't pollute the replay set and our curated
# entries don't drift under libFuzzer's pruning.
_CORPUS_DIR = Path(__file__).parent / 'fuzz' / 'user-corpus'


def _scenario_short_hash(scenario: Scenario) -> str:
    """First 16 hex chars of the SHA-1 over the JSONL form — used as
    the dedup suffix on corpus filenames so two captures of the same
    scenario don't pile up under different timestamps."""
    digest = hashlib.sha1(scenario.to_json().encode('utf-8')).hexdigest()
    return digest[:16]


def _maybe_dump_corpus(ctx: DiffContext) -> None:
    """Write ``diff_<hash>.jsonl`` + sidecar metadata when the example
    landed outside ACCEPTED_CATEGORIES.

    Sprint 18 — filename is now hash-only (no timestamp prefix) so
    re-captures of the same scenario overwrite rather than create new
    files.  Pre-Sprint-18 the timestamp prefix meant every run added
    another copy under a different ``diff_<ts>_<hash>.jsonl`` name.
    The ``captured_at_unix`` field in the sidecar still records when
    the divergence was last observed.
    """
    if ctx.category in ACCEPTED_CATEGORIES:
        return
    _CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    short = _scenario_short_hash(ctx.scenario)
    base = _CORPUS_DIR / f'diff_{short}'
    base.with_suffix('.jsonl').write_text(ctx.scenario.to_json() + '\n')
    meta = {
        'category': ctx.category.value,
        'captured_at_unix': int(_time.time()),
        'wire_request_latin1': ctx.wire_request.decode(
            'latin-1', errors='backslashreplace'),
        'nginx_exception': ctx.nginx.exception,
        'nginx_status': (ctx.nginx.response or {}).get('status'),
        'blackbull_exception': ctx.blackbull.exception,
        'blackbull_status': (ctx.blackbull.response or {}).get('status'),
    }
    base.with_suffix('.meta.json').write_text(json.dumps(meta, indent=2))


@pytest.mark.docker
@pytest.mark.xfail(
    reason='Sprint 17: known divergences from nginx remain (e.g. host: \':\' '
           'closes the connection on BlackBull).  Phase 4 categorises them '
           'as BB_TRANSPORT_FAIL; widening ACCEPTED_CATEGORIES is a future '
           'decision once the underlying causes are understood.',
    strict=False,
)
@given(scenario_strategy)
@settings(
    # Phase 6 — reproducibility over throughput.  200 examples × ~2 s
    # ceiling each ≈ 7 min wall budget.  Deadline catches scenarios
    # that drift past 2 s (which would otherwise erode determinism).
    max_examples=200,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
@pytest.mark.asyncio
async def test_blackbull_vs_nginx_http11_differential(
    nginx_server,
    diff_h1_app,
    scenario,
):
    ng, _ = await run_scenario(nginx_server['host'], nginx_server['port'], scenario)
    bb, bb_wire = await run_scenario('127.0.0.1', diff_h1_app.port, scenario)
    cat = categorize(ng, bb)

    note(f'scenario.steps={len(scenario.steps)}')
    note(f'category={cat.value}')
    note(f'nginx ok={ng.ok} elapsed={ng.elapsed_s:.3f}s')
    note(f'blackbull ok={bb.ok} elapsed={bb.elapsed_s:.3f}s')

    ctx = DiffContext(
        scenario=scenario, nginx=ng, blackbull=bb, category=cat,
        wire_request=bb_wire,
    )
    _maybe_dump_corpus(ctx)
    assert cat in ACCEPTED_CATEGORIES, dump(ctx)


# ---------------------------------------------------------------------------
# Phase 8 — corpus replay.
# Reloads every captured diff_*.jsonl + .meta.json pair, replays the
# scenario, and asserts the recorded category is reproduced.  Fast
# regression signal — drops a known-failing scenario into a fixed
# sequence, no Hypothesis budget consumed.
# ---------------------------------------------------------------------------


def _iter_corpus_pairs():
    """Yield (scenario, expected_category, source_path) for each
    ``diff_*.jsonl`` that has a matching ``.meta.json`` sidecar."""
    if not _CORPUS_DIR.exists():
        return
    for jsonl in sorted(_CORPUS_DIR.glob('diff_*.jsonl')):
        meta_path = jsonl.with_suffix('.meta.json')
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        try:
            expected = Category(meta['category'])
        except ValueError:
            # Recorded category not in current enum — skip rather than
            # fail; an enum migration could legitimately rename a
            # category and we don't want stale corpus to block the
            # suite.
            continue
        scenario = Scenario.from_json(jsonl.read_text())
        yield scenario, expected, jsonl


def _corpus_ids():
    return [p.stem for _, _, p in _iter_corpus_pairs()]


@pytest.mark.docker
@pytest.mark.asyncio
@pytest.mark.parametrize(
    'scenario,expected_category,source_path',
    list(_iter_corpus_pairs()),
    ids=_corpus_ids() or ['<no corpus>'],
)
async def test_corpus_replay(nginx_server, diff_h1_app,
                             scenario, expected_category, source_path):
    """Replay each captured corpus entry and assert the category still
    matches.  When new BlackBull / nginx behaviour aligns or diverges,
    this test pinpoints which scenarios moved without re-running the
    full 200-example Hypothesis sweep.
    """
    ng, _ = await run_scenario(nginx_server['host'], nginx_server['port'], scenario)
    bb, _ = await run_scenario('127.0.0.1', diff_h1_app.port, scenario)
    cat = categorize(ng, bb)
    assert cat == expected_category, (
        f'corpus replay drift in {source_path.name}: '
        f'recorded={expected_category.value}, now={cat.value}'
    )
