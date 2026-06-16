"""Unit tests for the HTTP/2 half of :mod:`blackbull.fault_injection`.

Covers:

* ``scenario_h2`` data model — round-trip, ``frame_matches`` predicate.
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
    H2FaultServerError,
    ScenarioH2,
    SendRawBytes,
    WaitForClientFrame,
    frame_matches,
    make_self_signed_h2_context,
    scenario_h2_from_json,
    scenario_h2_to_json,
    serialize_frame,
)
from blackbull.fault_injection import H2Abort as Abort
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
# Scenario data model
# ----------------------------------------------------------------------


def test_scenario_h2_round_trips_through_json():
    original = ScenarioH2(
        steps=(
            WaitForClientFrame(match={'type': 'HEADERS', 'stream_id': 1},
                               timeout=2.0),
            SendRawBytes(b'\x00\x00\x00\x09\x00\x00\x00\x00\x01'),
            Sleep(0.25),
            CloseGracefully(error_code=1, last_stream_id=1),
        ),
        send_preface=True,
        initial_settings=((0x4, 0),),
    )
    js = scenario_h2_to_json(original)
    back = scenario_h2_from_json(js)
    assert back == original


def test_scenario_h2_serialises_settings_frame():
    f = ft.SettingFrame(length=0, type_=ft.FrameTypes.SETTINGS,
                        flags=0, stream_id=0)
    f.settings = [(0x4, 0)]
    data = serialize_frame(f)
    # 9-byte header (length=6, type=4, flags=0, stream_id=0) + payload (6 bytes).
    assert data[:3] == b'\x00\x00\x06'
    assert data[3:4] == ft.FrameTypes.SETTINGS.value
    assert data[9:] == b'\x00\x04\x00\x00\x00\x00'


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


def test_catalogue_round_trips_through_json():
    """Every catalogue scenario must JSON-round-trip so they can be
    serialised into a portable bug-report alongside a captured failure."""
    for name, build in CATALOGUE.items():
        original = build()
        back = scenario_h2_from_json(scenario_h2_to_json(original))
        assert back == original, f'{name} did not round-trip'


# ----------------------------------------------------------------------
# H2FaultServer — safety locks
# ----------------------------------------------------------------------


def test_h2_fault_server_refuses_with_bb_production(monkeypatch):
    monkeypatch.setenv('BB_PRODUCTION', '1')
    with pytest.raises(H2FaultServerError, match='BB_PRODUCTION'):
        H2FaultServer(scenario=ScenarioH2(steps=()))


def test_h2_fault_server_refuses_non_localhost_bind():
    with pytest.raises(H2FaultServerError, match='allow_remote'):
        H2FaultServer(scenario=ScenarioH2(steps=()), host='0.0.0.0')


def test_h2_fault_server_accepts_allow_remote_override():
    # Construct only — does not start.  This proves the safety lock
    # is opt-out, not hard-coded.
    srv = H2FaultServer(
        scenario=ScenarioH2(steps=()), host='0.0.0.0', allow_remote=True)
    assert srv is not None


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
        client_ctx = _ssl.create_default_context()
        client_ctx.check_hostname = False
        client_ctx.verify_mode = _ssl.CERT_NONE
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
async def test_h2_fault_server_records_wait_timeout():
    """WaitForClientFrame whose match never arrives records timed-out
    on the result, then advances past the step."""
    scenario = ScenarioH2(
        steps=(
            WaitForClientFrame(
                match={'type': 'HEADERS', 'stream_id': 99}, timeout=0.2),
            CloseGracefully(),
        ),
    )
    async with H2FaultServer(scenario=scenario) as srv:
        reader, writer = await _synthetic_client_dial(srv.url)
        try:
            await asyncio.wait_for(reader.read(4096), timeout=2.0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        await srv.wait_for_connection_done(timeout=2.0)

    assert srv.last_result is not None
    assert srv.last_result.wait_timed_out
    assert srv.last_result.steps_completed == 2  # wait + close both counted
    assert srv.last_result.terminated


@pytest.mark.asyncio
async def test_h2_fault_server_advances_on_matching_client_frame():
    """When the client sends a matching HEADERS frame, the
    WaitForClientFrame step completes without timing out."""
    scenario = ScenarioH2(
        steps=(
            WaitForClientFrame(
                match={'type': 'HEADERS', 'stream_id': 1}, timeout=2.0),
            CloseGracefully(),
        ),
    )
    async with H2FaultServer(scenario=scenario) as srv:
        host, port = srv.url.split('//')[1].rstrip('/').split(':')
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(CLIENT_PREFACE)
            writer.write(_CLIENT_SETTINGS)
            writer.write(_CLIENT_HEADERS_S1)
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=2.0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        await srv.wait_for_connection_done(timeout=2.0)

    assert srv.last_result is not None
    assert not srv.last_result.wait_timed_out
    assert srv.last_result.terminated


@pytest.mark.asyncio
async def test_h2_fault_server_abort_step_closes_transport():
    """Abort terminator hard-closes the connection; the result is
    marked terminated and steps_completed reflects only the abort."""
    scenario = ScenarioH2(steps=(Abort(),))
    async with H2FaultServer(scenario=scenario) as srv:
        reader, writer = await _synthetic_client_dial(srv.url)
        try:
            # Reading after an abort should return empty fairly quickly
            # (transport got an RST or EOF).
            await asyncio.wait_for(reader.read(4096), timeout=2.0)
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
