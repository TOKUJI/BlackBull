"""Drivers that push a real server or client past a cap and report the answer."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib

from hpack import Decoder

from blackbull import BlackBull
from blackbull.client import HTTP1Client, HTTP2Client, ProtocolError, WebSocketClient
from blackbull.mqtt import MQTTExtension
from blackbull.mqtt.messages import (MQTTConnect, MQTTPuback, MQTTPublish, MQTTSubscribe,
                                     decode_packet, encode_packet)
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes
from blackbull.router import Scheme
from blackbull.server.ws_codec import WSOpcode, encode_frame
from tests.conformance.http2._harness import _make_h2_frame, _make_headers_frame, h2_connect
from tests.harness import peer, read_h2_frame, served

_WS_GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

WS_UPGRADE = (b'GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n'
              b'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
              b'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n')


def make_app() -> BlackBull:
    """``/`` reads the whole body and answers 200, ``/seen`` answers the body's
    last event type, ``/big`` sends 64 MiB,
    ``/hold`` never reads for two seconds, ``/ws`` accepts and reads."""
    app = BlackBull()

    @app.route(path='/', methods=['GET', 'POST'])
    async def drain(conn, receive, send):
        while (await receive()).get('more_body'):
            pass
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})

    @app.route(path='/seen', methods=['POST'])
    async def seen(conn, receive, send):
        while (event := await receive()).get('more_body'):
            pass
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': event['type'].encode()})

    @app.route(path='/big')
    async def big(conn, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': [
            (b'content-length', b'%d' % (64 << 20))]})
        chunk = bytes(1 << 20)
        for _ in range(64):
            await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
        await send({'type': 'http.response.body', 'body': b''})

    @app.route(path='/hold', methods=['GET', 'POST'])
    async def hold(conn, receive, send):
        await asyncio.sleep(2)            # never reads: DATA stays unconsumed

    @app.route(path='/ws', scheme=Scheme.websocket)
    async def ws_read(conn, receive, send):
        await receive()
        await send({'type': 'websocket.accept'})
        while (await receive())['type'] != 'websocket.disconnect':
            pass

    return app


def head(*fields: bytes, method: bytes = b'GET', target: bytes = b'/') -> bytes:
    return b'\r\n'.join([method + b' ' + target + b' HTTP/1.1', b'Host: x', *fields, b'', b''])


async def http1(*sends: bytes, app=None, pause: float = 0.0, **server_kwargs) -> str:
    """The status of the first response, or 'close' when the server hangs up
    without one.  *pause* waits between sends."""
    async with served(app or make_app(), **server_kwargs) as s:
        wire = await s.wire()
        for data in sends:
            await wire.send(data)
            if pause:
                await asyncio.sleep(pause)
        try:
            return str((await wire.response()).status)
        except (asyncio.IncompleteReadError, ConnectionError):
            return 'close'


async def http1_seen(*sends: bytes, pause: float = 0.0) -> str:
    """'<last body event the handler saw>, closed|open' for a POST to ``/seen``
    whose body is *sends*, sent *pause* apart after the head."""
    async with served(make_app()) as s:
        wire = await s.wire()
        for data in sends:
            await wire.send(data)
            await asyncio.sleep(pause)
        seen = (await wire.response()).body.decode()
        try:
            closed = await asyncio.wait_for(wire.reader.read(1), 1) == b''
        except TimeoutError:
            closed = False
        return f'{seen}, {"closed" if closed else "open"}'


async def ws(*frames: bytes, pause: float = 0.0) -> str:
    """'close <code>' for the CLOSE the server sends after *frames*."""
    async with served(make_app()) as s:
        wire = await s.wire()
        await wire.send(WS_UPGRADE)
        await wire.recv_until()
        for frame in frames:
            await wire.send(frame)
            if pause:
                await asyncio.sleep(pause)
        while True:
            first, second = await wire.recv_exactly(2)
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(await wire.recv_exactly(2), 'big')
            elif length == 127:
                length = int.from_bytes(await wire.recv_exactly(8), 'big')
            payload = await wire.recv_exactly(length)
            if first & 0x0F == WSOpcode.CLOSE:
                return f'close {int.from_bytes(payload[:2], "big")}'


def client_frame(payload: bytes, opcode=WSOpcode.BINARY, *, fin: bool = True) -> bytes:
    """A masked client frame; *fin* False opens or continues a fragmented message."""
    frame = bytearray(encode_frame(payload, opcode=opcode, mask=True))
    if not fin:
        frame[0] &= 0x7F
    return bytes(frame)


async def h2(*frames: bytes, probe: bool = False, pause: float = 0.0,
             **server_kwargs) -> str:
    """The first decisive frame the server answers *frames* with:
    'RST_STREAM <code>', 'GOAWAY <code>' or ':status <n>'.  With *probe*, a
    PING follows and 'ignored' means it was answered and nothing else was.
    *pause* waits between frames."""
    async with served(make_app(), **server_kwargs) as s:
        wire = await h2_connect(s)
        if pause:
            for frame in frames:
                await wire.send(frame)
                await asyncio.sleep(pause)
        else:
            await wire.send(b''.join(frames))
        if probe:
            await wire.send(_make_h2_frame(FrameTypes.PING, 0, 0, b'p' * 8))
        return await _h2_answer(wire)


async def _h2_answer(wire) -> str:
    decoder = Decoder()
    while True:
        kind, flags, _, payload = await read_h2_frame(wire.reader)
        if kind == FrameTypes.RST_STREAM.value[0]:
            return f'RST_STREAM {ErrorCodes(int.from_bytes(payload[:4], "big")).name}'
        if kind == FrameTypes.GOAWAY.value[0]:
            return f'GOAWAY {ErrorCodes(int.from_bytes(payload[4:8], "big")).name}'
        if kind == FrameTypes.HEADERS.value[0] and not flags & 1:
            fields = dict(decoder.decode(payload, raw=True))
            if fields.get(b':status', b'200') != b'200':
                return f':status {fields[b":status"].decode()}'
        if kind == FrameTypes.PING.value[0] and flags & 1 and payload == b'p' * 8:
            return 'ignored'
        if kind == FrameTypes.DATA.value[0] and flags & 1:
            return 'complete'


def h2_request(sid: int = 1, *fields: tuple[bytes, bytes], method: bytes = b'POST',
               path: bytes = b'/', end_stream: bool = False) -> bytes:
    return _make_headers_frame(sid, end_stream=end_stream, fields=[
        (b':method', method), (b':path', path), (b':scheme', b'http'),
        (b':authority', b'x'), *fields])


def h2_data(sid: int, payload: bytes, *, end_stream: bool = False) -> bytes:
    return _make_h2_frame(FrameTypes.DATA, int(end_stream), sid, payload)


# -- MQTT -----------------------------------------------------------------

async def _mqtt_packet(reader: asyncio.StreamReader) -> bytes:
    first = await reader.readexactly(1)
    length, shift, raw = 0, 0, b''
    while True:
        byte = await reader.readexactly(1)
        raw += byte
        length |= (byte[0] & 0x7F) << shift
        shift += 7
        if not byte[0] & 0x80:
            break
    return first + raw + await reader.readexactly(length)


async def mqtt(*sessions, answer: type, broker=None) -> str:
    """'<TYPE> 0x<reason>' for the first refusal (reason >= 0x80) an *answer*
    packet carries on the last of *sessions* (each a sequence of packets),
    or 'close'.  Earlier sessions connect, then hang up."""
    app = BlackBull()
    app.add_extension(MQTTExtension(port=0))
    async with served(app) as s:
        port = s.server.protocol_ports['mqtt']
        for i, packets in enumerate(sessions):
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            try:
                for packet in packets:
                    writer.write(packet if isinstance(packet, bytes) else encode_packet(packet))
                await writer.drain()
                if i < len(sessions) - 1:
                    await _mqtt_packet(reader)          # CONNACK
                    continue
                while True:
                    try:
                        message = decode_packet(await _mqtt_packet(reader))[0]
                    except (asyncio.IncompleteReadError, ConnectionError):
                        return 'close'
                    if not isinstance(message, answer):
                        continue
                    codes = getattr(message, 'reason_codes', None) or [message.reason_code]
                    refused = [int(c) for c in codes if int(c) >= 0x80]
                    if refused:
                        return f'{message.packet_type.name} 0x{refused[0]:02X}'
            finally:
                writer.close()


def connect(client_id: str = 'c', **properties) -> MQTTConnect:
    return MQTTConnect(client_id=client_id, clean_start=True, keep_alive=60,
                       properties=properties)


# -- clients ----------------------------------------------------------------

async def client_http1(*response: bytes, hold: bool = False, pause: float = 0.0) -> str:
    """The exception ``HTTP1Client.request`` raises for an origin that
    answers *response*, its pieces *pause* apart (then stays connected when
    *hold*), or 'ok'."""
    async def origin(reader, writer):
        await reader.readuntil(b'\r\n\r\n')
        for piece in response:
            writer.write(piece)
            await writer.drain()
            await asyncio.sleep(pause)
        if hold:
            await reader.read()

    async with peer(origin) as port:
        try:
            async with HTTP1Client('127.0.0.1', port) as client:
                await client.request('GET', '/')
        except Exception as exc:
            return type(exc).__name__
    return 'ok'


async def client_h2(*frames: bytes, report: str = 'raised', pause: float = 0.0) -> str:
    """Against an origin that answers the client's first request with
    *frames*: the exception ``request`` raised, or with *report* 'sent', the
    first RST_STREAM or GOAWAY the client sent back."""
    sent: list[str] = []
    answered = asyncio.Event()

    async def origin(reader, writer):
        await reader.readexactly(24)
        writer.write(_make_h2_frame(FrameTypes.SETTINGS))
        await writer.drain()
        while True:
            kind, _, sid, _ = await read_h2_frame(reader)
            if kind == FrameTypes.HEADERS.value[0] and sid == 1:
                break
        for frame in frames:
            writer.write(frame)
            await writer.drain()
            await asyncio.sleep(pause)
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            while True:
                kind, _, _, payload = await read_h2_frame(reader)
                if kind == FrameTypes.RST_STREAM.value[0]:
                    sent.append(f'RST_STREAM {ErrorCodes(int.from_bytes(payload[:4], "big")).name}')
                    answered.set()
                elif kind == FrameTypes.GOAWAY.value[0]:
                    sent.append(f'GOAWAY {ErrorCodes(int.from_bytes(payload[4:8], "big")).name}')
                    answered.set()

    raised = 'ok'
    async with peer(origin) as port:
        try:
            async with HTTP2Client('127.0.0.1', port) as client:
                try:
                    await client.request('GET', '/')
                finally:
                    if report == 'sent':     # the frame may still be in flight
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(answered.wait(), 2)
        except Exception as exc:
            raised = type(exc).__name__
    return sent[0] if report == 'sent' and sent else raised


def h2_response(sid: int = 1, *fields: tuple[bytes, bytes], end_stream: bool = False,
                status: bytes = b'200') -> bytes:
    return _make_headers_frame(sid, end_stream=end_stream,
                               fields=[(b':status', status), *fields])


async def client_ws(*server_frames: bytes) -> str:
    """'close <code>' for the CLOSE a ``WebSocketClient`` sends back after the
    server sends *server_frames*."""
    closes: list[str] = []

    async def origin(reader, writer):
        request = await reader.readuntil(b'\r\n\r\n')
        key = next(line.split(b':', 1)[1].strip() for line in request.split(b'\r\n')
                   if line.lower().startswith(b'sec-websocket-key:'))
        accept = base64.b64encode(hashlib.sha1(key + _WS_GUID).digest())
        writer.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                     b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        writer.write(b''.join(server_frames))
        await writer.drain()
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            while True:
                first, second = await reader.readexactly(2)
                length = second & 0x7F
                if length == 126:
                    length = int.from_bytes(await reader.readexactly(2), 'big')
                mask = await reader.readexactly(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(
                    await reader.readexactly(length)))
                if first & 0x0F == WSOpcode.CLOSE:
                    closes.append(f'close {int.from_bytes(payload[:2], "big")}')
                    return

    async with peer(origin) as port:
        async with WebSocketClient('127.0.0.1', port) as client:
            session = await client.connect('/')
            with contextlib.suppress(ProtocolError):    # the caller hears the refusal
                await asyncio.wait_for(session.receive(), 5)
            await session.close()
    return closes[0] if closes else 'no close'


def server_frame(payload: bytes, opcode=WSOpcode.BINARY, *, fin: bool = True) -> bytes:
    frame = bytearray(encode_frame(payload, opcode=opcode))
    if not fin:
        frame[0] &= 0x7F
    return bytes(frame)


async def mqtt_subscriber_fate(*publishes: MQTTPublish) -> str:
    """Whether a subscriber that stops reading survives *publishes* to its topic:
    'closed' when the broker ends its connection, else 'open'."""
    app = BlackBull()
    app.add_extension(MQTTExtension(port=0))
    async with served(app) as s:
        port = s.server.protocol_ports['mqtt']
        sub_r, sub_w = await asyncio.open_connection('127.0.0.1', port)
        sub_w.write(encode_packet(connect('sub')))
        sub_w.write(encode_packet(MQTTSubscribe(packet_id=1, subscriptions=[('t', 0)])))
        await sub_w.drain()
        await _mqtt_packet(sub_r)                         # CONNACK
        await _mqtt_packet(sub_r)                         # SUBACK
        pub_r, pub_w = await asyncio.open_connection('127.0.0.1', port)
        pub_w.write(encode_packet(connect('pub')))
        for publish in publishes:
            pub_w.write(encode_packet(publish))
        await pub_w.drain()
        await asyncio.sleep(1.0)                          # the subscriber has read nothing
        try:
            while await asyncio.wait_for(sub_r.read(1 << 20), 2):
                pass
            fate = 'closed'
        except TimeoutError:
            fate = 'open'
        except ConnectionError:
            fate = 'closed'
        sub_w.close()
        pub_w.close()
        return fate


async def mqtt_acknowledged(*publishes: MQTTPublish) -> str:
    """'<n> acknowledged' for the PUBACKs one connection receives for *publishes*."""
    app = BlackBull()
    app.add_extension(MQTTExtension(port=0))
    async with served(app) as s:
        reader, writer = await asyncio.open_connection('127.0.0.1', s.server.protocol_ports['mqtt'])
        writer.write(encode_packet(connect()) + b''.join(encode_packet(p) for p in publishes))
        await writer.drain()
        acked = 0
        with contextlib.suppress(TimeoutError):
            while acked < len(publishes):
                if isinstance(decode_packet(await asyncio.wait_for(_mqtt_packet(reader), 2))[0],
                              MQTTPuback):
                    acked += 1
        writer.close()
        return f'{acked} acknowledged'
