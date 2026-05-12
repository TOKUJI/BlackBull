import os
import asyncio
from http import HTTPMethod
import json
from pathlib import Path
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
def nginx():
    nginx_conf = (
        Path(__file__).parent / "nginx.conf"
    )
    with (
        DockerContainer("nginx:latest")
        .with_exposed_ports(80)
        .with_volume_mapping(
            str(nginx_conf),
            "/etc/nginx/nginx.conf",
        )
        # .with_extra_hosts(
        #     {"host.docker.internal": "host-gateway"}
        # )
    ) as nginx:
        port = nginx.get_exposed_port(80)

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
    app.create_server(port=0)

    p = Process(target=_run, args=(app,))
    p.start()
    app.wait_for_port(timeout=10.0)

    yield app

    app.stop()
    p.terminate()
    p.join(timeout=5)

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
