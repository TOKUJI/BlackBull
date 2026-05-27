import os
import asyncio
from http import HTTPMethod
import json
from pathlib import Path
import time
import docker as _docker
import httpx
import pytest
import pytest_asyncio
from multiprocessing import Process
from testcontainers.core.container import DockerContainer

# Test targets
from blackbull import BlackBull
from blackbull.middleware.cors import CORS
from blackbull.response import JSONResponse
from blackbull.request import read_body
from .conftest import live_server



@pytest.fixture(scope="session")
def docker_host_ipv4() -> str:
    """IPv4 address that Docker containers use to reach the WSL2/host machine.

    Docker Desktop injects *two* /etc/hosts entries for host.docker.internal —
    one IPv4 (e.g. 192.168.65.254) and one IPv6 (e.g. fdc4:f303:9324::254).
    glibc resolves IPv6 with higher precedence, but the IPv6 address is not
    reachable from the WSL2 host, so nginx would get ECONNREFUSED → 502.
    This fixture probes a throwaway container once per session to obtain just
    the IPv4 address, which is then used directly in the proxy_pass config.
    """
    client = _docker.from_env()
    output = client.containers.run(
        "alpine:latest",
        ["sh", "-c",
         "awk '/host.docker.internal/ && !/:/ {print $1; exit}' /etc/hosts"],
        extra_hosts={"host.docker.internal": "host-gateway"},
        remove=True,
    )
    return output.decode().strip()


@pytest.fixture(scope="session", autouse=True)
def docker_test_config():

    docker_config = (
        Path(__file__).resolve().parents[1]
        / "docker"
    )

    old = os.environ.get("DOCKER_CONFIG")

    os.environ["DOCKER_CONFIG"] = str(docker_config)

    yield

    if old is None:
        os.environ.pop("DOCKER_CONFIG", None)
    else:
        os.environ["DOCKER_CONFIG"] = old

@pytest.fixture
def nginx(app, docker_host_ipv4, tmp_path):
    nginx_conf = tmp_path / "nginx.conf"
    nginx_conf.write_text(
        (Path(__file__).parent / "nginx.conf")
        .read_text()
        .replace(":8000", f":{app.port}")
        .replace("host.docker.internal", docker_host_ipv4)
    )
    with (
        DockerContainer("nginx:latest")
        .with_exposed_ports(80)
        .with_volume_mapping(
            str(nginx_conf),
            "/etc/nginx/nginx.conf",
        )
    ) as nginx:
        port = int(nginx.get_exposed_port(80))

        # Wait for the full proxy chain — not just nginx TCP readiness, but
        # also the Docker Desktop port-forwarding tunnel from the container to
        # the WSL2-hosted BlackBull process.  Docker Desktop sets this up
        # asynchronously (~400 ms after BlackBull first binds), so the first
        # proxied request often gets ECONNREFUSED → 502.  We retry on 502
        # until the tunnel is live, then proceed with the actual test.
        import http.client as _http
        deadline = time.monotonic() + 30.0
        while True:
            try:
                conn = _http.HTTPConnection('localhost', port, timeout=1)
                conn.request('GET', '/api/hello')
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status != 502:
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "nginx proxy to upstream did not become ready within 30 s"
                )
            time.sleep(0.05)

        yield {
            "container": nginx,
            "base_url": f"http://localhost:{port}",
        }

# def _make_app(allow_origins) -> BlackBull:
def _make_app() -> BlackBull:
    app = BlackBull()

    @app.route(path='/api/hello')
    async def hello():
        return {'greeting': 'Hello, world!', 'cors': 'enabled'}

    @app.route(path='/api/echo', methods=[HTTPMethod.POST])
    async def echo(scope, receive, send):
        body = await read_body(receive)
        data = json.loads(body)
        await send(JSONResponse({'echo': data}))

    # app.use(CORS(
    #     allow_origins=allow_origins,
    #     allow_methods=['GET', 'POST', 'OPTIONS'],
    #     allow_headers=['Content-Type', 'Accept', 'X-Custom-Header'],
    #     max_age=3600,
    # ))
    return app


def _run(app):
    asyncio.run(app.run())


@pytest_asyncio.fixture
async def app():
    # app = _make_app(allow_origins=['https://example.com'])
    app = _make_app()
    with live_server(app) as handle:
        yield handle
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base(app) -> str:
    return f'http://127.0.0.1:{app.port}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_app_run_on_port(app):
    async with httpx.AsyncClient() as c:
        r = await c.get(f'{_base(app)}/api/hello')
    assert r.status_code == 200
    assert 'access-control-allow-origin' not in r.headers

@pytest.mark.integration
@pytest.mark.asyncio
async def test_access_through_nginx(app, nginx):

    async with httpx.AsyncClient() as c:
        r = await c.get(f'{nginx["base_url"]}/api/hello')
    assert r.status_code == 200
    assert 'access-control-allow-origin' not in r.headers
