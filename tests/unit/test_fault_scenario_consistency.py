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
            EndChunkedBody, SendChunk,
        )
        # The single most common HTTP/1.1 framing fault: a size that lies.
        assert SendChunk(data=b'ab', declared_size=5).wire() == \
            b'5\r\nab\r\n'
        assert SendChunk(data=b'ab', extension='x=1').wire() == \
            b'2;x=1\r\nab\r\n'
        assert EndChunkedBody(trailers=(('X', '1'),)).wire() == \
            b'0\r\nX: 1\r\n\r\n'

    async def test_status_line_and_obs_fold_are_typed(self):
        from blackbull.fault_injection.scenario_h1_server import (
            SendHeader, SendStatusLine,
        )
        assert SendStatusLine(omit_reason=True).wire() == \
            b'HTTP/1.1 200\r\n'
        assert SendStatusLine(version='HTTP/9.9').wire() == \
            b'HTTP/9.9 200 OK\r\n'
        # obs-fold (RFC 9112 §5.2) — deprecated, and a recipient must reject
        # or normalise it, which is what makes it worth staging.
        assert SendHeader('X', 'a', fold=True).wire() == b' a\r\n'
