"""The actor→sender "peer is gone" signal.

This used to travel as an ``http.disconnect`` dict pushed down the *send*
channel — a receive-side ASGI event sent the wrong way through the pipe,
because that pipe happened to be there.  It is now
:meth:`BaseSender.mark_client_gone`.

What the tests hold onto is the behaviour that mattered, not the spelling: a
sender told the peer is gone stops putting bytes on a dead transport, before
or after a response has completed.  The last test is the one with teeth — it
pins that the old encoding is *gone*, so nothing quietly re-adds an arm that
lets an application close its own connection by sending a dict.
"""
import pytest

from blackbull.asgi import ASGIEvent
from blackbull.headers import Headers
from blackbull.server.sender import SenderFactory


class _Writer:
    """Records what actually reached the transport."""

    def __init__(self):
        self.written = bytearray()

    def write(self, data) -> None:
        self.written += bytes(data)

    def writelines(self, parts) -> None:
        for p in parts:
            self.written += bytes(p)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def get_extra_info(self, name, default=None):
        return default


def _sender():
    w = _Writer()
    return SenderFactory.http1(w), w


@pytest.mark.asyncio
async def test_a_sender_told_the_client_is_gone_writes_nothing_further():
    """The whole point of the signal: the actor learned the connection is
    dead, so the response still in flight is dropped rather than raising a
    broken pipe out of ``_write``."""
    send, w = _sender()

    send.mark_client_gone()
    await send(b'body that missed its window')

    assert w.written == b''


@pytest.mark.asyncio
async def test_the_signal_still_lands_after_a_response_completed():
    """A completed response used to be an early ``return`` that only honoured
    the disconnect because a branch was threaded through it.  As a method the
    signal no longer has to survive that path — it just applies."""
    send, w = _sender()
    await send(b'hello', headers=Headers([(b'content-type', b'text/plain')]))
    sent = len(w.written)
    assert sent > 0

    send.mark_client_gone()
    await send(b'second response')

    assert len(w.written) == sent


@pytest.mark.asyncio
async def test_http_disconnect_is_no_longer_accepted_on_the_send_channel():
    """The de-smuggling, pinned.

    ``http.disconnect`` is a *receive* event; a sender that still honoured it
    would let anything holding ``send`` — an application, a middleware —
    silently close the connection by sending a dict.  It must now be treated
    as what it is: an unknown send event, logged and dropped, leaving the
    sender open.
    """
    send, w = _sender()

    await send({'type': ASGIEvent.HTTP_DISCONNECT})

    assert not send._closed
    await send(b'still writable')
    assert w.written != b''
