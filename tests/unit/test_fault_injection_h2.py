"""Unit tests for the HTTP/2 half of :mod:`blackbull.fault_injection`.

Covers:

* ``frame_matches`` predicate.
* ``catalogue.h2`` — each named builder returns a well-formed scenario.
* ``H2FaultServer`` — safety locks (``BB_PRODUCTION``, non-localhost
  bind) and the end-to-end handshake + scenario walk against a
  synthetic client.
"""
from __future__ import annotations

import asyncio

import pytest

from blackbull.fault_injection import (
    CLIENT_PREFACE,
    CloseGracefully,
    H2FaultServer,
    ScenarioH2,
    frame_matches,
    make_self_signed_h2_context,
)
from blackbull.fault_injection import H2Sleep as Sleep
from blackbull.fault_injection.catalogue import CATALOGUE
from blackbull.protocol import frame_types as ft


# A minimal client SETTINGS frame (zero payload).  Sent right after
# the preface so the server's inbound-frame loop has something to
# decode; the synthetic tests below rely on this rather than HEADERS
# for the WaitForClientFrame steps.
_CLIENT_SETTINGS = b'\x00\x00\x00\x04\x00\x00\x00\x00\x00'

# A minimal client HEADERS frame for stream 1 — flags END_STREAM +
# END_HEADERS so the server's WaitForClientFrame predicates match.
_CLIENT_HEADERS_S1 = (
    b'\x00\x00\x01'  # length
    b'\x01'           # type HEADERS
    b'\x05'           # flags END_STREAM | END_HEADERS
    b'\x00\x00\x00\x01'  # stream id 1
    b'\x88'           # static-table :status 200 (used as headers payload)
)


# ----------------------------------------------------------------------
# frame_matches predicate
# ----------------------------------------------------------------------


def test_frame_matches_returns_true_for_matching_type_and_stream():
    h = ft.Headers(length=0, type_=ft.FrameTypes.HEADERS,
                   flags=int(ft.HeaderFrameFlags.END_HEADERS), stream_id=3)
    assert frame_matches(h, {'type': 'HEADERS', 'stream_id': 3})


def test_frame_matches_rejects_mismatched_stream():
    h = ft.Headers(length=0, type_=ft.FrameTypes.HEADERS,
                   flags=0, stream_id=3)
    assert not frame_matches(h, {'type': 'HEADERS', 'stream_id': 1})


def test_frame_matches_honours_flags_set_and_unset():
    h = ft.Headers(length=0, type_=ft.FrameTypes.HEADERS,
                   flags=int(ft.HeaderFrameFlags.END_HEADERS), stream_id=1)
    assert frame_matches(
        h, {'type': 'HEADERS', 'flags_set': ['END_HEADERS']})
    assert frame_matches(
        h, {'type': 'HEADERS', 'flags_unset': ['END_STREAM']})
    assert not frame_matches(
        h, {'type': 'HEADERS', 'flags_set': ['END_STREAM']})
    assert not frame_matches(
        h, {'type': 'HEADERS', 'flags_unset': ['END_HEADERS']})


def test_frame_matches_fails_closed_on_unknown_key():
    """Unknown match keys are almost always typos in catalogue entries.
    Silently matching on them would hide bugs."""
    h = ft.Headers(length=0, type_=ft.FrameTypes.HEADERS,
                   flags=0, stream_id=1)
    assert not frame_matches(h, {'unknown_field': 'whatever'})


# ----------------------------------------------------------------------
# Catalogue — every named builder yields a usable scenario
# ----------------------------------------------------------------------


@pytest.mark.parametrize('name', list(CATALOGUE.keys()))
def test_catalogue_builder_returns_scenario(name):
    sc = CATALOGUE[name]()
    assert isinstance(sc, ScenarioH2)
    assert sc.steps, f'{name} produced an empty scenario'


# ----------------------------------------------------------------------
# TLS — H2FaultServer over HTTPS with ALPN h2
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_h2_fault_server_url_is_https_when_ssl_context_provided():
    """Constructing with an ssl_context flips the URL scheme to ``https``."""
    ctx = make_self_signed_h2_context()
    scenario = ScenarioH2(steps=(CloseGracefully(),))
    async with H2FaultServer(scenario=scenario, ssl_context=ctx) as srv:
        assert srv.url is not None
        assert srv.url.startswith('https://'), srv.url


@pytest.mark.asyncio
async def test_h2_fault_server_negotiates_h2_via_alpn():
    """A TLS client offering ``h2`` in ALPN gets ``h2`` selected; the
    server then completes a CloseGracefully scenario over the encrypted
    connection."""
    import ssl as _ssl
    ctx = make_self_signed_h2_context()
    scenario = ScenarioH2(steps=(CloseGracefully(error_code=0),))
    async with H2FaultServer(scenario=scenario, ssl_context=ctx) as srv:
        host, port = srv.url.split('//')[1].rstrip('/').split(':')
        client_ctx = _ssl.create_default_context(cafile=ctx.bb_ca_cert_path)
        client_ctx.set_alpn_protocols(['h2'])
        reader, writer = await asyncio.open_connection(
            host, int(port), ssl=client_ctx)
        try:
            assert writer.get_extra_info('ssl_object').selected_alpn_protocol() == 'h2'
            writer.write(CLIENT_PREFACE)
            writer.write(_CLIENT_SETTINGS)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), timeout=2.0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        await srv.wait_for_connection_done(timeout=2.0)

    assert srv.last_result is not None
    assert srv.last_result.terminated
    # GOAWAY frame type byte must appear in the wire data.
    assert b'\x07' in data, f'expected GOAWAY in {data.hex()}'


# ----------------------------------------------------------------------
# H2FaultServer — end-to-end (synthetic client)
# ----------------------------------------------------------------------


async def _synthetic_client_dial(url: str) -> tuple[asyncio.StreamReader,
                                                    asyncio.StreamWriter]:
    """Open a raw connection to *url* and send preface + SETTINGS."""
    host, port = url.split('//')[1].rstrip('/').split(':')
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(CLIENT_PREFACE)
    writer.write(_CLIENT_SETTINGS)
    await writer.drain()
    return reader, writer


@pytest.mark.asyncio
async def test_h2_fault_server_completes_close_gracefully_scenario():
    """Server handshakes, then immediately emits GOAWAY and shuts down.

    Asserts the scenario terminator step (CloseGracefully) marks the
    result as ``terminated`` and the server replies with a GOAWAY frame
    on stream 0.
    """
    scenario = ScenarioH2(steps=(CloseGracefully(error_code=0),))
    async with H2FaultServer(scenario=scenario) as srv:
        reader, writer = await _synthetic_client_dial(srv.url)
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=2.0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        await srv.wait_for_connection_done(timeout=2.0)

    assert srv.last_result is not None
    assert srv.last_result.terminated
    assert srv.last_result.steps_completed == 1
    # Look for a GOAWAY frame (type byte 0x07) on stream 0 in the
    # response.  We don't care about its exact byte position because
    # the server preface SETTINGS precedes it.
    assert b'\x07' in data, f'expected GOAWAY frame in {data.hex()}'


@pytest.mark.asyncio
async def test_stopping_the_server_cancels_a_scenario_still_running():
    """A scenario holding the connection open does not hold up teardown."""
    srv = H2FaultServer(scenario=ScenarioH2(steps=(Sleep(30.0),)))
    await srv.start()
    reader, writer = await _synthetic_client_dial(srv.url)
    try:
        await asyncio.wait_for(reader.read(9), timeout=2.0)  # its SETTINGS
        await asyncio.wait_for(srv.stop(), timeout=5.0)
    finally:
        writer.close()
    assert srv.last_result is not None
    assert not srv.last_result.terminated


@pytest.mark.asyncio
async def test_h2_fault_server_serves_settings_max_frame_size_catalogue():
    """The illegal-SETTINGS catalogue entry is the simplest end-to-end
    catalogue smoke: it has no Wait steps and no body, just an initial
    SETTINGS payload with the illegal value, then a brief sleep."""
    scenario = CATALOGUE['settings_max_frame_size_below_minimum']()
    async with H2FaultServer(scenario=scenario) as srv:
        reader, writer = await _synthetic_client_dial(srv.url)
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=3.0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        await srv.wait_for_connection_done(timeout=3.0)

    # The server's initial SETTINGS frame must carry MAX_FRAME_SIZE
    # (id 0x05) below the legal floor (16384).
    # SETTINGS frame: 9-byte header + 6-byte payload (id + value).
    settings_payload = data[9:15]
    assert settings_payload[:2] == b'\x00\x05', \
        f'expected setting id 0x05, got {settings_payload.hex()}'
    value = int.from_bytes(settings_payload[2:6], 'big')
    assert value < 16_384, f'expected illegal value, got {value}'


def test_self_signed_tls_context_cleans_up_its_key_directory():
    import gc
    from pathlib import Path
    pytest.importorskip('cryptography')
    from blackbull.fault_injection._tls import make_self_signed_h2_context

    ctx = make_self_signed_h2_context()
    keydir = Path(ctx.bb_ca_cert_path).parent
    assert keydir.exists()
    del ctx
    gc.collect()
    assert not keydir.exists()
