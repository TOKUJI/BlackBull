"""The native→ASGI boundary for WebSocket, under an external ASGI host.

BlackBull is native internally in both modes: the object-form
:class:`~blackbull.websocket.WebSocket` puts ``NativeWSMessage`` on the send
channel.  Under an external host (uvicorn, or ``asgi=True``) that channel ends
at code which reads ``event['type']`` off a dict, so the boundary has to expand
those messages exactly as it already expands ``NativeResponse`` on the HTTP
side.

The assertions are on what the *host* receives, not on how the conversion is
done, so they hold regardless of where the expansion is implemented.
"""
import pytest

from blackbull import BlackBull
from blackbull.native import NativeResponse, NativeWSMessage
from blackbull.utils import Scheme
from http import HTTPMethod


def _ws_scope(path='/ws'):
    return {
        'type': 'websocket',
        'path': path,
        'raw_path': path.encode(),
        'query_string': b'',
        'headers': [],
        'client': ['127.0.0.1', 12345],
        'server': ['127.0.0.1', 80],
        'scheme': 'ws',
        'subprotocols': [],
        'asgi': {'version': '3.0', 'spec_version': '2.3'},
    }


class _Host:
    """Stands in for uvicorn: feeds client events, records what it is sent."""

    def __init__(self, *events):
        self._incoming = list(events)
        self.sent: list = []

    async def receive(self):
        if self._incoming:
            return self._incoming.pop(0)
        return {'type': 'websocket.disconnect', 'code': 1005}

    async def send(self, event):
        self.sent.append(event)


@pytest.mark.asyncio
async def test_object_form_ws_reaches_an_external_host_as_asgi_dicts():
    """An external host must never be handed a native object.

    Every ``websocket.*`` event uvicorn reads is subscripted by key; a
    ``NativeWSMessage`` arriving here is a TypeError in the host, not in us.
    """
    app = BlackBull()

    @app.route(path='/ws', methods=[HTTPMethod.GET], scheme=Scheme.websocket)
    async def echo(ws):
        await ws.accept()
        async for message in ws:
            await ws.send(message)

    host = _Host(
        {'type': 'websocket.connect'},
        {'type': 'websocket.receive', 'text': 'ping', 'bytes': None},
        {'type': 'websocket.disconnect', 'code': 1000},
    )
    await app(_ws_scope(), host.receive, host.send)

    assert host.sent, 'the handler sent nothing; the test proves nothing'
    native = [e for e in host.sent
              if isinstance(e, (NativeWSMessage, NativeResponse))]
    assert not native, f'native objects crossed the ASGI boundary: {native}'
    assert all(isinstance(e, dict) for e in host.sent), host.sent

    kinds = [e['type'] for e in host.sent]
    assert kinds[0] == 'websocket.accept'
    assert 'websocket.send' in kinds
    echoed = [e for e in host.sent if e['type'] == 'websocket.send']
    assert echoed[0].get('text') == 'ping', echoed[0]
