"""A server sender keeps the content rules it advertises (RFC 9110 §15.3.6)."""
from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus

import pytest

from blackbull.protocol.frame import FrameFactory
from blackbull.server.sender import AbstractWriter, ConnectionWindow, HTTP2Sender

pytestmark = pytest.mark.asyncio

DATA = 0


class _Writer(AbstractWriter):
    def __init__(self) -> None:
        self.frames: list[tuple[int, int, int, bytes]] = []

    async def write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            length = int.from_bytes(data[offset:offset + 3], 'big')
            end = offset + 9 + length
            kind, flags = data[offset + 3:offset + 5]
            sid = int.from_bytes(data[offset + 5:offset + 9], 'big')
            self.frames.append((kind, flags, sid, data[offset + 9:end]))
            offset = end

    def body(self, sid: int | None = None) -> bytes:
        return b''.join(payload for kind, _, stream, payload in self.frames
                        if kind == DATA and (sid is None or sid == stream))


def _sender(head_mode: bool = False) -> tuple[HTTP2Sender, _Writer]:
    writer = _Writer()
    sender = HTTP2Sender(writer, FrameFactory(), 1,
                         conn_window=ConnectionWindow(1 << 20),
                         initial_window=1 << 20,
                         flow_control_timeout=0.0,
                         head_mode=head_mode)
    return sender, writer


@pytest.mark.parametrize('status', [204, 205, 304])
async def test_no_content_reaches_the_wire_on_a_bodyless_status(status):
    """RFC 9112 §6.3 rule 1 for 204 and 304, RFC 9110 §15.3.6 for 205: the
    head promises none, so the octets never leave. The in-tree HTTP/2 client
    refuses exactly these frames."""
    sender, writer = _sender()
    await sender(b'abc', HTTPStatus(status))
    assert writer.body() == b''


async def test_no_content_reaches_the_wire_on_a_head_response():
    sender, writer = _sender(head_mode=True)
    await sender(b'abc', HTTPStatus.OK)
    assert writer.body() == b''


async def test_content_still_reaches_the_wire_on_an_ordinary_status():
    sender, writer = _sender()
    await sender(b'abc', HTTPStatus.OK)
    assert writer.body() == b'abc'


def _decoded(writer: _Writer) -> list[tuple[int, dict]]:
    """(frame kind, decoded field block) for the HEADERS frames written."""
    import hpack
    out = []
    decoder = hpack.Decoder()
    for kind, _, _sid, payload in writer.frames:
        if kind == 1:
            pairs = decoder.decode(payload)
            out.append((kind, {str(k): str(v) for k, v in pairs}))
    return out


def _wire(writer: _Writer) -> list[tuple[int, int]]:
    """(frame kind, END_STREAM bit) for every frame written."""
    return [(kind, flags & 0x1) for kind, flags, _, _ in writer.frames]


async def test_the_buffered_path_keeps_the_rules_too():
    """The ASGI arms are the ones production uses, and they write through
    `_write_response_start_and_body` rather than the bytes arm above.

    The flush is scheduled rather than synchronous, so the assert waits a
    tick: without it this test passes against a sender whose rules have been
    deleted outright."""
    sender, writer = _sender()
    await sender({'type': 'http.response.start', 'status': 204, 'headers': [],
                  'trailers': True})
    await sender({'type': 'http.response.body', 'body': b'x',
                  'more_body': True})
    await asyncio.sleep(0)
    assert _wire(writer) == [(1, 1)]


async def test_no_frame_follows_end_stream_on_a_bodyless_status():
    """A second body chunk after the head already carried END_STREAM would be
    STREAM_CLOSED — a protocol error, not merely unwanted content."""
    sender, writer = _sender()
    await sender({'type': 'http.response.start', 'status': 205, 'headers': [],
                  'trailers': True})
    await sender({'type': 'http.response.body', 'body': b'ab',
                  'more_body': True})
    await sender({'type': 'http.response.body', 'body': b'cd',
                  'more_body': True})
    await asyncio.sleep(0)
    assert _wire(writer) == [(1, 1)]


async def test_a_trailer_section_does_not_reach_a_bodyless_status():
    """RFC 9112 §6.3 rule 1 — such a response "cannot contain a message body
    or trailer section", so the head terminates it and the trailers never
    leave. The head terminates the response in place of the trailing section,
    which is the shape a gRPC error takes."""
    sender, writer = _sender()
    await sender({'type': 'http.response.start', 'status': 204, 'headers': [],
                  'trailers': True})
    await sender({'type': 'http.response.body', 'body': b'ab',
                  'more_body': True})
    await sender({'type': 'http.response.trailers',
                  'headers': [(b'x-t', b'1')], 'more_trailers': False})
    await asyncio.sleep(0)
    assert _wire(writer) == [(1, 1)]


async def test_an_informational_head_leaves_the_stream_open_for_the_final_one():
    """An informational response is not the response yet: it carries no
    END_STREAM, so the final response must still be able to close the stream.
    Marking the stream finished here would leak it."""
    sender, writer = _sender()
    await sender(b'', HTTPStatus.CONTINUE)
    await sender(b'hello', HTTPStatus.OK)
    await asyncio.sleep(0)
    assert _wire(writer) == [(1, 0), (1, 0), (0, 1)]


async def test_the_dict_arm_completes_a_final_response_after_an_interim_one():
    """The buffered arms accept a second `start`: a terminal body event
    arriving while the interim head is still the only thing written must not
    mark the stream finished, or the final response is dropped and the
    stream leaks."""
    sender, writer = _sender()
    await sender({'type': 'http.response.start', 'status': 103, 'headers': []})
    await sender({'type': 'http.response.body', 'body': b'',
                  'more_body': False})
    await sender({'type': 'http.response.start', 'status': 200, 'headers': []})
    await sender({'type': 'http.response.body', 'body': b'hello',
                  'more_body': False})
    await asyncio.sleep(0)
    assert _wire(writer) == [(1, 0), (1, 0), (0, 1)]


async def test_an_interim_head_is_written_before_the_next_one_arrives():
    """RFC 9113 §8.1: "Interim responses ... do not end the stream" and are
    part of the exchange they answer.  An interim head is written when it is
    accepted.  Buffering it until a body event lets the next `start`
    overwrite it, and the interim response is never sent at all."""
    sender, writer = _sender()
    await sender({'type': 'http.response.start', 'status': 103, 'headers': []})
    await sender({'type': 'http.response.start', 'status': 200, 'headers': []})
    await sender({'type': 'http.response.body', 'body': b'hello',
                  'more_body': False})
    await asyncio.sleep(0)
    assert _wire(writer) == [(1, 0), (1, 0), (0, 1)]


async def test_an_interim_head_drops_the_fields_the_rule_forbids():
    """RFC 9112 §6.1 forbids Content-Length and Transfer-Encoding in a
    contentless message, and RFC 9113 §8.2.2 keeps connection-specific
    fields out of HTTP/2 entirely. A strict peer treats one as a protocol
    error, so the sender must not forward what the application supplied."""
    sender, writer = _sender()
    await sender({'type': 'http.response.start', 'status': 103, 'headers': [
        (b'content-length', b'5'),
        (b'transfer-encoding', b'chunked'),
        (b'link', b'</s.css>; rel=preload'),
    ]})
    await sender({'type': 'http.response.start', 'status': 200, 'headers': []})
    await asyncio.sleep(0)
    fields = dict(_decoded(writer)[0][1])
    assert fields[':status'] == '103', fields
    assert 'content-length' not in fields, fields
    assert 'transfer-encoding' not in fields, fields
    assert fields['link'] == '</s.css>; rel=preload', fields


async def test_the_native_arm_sends_the_interim_head_it_accepts():
    """The native arm does what the dict arm does — or the docstring that
    says so is a lie. A `NativeResponse` for an interim head must reach the
    wire instead of being overwritten by the final one."""
    from blackbull.server.sender import NativeResponse

    sender, writer = _sender()
    await sender(NativeResponse(status=103, header=[(b'link', b'</s.css>')]))
    await sender(NativeResponse(status=200, header=[], body=b'x'))
    await asyncio.sleep(0)
    statuses = [dict(f)[':status'] for _, f in _decoded(writer)]
    assert statuses[:2] == ['103', '200'], statuses


async def test_a_head_response_is_quiet_about_the_chunks_it_suppresses(caplog):
    """Suppressing the body is the sender doing its job, not an application
    mistake, so it logs nothing. Without this the message blames the app once
    per chunk — a HEAD of a large file becomes a warning per 64 KiB."""
    sender, writer = _sender(head_mode=True)
    with caplog.at_level('WARNING'):
        await sender({'type': 'http.response.start', 'status': 200,
                      'headers': [], 'trailers': True})
        for _ in range(3):
            await sender({'type': 'http.response.body', 'body': b'x',
                          'more_body': True})
        await sender({'type': 'http.response.trailers',
                      'headers': [(b'x-t', b'1')], 'more_trailers': False})
    await asyncio.sleep(0)
    assert _wire(writer) == [(1, 1)]
    assert caplog.records == []


@pytest.mark.asyncio
async def test_a_plain_asgi_dict_still_reaches_the_wire():
    """A plain ASGI app speaks dicts, and hosting one is a public feature.
    The senders convert at the entry, so this pins that the conversion is
    the one doing the work — not a shape that quietly stopped arriving."""
    sender, writer = _sender()
    await sender({'type': 'http.response.start', 'status': 200,
                  'headers': [(b'content-length', b'2')]})
    await sender({'type': 'http.response.body', 'body': b'ok'})
    await asyncio.sleep(0)

    assert _decoded(writer), 'a plain ASGI dict never reached the wire'
