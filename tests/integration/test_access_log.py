"""Integration tests for the blackbull.access logger (guide.md §14.1, §14.2).

Verifies that `blackbull.access` emits one INFO record per completed request
with the documented extra fields, by installing a capturing handler inside
the server subprocess and exposing the captured records via an HTTP endpoint.
"""
import asyncio
import logging
import time
from multiprocessing import Process

import httpx
import pytest

from blackbull import BlackBull
from .conftest import live_server


def _make_app() -> BlackBull:
    app = BlackBull()

    _records: list[dict] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            _records.append({
                'path':           getattr(record, 'path', None),
                'method':         getattr(record, 'method', None),
                'status':         getattr(record, 'status', None),
                'response_bytes': getattr(record, 'response_bytes', None),
                'client_ip':      getattr(record, 'client_ip', None),
                'duration_ms':    getattr(record, 'duration_ms', None),
            })

    # Install the handler inside the subprocess (it runs at import time of
    # _make_app, which is called inside the forked process).
    _handler = _Capture()
    logging.getLogger('blackbull.access').addHandler(_handler)

    @app.route(path='/resource')
    async def resource():
        return {'ok': True}

    @app.route(path='/log-records')
    async def log_records():
        return _records

    return app


@pytest.fixture(scope="module")
def live():
    app = _make_app()
    with live_server(app) as handle:
        yield handle
def _base(app) -> str:
    return f'http://127.0.0.1:{app.port}'


def _get_records(base: str, min_count: int = 1) -> list[dict]:
    """Poll until at least *min_count* records appear (max 2s)."""
    import httpx as _httpx
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        r = _httpx.get(f'{base}/log-records')
        records = r.json()
        if len(records) >= min_count:
            return records
        time.sleep(0.05)
    return records


@pytest.mark.integration
@pytest.mark.asyncio
async def test_access_log_record_fields(live):
    async with httpx.AsyncClient() as c:
        await c.get(f'{_base(live)}/resource')
    records = _get_records(_base(live), min_count=1)
    # At least one record whose path is /resource
    hit = next((r for r in records if r.get('path') == '/resource'), None)
    assert hit is not None, f'no record for /resource in {records}'
    assert hit['method'] == 'GET'
    assert hit['status'] == 200
    assert hit['response_bytes'] is not None and hit['response_bytes'] >= 0
    assert hit['client_ip'] is not None
    assert hit['duration_ms'] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_access_log_status_404(live):
    async with httpx.AsyncClient() as c:
        await c.get(f'{_base(live)}/not-found')
    records = _get_records(_base(live), min_count=1)
    hit = next((r for r in records if r.get('path') == '/not-found'), None)
    assert hit is not None, f'no record for /not-found in {records}'
    assert hit['status'] == 404
