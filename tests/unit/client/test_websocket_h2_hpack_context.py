"""One HPACK context per connection — RFC 7541 §2.3, RFC 9113 §4.3.

The dynamic table is connection state, and the peer keeps exactly **one**
decoder for it.  Two encoders writing header blocks to the same connection
therefore keep two tables that diverge as soon as their writes interleave,
and the peer resolves an index against whichever table its single decoder
built — so a field indexed by one encoder is read back as a field another
encoder inserted.

That is what these tests pin, from two directions:

* **the seam** — the factory a ``WebSocketH2Client`` and its
  ``WebSocketH2Session`` use *is* the ``HTTP2Client``'s, by identity, and
  neither holds a second one; and
* **the wire** — a peer with a single decoder reads every block as sent,
  across the A→B→A interleaving that makes divergence observable.

A→B→A is the minimum sequence.  A block is decoded correctly by a
divergent table right up until a *foreign* entry lands above one of the
encoder's own, so the first block from each encoder decodes correctly even
when the connection carries two of them.  What lands wrong is the **second**
block from the first encoder, after the second encoder has inserted.  A test
asserting on the first blocks — or one giving each encoder its own decoder —
passes against a connection that has been corrupting header blocks all along.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.client.http2 import HTTP2Client
from blackbull.client.websocket_h2 import WebSocketH2Client, WebSocketH2Session
from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (FrameTypes, HeaderFrameFlags,
                                            PseudoHeaders)
from blackbull.server.sender import AbstractWriter

# Under ``--beartype-packages=blackbull`` the wrapper is ``(*args, **kwargs)``,
# so a fourth positional argument is reported as a type violation on the
# parameter it landed on rather than as an arity ``TypeError``.  Kept as the
# optional-import dance the rest of the suite uses so the file still runs
# without beartype installed.
try:
    from beartype.roar import BeartypeCallHintParamViolation as _BeartypeViolation
except ImportError:
    _BeartypeViolation = None  # type: ignore[assignment,misc]

_TYPE_ERRORS = (TypeError,) if _BeartypeViolation is None else (TypeError, _BeartypeViolation)

_HOST = 'localhost'
_PORT = 1
_AUTHORITY = f'{_HOST}:{_PORT}'
_WS_PATH = '/ws'
_REQ_PATH = '/probe'
_PROBE = ('x-secret', 'alpha-token')


class _CaptureWriter(AbstractWriter):
    """Records every byte the client puts on the wire, in order."""

    def __init__(self) -> None:
        self.data = bytearray()

    async def write(self, data: bytes) -> None:
        self.data += data


def _wired_pair(stream_id: int = 1):
    """A ``WebSocketH2Client`` over a connected-looking ``HTTP2Client``.

    Stands in for ``__aenter__``, which is where the real connection —
    and so the connection's HPACK context — comes into being.
    """
    client = HTTP2Client(_HOST, _PORT)
    writer = _CaptureWriter()
    client._writer = writer
    ws = WebSocketH2Client(_HOST, _PORT, stream_id=stream_id)
    ws._client = client
    return client, ws, writer


async def _until_written(writer: _CaptureWriter, coro) -> tuple[asyncio.Future, bytes]:
    """Run *coro* until it has put a frame on the wire; return the new bytes.

    Both ``request()`` and ``connect()`` park on an answer that no receive
    loop is going to bring here.  The send is what this file is about, so the
    task is abandoned once its bytes are out.
    """
    before = len(writer.data)
    task = asyncio.ensure_future(coro)
    for _ in range(200):
        await asyncio.sleep(0)
        if len(writer.data) > before:
            break
    else:  # pragma: no cover — a send that never reached the writer
        task.cancel()
        raise AssertionError('nothing was written to the wire')
    return task, bytes(writer.data[before:])


async def _abandon(*tasks: asyncio.Future) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _peer_reads(peer: FrameFactory, blob: bytes) -> list[dict]:
    """Every header block in *blob*, as a peer with ONE decoder reads it.

    *peer* is that decoder's owner and is deliberately reused across the
    whole byte stream: a fresh ``FrameFactory`` per block would rebuild the
    dynamic table each time and read divergent blocks correctly.
    """
    out: list[dict] = []
    buf = blob
    while buf:
        length = int.from_bytes(buf[:3], 'big')
        frame = peer.load(buf[:9 + length])
        if frame.FrameType() == FrameTypes.HEADERS:
            out.append({
                'pseudo': {k.name: v for k, v in frame.pseudo_headers.items()},
                'fields': [(k.decode(), v.decode()) for k, v in frame.headers],
                'malformed': frame.malformed_reason,
            })
        buf = buf[9 + length:]
    return out


def _sent_request_block() -> dict:
    return {
        'pseudo': {'METHOD': 'GET', 'PATH': _REQ_PATH,
                   'SCHEME': 'http', 'AUTHORITY': _AUTHORITY},
        'fields': [_PROBE],
        'malformed': None,
    }


def _sent_connect_block() -> dict:
    return {
        'pseudo': {'METHOD': 'CONNECT', 'PROTOCOL': 'websocket',
                   'SCHEME': 'http', 'PATH': _WS_PATH,
                   'AUTHORITY': _AUTHORITY},
        'fields': [],
        'malformed': None,
    }


def _connect_response(peer: FrameFactory, stream_id: int, status: str = '200'):
    """The server's ``:status`` answer to Extended CONNECT.

    Handed to the client as a frame object on its raw-stream queue, which is
    where its receive loop would put it.
    """
    frame = peer.create(FrameTypes.HEADERS, HeaderFrameFlags.END_HEADERS,
                        stream_id)
    frame.pseudo_headers[PseudoHeaders.STATUS] = status
    return frame


# ═══════════════════════════════════════════════════════════════════════
# The seam — no second context exists to diverge
# ═══════════════════════════════════════════════════════════════════════

class TestOneContextPerConnection:
    def test_ws_client_uses_the_connections_factory(self):
        """Identity, not equality: a second ``FrameFactory`` with the same
        settings is still a second dynamic table."""
        client, ws, _writer = _wired_pair()
        assert ws.frame_factory is client.frame_factory

    def test_ws_client_shares_the_connections_hpack_codecs(self):
        """What must be shared is the codec pair, which is what holds the
        table — naming them makes the reason the identity matters explicit."""
        client, ws, _writer = _wired_pair()
        assert ws.frame_factory.encoder is client.frame_factory.encoder
        assert ws.frame_factory.decoder is client.frame_factory.decoder

    def test_ws_client_holds_no_second_factory(self):
        """The defect was a ``FrameFactory()`` built in ``__init__`` because
        the connection did not exist yet.  Nothing may hold one but the
        connection."""
        client, ws, _writer = _wired_pair()
        strays = [name for name, value in vars(ws).items()
                  if isinstance(value, FrameFactory)
                  and value is not client.frame_factory]
        assert not strays, f'second HPACK context on {strays}'

    @pytest.mark.asyncio
    async def test_session_uses_the_connections_factory(self):
        client, ws, writer = _wired_pair()
        peer = FrameFactory()
        task, _block = await _until_written(writer, ws.connect(_WS_PATH))
        client._raw_streams[1].put_nowait(_connect_response(peer, 1))
        session = await asyncio.wait_for(task, 2)
        assert session._factory is client.frame_factory
        strays = [name for name, value in vars(session).items()
                  if isinstance(value, FrameFactory)
                  and value is not client.frame_factory]
        assert not strays, f'second HPACK context on {strays}'

    def test_session_cannot_be_handed_a_different_context(self):
        """A session on a connection has no choice about which context it
        uses, so the constructor takes none — a parameter that can be passed
        wrongly is the same hole with a default."""
        client, _ws, _writer = _wired_pair()
        queue = client.register_raw_stream(3)
        with pytest.raises(_TYPE_ERRORS):
            WebSocketH2Session(client, FrameFactory(), 3, queue)  # type: ignore[arg-type]

    def test_frame_factory_before_aenter_names_the_context_manager(self):
        """There is no context to hand out before the connection exists, and
        the refusal has to be readable — ``None`` has no ``frame_factory``."""
        ws = WebSocketH2Client(_HOST, _PORT)
        with pytest.raises(RuntimeError, match='async context manager'):
            ws.frame_factory


# ═══════════════════════════════════════════════════════════════════════
# The wire — a single-decoder peer reads every block as sent
# ═══════════════════════════════════════════════════════════════════════

class TestSingleDecoderPeerReadsEveryBlock:
    @pytest.mark.asyncio
    async def test_request_connect_request_all_read_as_sent(self):
        """A→B→A on one connection: ``request()``, the WebSocket Extended
        CONNECT, then ``request()`` again.

        The third block is the one with teeth.  Encoded against a second,
        private table it indexes an entry the peer's single decoder no longer
        has at that position, and the peer reads the CONNECT's ``:path`` and
        ``:protocol`` inside an ordinary request while that request's own
        field disappears.
        """
        client, ws, writer = _wired_pair()
        peer = FrameFactory()  # one decoder, as RFC 9113 §4.3 gives a peer

        t_a1, block_a1 = await _until_written(
            writer, client.request('GET', _REQ_PATH, headers=[_PROBE]))
        t_b, block_b = await _until_written(writer, ws.connect(_WS_PATH))
        client._raw_streams[1].put_nowait(_connect_response(peer, 1))
        session = await asyncio.wait_for(t_b, 2)
        t_a2, block_a2 = await _until_written(
            writer, client.request('GET', _REQ_PATH, headers=[_PROBE]))
        await _abandon(t_a1, t_a2)

        read = _peer_reads(peer, block_a1 + block_b + block_a2)
        assert len(read) == 3, 'expected one header block per send'
        assert read[0] == _sent_request_block()
        assert read[1] == _sent_connect_block()
        # Correct even against a divergent table — see the module docstring.
        assert read[2] == _sent_request_block(), (
            'the peer resolved the second request against a table a second '
            'encoder had shifted')
        assert session is not None
