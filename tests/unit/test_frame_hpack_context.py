"""A header frame cannot invent its own HPACK context — RFC 7541 §2.3, RFC 9113 §4.3.

The dynamic table is connection state and the peer keeps exactly one decoder
for it, so *every* header block written to a connection has to come from that
connection's one encoder, and every block read has to reach that connection's
one decoder.  ``FrameFactory`` is where the pair lives; a frame that cannot
name it has no way to be right.

``Headers`` and ``PushPromise`` used to paper over a missing codec twice, in
opposite directions and both silently:

* on the send path, a missing encoder was replaced by a fresh ``Encoder()`` —
  a private table whose indices the peer resolves against the table its one
  decoder actually built;
* on the receive path, ``PushPromise`` returned without decoding when the
  decoder was missing — and RFC 9113 §4.3 requires the promised block be
  decoded precisely because the table update is connection-wide.

Both produce valid-looking bytes and raise on neither side.  What is pinned
here is that each one now refuses, and that the refusal says the frame belongs
to a connection's context rather than that an argument was missing.

The wire tests need the interleaving that makes divergence observable: a
private context is read correctly right up until a *foreign* entry lands above
one of its own, so it is the **second** block from the long-lived encoder that
comes back wrong.  A connection where every block comes from its own throwaway
encoder never references an index it did not itself insert and stays readable
throughout — which is why the fault-injection scenario client is not this
defect, and why a test without a long-lived encoder on the connection would
prove nothing.
"""
from __future__ import annotations

import pytest
from hpack import Encoder

from blackbull.protocol.frame import FrameFactory
from blackbull.protocol.frame_types import (
    FrameTypes,
    HeaderFrameFlags,
    Headers,
    PseudoHeaders,
    PushPromise,
)

_SECRET = b'x-secret'
_ALPHA = b'alpha-token'
_BETA = b'beta-token'

#: The refusal has to teach the invariant, not report a missing argument: a
#: caller reading it should come away knowing the frame belongs to a
#: connection's HPACK context and that ``FrameFactory`` is what supplies it.
_TAUGHT = r'HPACK.*connection'
_TOLD_WHERE_TO_LOOK = r'FrameFactory'


def _outbound_headers(encoder, stream_id: int, method: str, secret: bytes) -> Headers:
    """A HEADERS frame built to be sent, as an external caller would build it."""
    frame = Headers(length=0, type_=FrameTypes.HEADERS.value,
                    flags=int(HeaderFrameFlags.END_HEADERS),
                    stream_id=stream_id, encoder=encoder)
    frame.pseudo_headers[PseudoHeaders.METHOD] = method
    frame.headers.append((_SECRET, secret))
    return frame


def _outbound_push_promise(encoder, stream_id: int, promised: int) -> PushPromise:
    frame = PushPromise(length=0, type_=FrameTypes.PUSH_PROMISE.value,
                        flags=0, stream_id=stream_id,
                        data=promised.to_bytes(4, 'big'), encoder=encoder)
    frame.pseudo_headers[PseudoHeaders.METHOD] = 'GET'
    frame.headers.append((_SECRET, _ALPHA))
    return frame


def _as_read(peer: FrameFactory, wire: bytes) -> list[tuple[bytes, bytes]]:
    """The fields a peer with ONE decoder gets out of *wire*.

    *peer* is reused across the whole sequence on purpose: a fresh factory per
    block would rebuild the dynamic table each time and read a divergent block
    correctly, which is the shape of test that let this ship.
    """
    frame = peer.load(wire)
    return ([(f':{k.name.lower()}'.encode(), v.encode())
             for k, v in frame.pseudo_headers.items()] + list(frame.headers))


# ═══════════════════════════════════════════════════════════════════════
# The refusal — a frame with no context says so, and says why
# ═══════════════════════════════════════════════════════════════════════

class TestAFrameWithNoContextRefuses:
    def test_headers_will_not_encode_without_the_connections_encoder(self):
        frame = _outbound_headers(None, 1, 'GET', _ALPHA)
        with pytest.raises(TypeError, match=_TAUGHT):
            frame.save()

    def test_push_promise_will_not_encode_without_the_connections_encoder(self):
        frame = _outbound_push_promise(None, 1, 2)
        with pytest.raises(TypeError, match=_TAUGHT):
            frame.save()

    def test_headers_will_not_decode_without_the_connections_decoder(self):
        block = Encoder().encode([(':method', 'GET'), ('x-secret', 'alpha')])
        frame = Headers(length=len(block), type_=FrameTypes.HEADERS.value,
                        flags=0, stream_id=1, data=block,
                        decoder=None, encoder=Encoder())
        with pytest.raises(TypeError, match=_TAUGHT):
            frame.parse_payload()

    def test_push_promise_will_not_skip_the_promised_block(self):
        """RFC 9113 §4.3 — the table update is the point of decoding a block
        the client acts on no further.  Returning without decoding leaves the
        connection's decoder one insertion behind its peer for good."""
        block = Encoder().encode([(':method', 'GET'), (':path', '/pushed')])
        payload = (2).to_bytes(4, 'big') + block
        with pytest.raises(TypeError, match=_TAUGHT):
            PushPromise(length=len(payload), type_=FrameTypes.PUSH_PROMISE.value,
                        flags=int(HeaderFrameFlags.END_HEADERS), stream_id=1,
                        data=payload, decoder=None, encoder=None)

    @pytest.mark.parametrize('build', [
        pytest.param(lambda: _outbound_headers(None, 1, 'GET', _ALPHA).save(),
                     id='headers-encode'),
        pytest.param(lambda: _outbound_push_promise(None, 1, 2).save(),
                     id='push-promise-encode'),
    ])
    def test_the_refusal_names_the_factory_that_owns_the_context(self, build):
        """A caller who sees this should learn where the codec comes from.
        "missing argument" would send them to the signature instead."""
        with pytest.raises(TypeError, match=_TOLD_WHERE_TO_LOOK):
            build()

    def test_a_frame_from_the_factory_carries_the_connections_pair(self):
        """The seam: nothing has to remember to pass the codecs, because the
        one construction path in the tree passes both by identity."""
        factory = FrameFactory()
        frame = factory.create(FrameTypes.HEADERS,
                               HeaderFrameFlags.END_HEADERS, 1)
        assert frame.encoder is factory.encoder
        assert frame.decoder is factory.decoder


# ═══════════════════════════════════════════════════════════════════════
# The wire — what a second context does to the block after next
# ═══════════════════════════════════════════════════════════════════════

class TestASecondContextCorruptsTheThirdBlock:
    def test_a_private_encoder_makes_the_peer_read_fields_nobody_sent(self):
        """A→B→A, where B is a context of its own.

        This is the hazard the refusal exists for, produced through the real
        ``Headers.save()`` by handing it a private encoder explicitly — which
        is byte-for-byte what the removed fallback substituted.  Nothing here
        raises; the corruption is only visible in what the peer reads.
        """
        peer = FrameFactory()          # one decoder, as RFC 9113 §4.3 gives a peer
        connection = FrameFactory()    # the connection's one encoder

        a1 = _outbound_headers(connection.encoder, 1, 'CONNECT', _ALPHA).save()
        b = _outbound_headers(Encoder(), 3, 'GET', _BETA).save()
        a2 = _outbound_headers(connection.encoder, 5, 'CONNECT', _ALPHA).save()

        sent = [(b':method', b'CONNECT'), (_SECRET, _ALPHA)]
        assert _as_read(peer, a1) == sent, 'the first block is read correctly ...'
        assert _as_read(peer, b) == [(b':method', b'GET'), (_SECRET, _BETA)], \
            '... and so is the private context\'s own first block'

        corrupt = _as_read(peer, a2)
        assert corrupt != sent, 'the divergence has to be observable to be a test'
        assert (_SECRET, _BETA) in corrupt, \
            'the other request\'s field is what lands in this one'
        assert (b':method', b'CONNECT') not in corrupt, \
            'and this request\'s own pseudo-header is what vanishes'

    def test_the_context_free_route_to_that_wire_is_closed(self):
        """The same sequence with the middle block's encoder left out.  It
        cannot be built at all now, so the bytes above have no way to reach a
        connection by accident."""
        connection = FrameFactory()
        _outbound_headers(connection.encoder, 1, 'CONNECT', _ALPHA).save()
        with pytest.raises(TypeError, match=_TAUGHT):
            _outbound_headers(None, 3, 'GET', _BETA).save()

    def test_a_connection_with_no_long_lived_encoder_stays_readable(self):
        """Why a throwaway encoder is not a defect by itself, and why the test
        above needs the connection to hold a long-lived one.

        An encoder that emits exactly one block never references a dynamic
        index it did not itself insert, so every block is literals and static
        references and the peer reads all of them as sent.  The fault-injection
        scenario client is this shape.
        """
        peer = FrameFactory()
        for stream_id, method, secret in ((1, 'CONNECT', _ALPHA),
                                          (3, 'GET', _BETA),
                                          (5, 'CONNECT', _ALPHA)):
            wire = _outbound_headers(Encoder(), stream_id, method, secret).save()
            assert _as_read(peer, wire) == [(f':method'.encode(), method.encode()),
                                            (_SECRET, secret)]


class TestASkippedPromisedBlockDesynchronisesTheTable:
    def test_a_promised_block_that_never_reached_the_decoder_shifts_every_index(self):
        """The receive-side hazard, in the same shape.

        A PUSH_PROMISE whose block is not decoded leaves the connection's
        decoder short one set of insertions, so the next block the peer indexes
        resolves against entries from an older request.  Built with
        END_HEADERS clear so nothing decodes it — the state the silent return
        used to produce.
        """
        peer_encoder = Encoder()
        connection = FrameFactory()

        # The connection is already carrying table state from a request.
        connection.decoder.decode(
            peer_encoder.encode([(':method', 'GET'), (':authority', 'example.com')]),
            raw=True)

        promised = peer_encoder.encode([(':method', 'GET'), (':path', '/pushed'),
                                        ('x-promise', 'p-value')])
        payload = (2).to_bytes(4, 'big') + promised
        skipped = PushPromise(length=len(payload),
                              type_=FrameTypes.PUSH_PROMISE.value, flags=0,
                              stream_id=1, data=payload,
                              decoder=connection.decoder,
                              encoder=connection.encoder)
        assert not skipped.end_headers, 'nothing decoded the promised block'

        later = peer_encoder.encode([(':method', 'GET'), ('x-promise', 'p-value')])
        assert connection.decoder.decode(later, raw=True) != [
            (b':method', b'GET'), (b'x-promise', b'p-value')], \
            'a decoder that missed the promised insertions cannot read what follows'
