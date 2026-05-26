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

import requests  # noqa: E402
from docker import from_env  # noqa: E402
from hypothesis import HealthCheck, given, note, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from testcontainers.core.container import DockerContainer  # noqa: E402
from testcontainers.nginx import NginxContainer  # noqa: E402

from blackbull.client import HTTP1Client  # noqa: E402


# ---------------------------------------------------------------------------
# Docker fixtures: nginx oracle + echo backend on a shared network
# ---------------------------------------------------------------------------

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
# Diff context (Phase 4 will expand this with wire_bytes + categories)
# ---------------------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402


@dataclass
class DiffContext:
    req: dict
    nginx: dict
    blackbull: dict
    category: str | None = None


def normalize_response(resp):
    return {
        'status': resp.status,
        'headers': sorted(
            (k.lower(), v)
            for k, v in resp.headers
            if k.lower() not in {b'date', b'server', b'content-length'}
        ),
        'body': resp.body,
    }


async def send_http1(host: str, port: int, req: dict):
    headers = list(req['headers'].items())
    async with HTTP1Client(host, port) as c:
        return await c.request(
            req['method'],
            req['path'],
            headers=headers,
            body=req['body'],
        )


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


def categorize(req, nginx_resp, blackbull_resp):
    if nginx_resp['status'] != blackbull_resp['status']:
        return 'status_mismatch'
    if nginx_resp['body'] != blackbull_resp['body']:
        return 'body_mismatch'
    return 'OK'


def _json_default(o):
    """JSON fallback for objects the stdlib encoder can't handle.
    Headers use bytes for keys and values; render them as latin-1 with
    backslash-escapes so the dump round-trips through stdout cleanly."""
    if isinstance(o, bytes):
        return o.decode('latin-1', errors='backslashreplace')
    return repr(o)


def dump(ctx):
    payload = {
        'category': ctx.category,
        # Convert bytes-keyed dicts (req['headers']) ahead of json.dumps —
        # the encoder accepts strings for keys but our headers are bytes.
        'req': {
            **ctx.req,
            'headers': {
                _json_default(k): _json_default(v)
                for k, v in ctx.req.get('headers', {}).items()
            },
            'body': _json_default(ctx.req['body']),
        },
        'nginx': ctx.nginx,
        'blackbull': ctx.blackbull,
    }
    return json.dumps(payload, indent=2, default=_json_default)


@pytest.mark.xfail(
    reason='Sprint 17: known divergences from nginx — Phase 4 introduces '
           'failure categorisation and a whitelist so this can pass '
           'without obscuring real bugs.  Remove this xfail when the '
           'whitelist lands.',
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
    h1_app,
    req,
):
    nginx_resp = await send_http1(nginx_server['host'], nginx_server['port'], req)
    blackbull_resp = await send_http1('127.0.0.1', h1_app.port, req)

    n = normalize_response(nginx_resp)
    b = normalize_response(blackbull_resp)
    note(f'req={req}')
    note(f'nginx={n}')
    note(f'blackbull={b}')
    ctx = DiffContext(req=req, nginx=n, blackbull=b, category=categorize(req, n, b))
    assert ctx.category == 'OK', dump(ctx)
