"""Integration test for the Non-ASGI bridge — Raw TCP echo (Sprint 50).

Spins up a single BlackBull ``Server`` that serves HTTP on one port and a raw
TCP echo protocol on another, then exercises both to prove coexistence
(non-disruption) and that the bridge round-trips bytes end to end.
"""
import asyncio
import http.client
import socket
import time
from multiprocessing import Process

import pytest

from blackbull import BlackBull
from blackbull.server import Server


def _make_app():
    app = BlackBull()

    @app.route(path='/')
    async def index():
        return "http-ok"

    @app.raw_handler('echo', port=0)
    async def echo(reader, writer, ctx):
        while True:
            data = await reader.read(1024)
            if not data:
                break
            await writer.write(data)

    return app


@pytest.fixture
def live_bridge():
    """Bind HTTP + echo on ephemeral ports, fork a worker, yield (http, echo)."""
    app = _make_app()
    server = Server(app)
    server.open_socket(0)  # binds HTTP + the echo port; populates protocol_ports
    http_port = server.port
    echo_port = server.protocol_ports['echo']
    p = Process(target=lambda: asyncio.run(server.run()))
    p.start()
    try:
        server.wait_for_port(timeout=10.0)
        yield http_port, echo_port
    finally:
        server.close()
        p.terminate()
        p.join(timeout=5)


def _connect_echo(port, timeout=5.0):
    """Connect to the echo port, retrying until the worker is serving it."""
    deadline = time.time() + timeout
    while True:
        try:
            return socket.create_connection(('127.0.0.1', port), timeout=1)
        except OSError:
            if time.time() >= deadline:
                raise
            time.sleep(0.05)


def test_raw_echo_round_trips(live_bridge):
    _http_port, echo_port = live_bridge
    with _connect_echo(echo_port) as sock:
        sock.sendall(b'hello bridge\n')
        received = sock.recv(1024)
    assert received == b'hello bridge\n'


def test_raw_echo_multiple_chunks(live_bridge):
    _http_port, echo_port = live_bridge
    with _connect_echo(echo_port) as sock:
        for chunk in (b'one', b'two', b'three'):
            sock.sendall(chunk)
            assert sock.recv(1024) == chunk


def test_http_still_served_alongside_raw(live_bridge):
    """Non-disruption: the HTTP listener works while a raw protocol is bound."""
    http_port, _echo_port = live_bridge
    conn = http.client.HTTPConnection('127.0.0.1', http_port, timeout=5)
    conn.request('GET', '/')
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    assert resp.status == 200
    assert body == b'http-ok'
