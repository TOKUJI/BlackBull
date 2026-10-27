"""Client-owned transport and WebSocket bounds stay distinct from server caps."""
from __future__ import annotations

import logging
import ssl

import pytest

from blackbull.client import Client, ClientConnection
from blackbull.client.http1 import HTTP1Client
from blackbull.client.http2 import HTTP2Client
from blackbull.client.websocket import WebSocketSession
from blackbull.client.websocket_h2 import WebSocketH2Session
from blackbull.env import get_settings, reset_settings_cache
from blackbull.protocol.frame import FrameFactory
from blackbull.server.http2_ws import HTTP2StreamReader
from blackbull.server.recipient import (AbstractReader, ProtocolError,
                                        WebSocketRecipient)
from blackbull.server.sender import (AbstractWriter, AsyncioWriter,
                                     ConnectionWindow, FlowControlStalled,
                                     HTTP2Sender)
from blackbull.server.ws_codec import MessageTooLarge

from tests.harness import fed, served


@pytest.fixture(autouse=True)
def _fresh_settings():
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_client_bound_defaults_and_environment(monkeypatch):
    for name in (
        'BB_CLIENT_WRITE_TIMEOUT',
        'BB_CLIENT_WS_MAX_FRAME_PAYLOAD',
        'BB_CLIENT_WS_MAX_MESSAGE_SIZE',
    ):
        monkeypatch.delenv(name, raising=False)
    settings = get_settings()
    assert settings.client_write_timeout == 30.0
    assert settings.client_ws_max_frame_payload == 64 * 1024 * 1024
    assert settings.client_ws_max_message_size == 16 * 1024 * 1024

    monkeypatch.setenv('BB_CLIENT_WRITE_TIMEOUT', '1.5')
    monkeypatch.setenv('BB_CLIENT_WS_MAX_FRAME_PAYLOAD', '123')
    monkeypatch.setenv('BB_CLIENT_WS_MAX_MESSAGE_SIZE', '456')
    reset_settings_cache()
    settings = get_settings()
    assert settings.client_write_timeout == 1.5
    assert settings.client_ws_max_frame_payload == 123
    assert settings.client_ws_max_message_size == 456


class _StreamWriter:
    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class _Reader(AbstractReader):
    async def read(self, n=-1):
        return b''

    async def readuntil(self, sep):
        return sep

    async def readexactly(self, n):
        return b'\x00' * n


class _Writer(AbstractWriter):
    async def write(self, data):
        pass


def test_asyncio_writer_keeps_injected_client_cap_name():
    writer = AsyncioWriter(_StreamWriter(), 2.0,
                           cap_name='client_write_timeout')
    assert writer._write_timeout == 2.0
    assert writer._cap_name == 'client_write_timeout'


@pytest.mark.parametrize('protocol', ['http1', 'ws'])
def test_client_writer_runtime_log_has_owner_and_protocol(protocol, caplog):
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    writer = AsyncioWriter(_StreamWriter(), 2.0,
                           cap_name='client_write_timeout',
                           protocol=protocol)
    with pytest.raises(ConnectionError):
        writer._fail_write_timeout()
    records = [r for r in caplog.records
               if getattr(r, 'cap', None) == 'client_write_timeout']
    assert len(records) == 1
    assert records[0].protocol == protocol


@pytest.mark.asyncio
async def test_client_websocket_frame_runtime_log_uses_client_name(caplog):
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    recipient = WebSocketRecipient(
        fed(b'\x82\x7e\xff\xff').reader, _Writer(),
        require_masked=False, max_frame_payload=1024,
        frame_cap_name='client_ws_max_frame_payload',
        message_cap_name='client_ws_max_message_size')
    await recipient.await_connect()
    with pytest.raises(ProtocolError, match='65535'):
        await recipient.next_message()
    records = [r for r in caplog.records
               if getattr(r, 'cap', None) == 'client_ws_max_frame_payload']
    assert len(records) == 1
    assert records[0].protocol == 'ws'
    assert not [r for r in caplog.records
                if getattr(r, 'cap', None) == 'ws_max_frame_payload']


def test_client_websocket_message_runtime_log_uses_client_name(caplog):
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    recipient = WebSocketRecipient(
        _Reader(), _Writer(), require_masked=False, max_message_size=4,
        frame_cap_name='client_ws_max_frame_payload',
        message_cap_name='client_ws_max_message_size')
    with pytest.raises(ProtocolError):
        recipient._refuse_oversized_message(MessageTooLarge(5, 4))
    records = [r for r in caplog.records
               if getattr(r, 'cap', None) == 'client_ws_max_message_size']
    assert len(records) == 1
    assert records[0].protocol == 'ws'


@pytest.mark.asyncio
async def test_client_protocols_inject_client_writer_cap(monkeypatch):
    monkeypatch.setenv('BB_CLIENT_WRITE_TIMEOUT', '2.5')
    h1 = HTTP1Client('localhost', 80)
    h2 = HTTP2Client('localhost', 80)
    assert h1._writer is None and h2._writer is None
    async with served(_ok) as plain, served(_ok, tls=True) as tls:
        connectors = (
            (HTTP1Client('127.0.0.1', plain.server.port), 'http1'),
            (HTTP2Client('127.0.0.1', plain.server.port), 'http2'),
            # Client adopts the connection whichever protocol ALPN chose.
            (Client('127.0.0.1', tls.server.port, ssl=_alpn('h2')), 'http2'),
            (Client('127.0.0.1', tls.server.port, ssl=_alpn('http/1.1')), 'http1'),
        )
        for connector, protocol in connectors:
            async with connector as client:
                assert (await client.request('GET', '/')).status == 204
                assert client._writer._write_timeout == 2.5
                assert client._writer._cap_name == 'client_write_timeout'
                assert client._writer._protocol == protocol


def _alpn(protocol: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    ctx.set_alpn_protocols([protocol])
    return ctx


async def _ok(scope, receive, send):
    await send({'type': 'http.response.start', 'status': 204, 'headers': []})
    await send({'type': 'http.response.body', 'body': b''})


def test_websocket_recipient_accepts_explicit_client_caps():
    recipient = WebSocketRecipient(
        reader=_Reader(), writer=_Writer(), require_masked=False,
        max_frame_payload=123, max_message_size=456,
        frame_cap_name='client_ws_max_frame_payload',
        message_cap_name='client_ws_max_message_size')
    assert recipient._max_frame_payload == 123
    assert recipient._max_message_size == 456
    assert recipient._frame_cap_name == 'client_ws_max_frame_payload'
    assert recipient._message_cap_name == 'client_ws_max_message_size'


async def _no_credit(n: int) -> None:
    pass


def test_h2_websocket_session_injects_client_caps(monkeypatch):
    monkeypatch.setenv('BB_CLIENT_WS_MAX_FRAME_PAYLOAD', '123')
    monkeypatch.setenv('BB_CLIENT_WS_MAX_MESSAGE_SIZE', '456')
    client = HTTP2Client('localhost', 80)
    client._writer = _Writer()
    session = WebSocketH2Session(client, 1, HTTP2StreamReader(1, _no_credit))
    assert session._recipient._max_frame_payload == 123
    assert session._recipient._max_message_size == 456
    assert session._recipient._frame_cap_name == 'client_ws_max_frame_payload'
    assert session._recipient._message_cap_name == 'client_ws_max_message_size'


def test_direct_websocket_session_none_uses_client_caps(monkeypatch):
    monkeypatch.setenv('BB_CLIENT_WS_MAX_FRAME_PAYLOAD', '123')
    monkeypatch.setenv('BB_CLIENT_WS_MAX_MESSAGE_SIZE', '456')
    session = WebSocketSession(_Reader(), _Writer(), ClientConnection(), None)
    assert session._recipient._max_frame_payload == 123
    assert session._recipient._max_message_size == 456
    assert session._recipient._frame_cap_name == 'client_ws_max_frame_payload'
    assert session._recipient._message_cap_name == 'client_ws_max_message_size'


@pytest.mark.asyncio
async def test_client_h2_flow_control_timeout_is_named_and_protocolled(
        monkeypatch, caplog):
    monkeypatch.setenv('BB_CLIENT_WRITE_TIMEOUT', '0.01')
    caplog.set_level(logging.WARNING, logger='blackbull.caps')
    client = HTTP2Client('localhost', 80)
    client._writer = _Writer()
    client._conn_window.size = 0
    sender = client._make_sender(1)
    sender.stream_window_size = 0
    with pytest.raises(FlowControlStalled) as exc_info:
        await sender._write_data(b'x', end_stream=True)
    assert 'BB_CLIENT_WRITE_TIMEOUT' in str(exc_info.value)
    records = [r for r in caplog.records
               if getattr(r, 'cap', None) == 'client_write_timeout']
    assert records and records[-1].protocol == 'http2'


def test_direct_h2_sender_retains_server_cap_defaults():
    sender = HTTP2Sender(_Writer(), FrameFactory(), 1,
                         conn_window=ConnectionWindow())
    assert sender._flow_control_cap == 'write_timeout'


@pytest.mark.asyncio
async def test_direct_h2_sender_timeout_names_server_setting():
    sender = HTTP2Sender(_Writer(), FrameFactory(), 1,
                         conn_window=ConnectionWindow(),
                         flow_control_timeout=0.001)
    sender._conn_window.size = 0
    sender.stream_window_size = 0
    with pytest.raises(FlowControlStalled) as exc_info:
        await sender._write_data(b'x', end_stream=True)
    assert 'BB_WRITE_TIMEOUT' in str(exc_info.value)
