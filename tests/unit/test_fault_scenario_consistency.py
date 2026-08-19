"""The consistency sweep, as a test rather than a one-off report.

Sprints 107 and 108 ship as one release so this could be asked once, about
a finished grid: is the notation unified across HTTP/1.1 and HTTP/2, and
can each protocol's own faults be expressed?

Running the sweep found four gratuitous divergences, one capability gap and
one latent bug — none of which anything inside the code was going to
surface.  The sweep is therefore kept as a test, so the next vocabulary
added has to answer to it.
"""
from __future__ import annotations

import dataclasses as dc

import pytest

from blackbull.fault_injection import (
    scenario_h1 as h1_client,
    scenario_h1_server as h1_server,
    scenario_h2 as h2_server,
    scenario_h2_client as h2_client,
)

pytestmark = pytest.mark.asyncio

_VOCABULARIES = {
    'h1-client': h1_client,
    'h1-server': h1_server,
    'h2-client': h2_client,
    'h2-server': h2_server,
}


def _steps(module) -> dict[str, tuple[str, ...]]:
    out = {}
    for name in dir(module):
        obj = getattr(module, name)
        if dc.is_dataclass(obj) and not name.startswith(('Scenario', '_')):
            out[name] = tuple(f.name for f in dc.fields(obj))
    return out


class TestQ1NotationIsUnified:
    async def test_the_shared_steps_are_identical_everywhere(self):
        """`Abort` and `Sleep` mean one thing in all four."""
        for step in ('Abort', 'Sleep'):
            shapes = {k: _steps(m).get(step) for k, m in _VOCABULARIES.items()}
            assert len(set(shapes.values())) == 1, f'{step} differs: {shapes}'

    async def test_the_raw_escape_hatch_has_one_name(self):
        """It was `SendBytes` on the clients and `SendRawBytes` on the
        servers — the same two fields, split by role rather than by
        anything a reader could predict."""
        for key, module in _VOCABULARIES.items():
            steps = _steps(module)
            assert 'SendRawBytes' in steps, f'{key} lacks SendRawBytes'
            assert steps['SendRawBytes'] == ('data', 'byte_interval'), key

    async def test_the_old_spelling_still_works_and_warns(self):
        for module in (h1_client, h2_client):
            with pytest.warns(DeprecationWarning, match='SendRawBytes'):
                assert module.SendBytes is module.SendRawBytes

    async def test_every_scenario_carries_a_name(self):
        for key, module in _VOCABULARIES.items():
            cls = next(getattr(module, n) for n in dir(module)
                       if n.startswith('Scenario') and not n.endswith('Result'))
            assert 'name' in {f.name for f in dc.fields(cls)}, key

    async def test_both_h2_halves_can_lie_about_a_frame_length(self):
        """The client half could; the server half could not."""
        for module in (h2_client, h2_server):
            fields = {f.name for f in dc.fields(module.SendFrame)}
            assert 'declared_length' in fields


class TestQ2Http2FaultsAreExpressible:
    async def test_header_faults_no_longer_need_hand_built_hex(self):
        """Three sweep rows moved from raw-bytes-only to typed."""
        from blackbull.fault_injection.scenario_h2_client import (
            SendHeaders, encode_headers,
        )

        # A pseudo-header after a regular one — RFC 9113 §8.3 forbids it.
        out_of_order = SendHeaders(headers=(('x-a', '1'),),
                                   pseudo=((':path', '/'),))
        assert len(encode_headers(out_of_order)) > 9

        # A block HPACK itself would never produce.
        broken = SendHeaders(raw_block=b'\x82\xff\xff')
        assert encode_headers(broken).endswith(b'\x82\xff\xff')

        # And a header block whose length lies.
        lying = SendHeaders(declared_length=999)
        assert encode_headers(lying)[:3] == (999).to_bytes(3, 'big')


class TestQ3Http1FaultsAreExpressible:
    async def test_chunked_framing_faults_are_typed(self):
        from blackbull.fault_injection.scenario_h1_server import (
            EndChunkedBody, SendChunk, encode_chunk, encode_chunked_terminator,
        )
        # The single most common HTTP/1.1 framing fault: a size that lies.
        assert encode_chunk(SendChunk(data=b'ab', declared_size=5)) == \
            b'5\r\nab\r\n'
        assert encode_chunk(SendChunk(data=b'ab', extension='x=1')) == \
            b'2;x=1\r\nab\r\n'
        assert encode_chunked_terminator(
            EndChunkedBody(trailers=(('X', '1'),))) == b'0\r\nX: 1\r\n\r\n'

    async def test_status_line_and_obs_fold_are_typed(self):
        from blackbull.fault_injection.scenario_h1_server import (
            SendHeader, SendStatusLine, encode_header, encode_status_line,
        )
        assert encode_status_line(SendStatusLine(omit_reason=True)) == \
            b'HTTP/1.1 200\r\n'
        assert encode_status_line(SendStatusLine(version='HTTP/9.9')) == \
            b'HTTP/9.9 200 OK\r\n'
        # obs-fold (RFC 9112 §5.2) — deprecated, and a recipient must reject
        # or normalise it, which is what makes it worth staging.
        assert encode_header(SendHeader('X', 'a', fold=True)) == b' a\r\n'


class TestTheLatentBugTheSweepFound:
    async def test_a_ping_scenario_round_trips(self):
        """`Ping` is the one frame class whose constructor requires `data`,
        so a scenario containing one serialised and would not read back.
        Present since the serialiser was written."""
        from blackbull.fault_injection import (
            ScenarioH2, SendFrame, scenario_h2_from_json, scenario_h2_to_json,
        )
        from blackbull.protocol.frame import FrameFactory
        from blackbull.protocol.frame_types import FrameTypes

        frame = FrameFactory().create(FrameTypes.PING, 0, 0, data=b'\x01' * 8)
        scenario = ScenarioH2(steps=(SendFrame(frame=frame),))
        restored = scenario_h2_from_json(scenario_h2_to_json(scenario))
        assert restored.steps[0].frame.payload == b'\x01' * 8
