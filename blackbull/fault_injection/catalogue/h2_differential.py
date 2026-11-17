"""Broken HTTP/2 peers for the two-axis differential.

``BROKEN_CLIENTS`` (the server axis) run unchanged against BlackBull's server
and reference servers through [`oracle_h2`][blackbull.fault_injection.oracle_h2],
which reads every frame itself, so they carry write steps only; every request
asks for ``/``.  ``BROKEN_SERVERS`` (the client axis) are served to BlackBull's
client and reference clients by ``H2FaultServer``; each answers one request
on stream 1.  Every case records the RFC 9113 section it exercises and, where
h2spec v2.6.0 has a check for the same rule, that check's RFC 7540-numbered
suite/section.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from hpack import Encoder, NeverIndexedHeaderTuple

from blackbull.protocol.frame_types import FrameTypes

from .. import scenario_h2 as h2s
from ..scenario_h2_client import (
    HalfClose, ScenarioH2Client, SendFrame, SendHeaders, SendPreface, SendRawBytes, Sleep,
)
from . import h2 as _h2_servers

__all__ = ['BROKEN_CLIENTS', 'BROKEN_SERVERS', 'Case']

_GET = ((':method', 'GET'), (':scheme', 'http'), (':authority', 'localhost'), (':path', '/'))
_SETTINGS = SendFrame(FrameTypes.SETTINGS)


def _block(fields) -> bytes:
    return Encoder().encode([NeverIndexedHeaderTuple(n, v) for n, v in fields])


def _scenario(name: str, *steps, settings=_SETTINGS) -> ScenarioH2Client:
    return ScenarioH2Client(name=name, steps=(SendPreface(), settings, *steps))


def _get(name: str, extra=(), pseudo=_GET, **kwargs) -> ScenarioH2Client:
    return _scenario(name, SendHeaders(pseudo=pseudo, headers=extra, end_stream=True), **kwargs)


def _raw_get(name: str, block: bytes, flags: int = 0x05) -> ScenarioH2Client:
    return _scenario(name, SendFrame(FrameTypes.HEADERS, flags=flags, stream_id=1, data=block))


def _priority(stream_id: int, depends_on: int) -> SendFrame:
    return SendFrame(FrameTypes.PRIORITY, stream_id=stream_id,
                     data=depends_on.to_bytes(4, 'big') + b'\x10')


def _with(field: tuple[str, str]) -> tuple:
    return (field,)


def _request_bytes() -> bytes:
    return (SendPreface().wire() + _SETTINGS.wire()
            + SendHeaders(pseudo=_GET, end_stream=True).wire())


# -- HPACK fields with incremental indexing: they enter the dynamic table -----

def _indexed_literal(name: str, value: str) -> bytes:
    """``Literal Header Field with Incremental Indexing — New Name`` (RFC 7541 §6.2.1)."""
    return b'\x40' + bytes([len(name)]) + name.encode() + bytes([len(value)]) + value.encode()


@dataclass(frozen=True)
class Case:
    name: str
    group: str            # 'rfc9113', 'transport' or 'stateful'
    rfc9113: str          # the section the case exercises
    h2spec: str | None    # h2spec v2.6.0's suite/section for the same rule, if any
    build: Callable[[], ScenarioH2Client | h2s.ScenarioH2]

    @property
    def id(self) -> str:
        return f'{self.group}/9113-{self.rfc9113}/h2spec-{self.h2spec or "none"}/{self.name}'


_CASES = (
    # -- RFC 9113 deltas h2spec does not check ---------------------------------
    Case('control_valid_get', 'rfc9113', '8.3.1', 'generic/4', lambda: _get('control_valid_get')),
    Case('host_authority_disagree', 'rfc9113', '8.3.1', None,
         lambda: _get('host_authority_disagree', _with(('host', 'other.example')))),
    Case('host_authority_agree', 'rfc9113', '8.3.1', None,
         lambda: _get('host_authority_agree', _with(('host', 'localhost')))),
    Case('uppercase_field_name', 'rfc9113', '8.2.1', 'http2/8.1.2', lambda: _get(
        'uppercase_field_name', _with(('X-Upper', 'v')))),
    Case('field_value_leading_space', 'rfc9113', '8.2.1', None, lambda: _get(
        'field_value_leading_space', _with(('x-v', ' v')))),
    Case('field_value_nul', 'rfc9113', '8.2.1', None,
         lambda: _get('field_value_nul', _with(('x-v', 'a\x00b')))),
    Case('field_name_space', 'rfc9113', '8.2.1', None,
         lambda: _get('field_name_space', _with(('x v', 'v')))),
    Case('connection_header', 'rfc9113', '8.2.2', 'http2/8.1.2.2',
         lambda: _get('connection_header', _with(('connection', 'keep-alive')))),
    Case('keep_alive_header', 'rfc9113', '8.2.2', 'http2/8.1.2.2',
         lambda: _get('keep_alive_header', _with(('keep-alive', 'timeout=5')))),
    Case('upgrade_header', 'rfc9113', '8.2.2', 'http2/8.1.2.2',
         lambda: _get('upgrade_header', _with(('upgrade', 'websocket')))),
    Case('transfer_encoding_header', 'rfc9113', '8.2.2', 'http2/8.1.2.2',
         lambda: _get('transfer_encoding_header', _with(('transfer-encoding', 'chunked')))),
    Case('te_trailers', 'rfc9113', '8.2.2', None,
         lambda: _get('te_trailers', _with(('te', 'trailers')))),
    Case('te_gzip', 'rfc9113', '8.2.2', 'http2/8.1.2.2', lambda: _get('te_gzip', _with(('te', 'gzip')))),
    Case('empty_path', 'rfc9113', '8.3.1', 'http2/8.1.2.3', lambda: _get(
        'empty_path', pseudo=_GET[:3] + ((':path', ''),))),
    Case('pseudo_after_regular', 'rfc9113', '8.3', 'http2/8.1.2.1', lambda: _get(
        'pseudo_after_regular', pseudo=_GET[:2] + (('x-a', 'b'),) + _GET[2:])),
    Case('dtsu_at_block_start', 'rfc9113', '4.3.1', 'generic/5',
         lambda: _raw_get('dtsu_at_block_start', b'\x3f\xe1\x1f' + _block(_GET))),
    Case('dtsu_after_field', 'rfc9113', '4.3.1', 'hpack/4.2',
         lambda: _raw_get('dtsu_after_field', _block(_GET) + b'\x20')),
    Case('dtsu_above_settings', 'rfc9113', '4.3.1', 'hpack/6.3',
         lambda: _raw_get('dtsu_above_settings', b'\x3f\xe1\x3f' + _block(_GET))),
    Case('priority_on_idle_stream', 'rfc9113', '5.3.2', 'generic/2', lambda: _scenario(
        'priority_on_idle_stream', _priority(3, 0), SendHeaders(pseudo=_GET, end_stream=True))),
    Case('priority_self_dependency', 'rfc9113', '5.3.1', 'http2/5.3.1', lambda: _scenario(
        'priority_self_dependency', _priority(1, 1), SendHeaders(pseudo=_GET, end_stream=True))),
    Case('headers_priority_flag', 'rfc9113', '5.3.2', 'generic/3.2', lambda: _raw_get(
        'headers_priority_flag', b'\x00\x00\x00\x00\x10' + _block(_GET), flags=0x25)),
    Case('unknown_setting', 'rfc9113', '6.5.2', 'http2/6.5.2', lambda: _get(
        'unknown_setting', settings=SendFrame(FrameTypes.SETTINGS, data=b'\x00\xff\x00\x00\x00\x01'))),

    # -- Transport faults: legal bytes, hostile delivery ------------------------
    Case('request_one_byte_per_write', 'transport', '4.1', None, lambda: ScenarioH2Client(
        name='request_one_byte_per_write',
        steps=(SendRawBytes(_request_bytes(), byte_interval=0.001),))),
    Case('frame_header_then_payload_later', 'transport', '4.1', None, lambda: _scenario(
        'frame_header_then_payload_later',
        SendRawBytes(SendHeaders(pseudo=_GET, end_stream=True).wire()[:9]), Sleep(0.3),
        SendRawBytes(SendHeaders(pseudo=_GET, end_stream=True).wire()[9:]))),
    Case('half_close_after_request', 'transport', '5.1', None, lambda: _scenario(
        'half_close_after_request', SendHeaders(pseudo=_GET, end_stream=True), HalfClose())),
    Case('truncated_frame_then_half_close', 'transport', '4.1', None, lambda: _scenario(
        'truncated_frame_then_half_close',
        SendHeaders(pseudo=_GET, end_stream=True, declared_length=64), HalfClose())),
    Case('request_after_reset_stream', 'transport', '6.4', None, lambda: _scenario(
        'request_after_reset_stream', SendHeaders(pseudo=_GET, end_stream=False),
        SendFrame(FrameTypes.RST_STREAM, stream_id=1, data=(8).to_bytes(4, 'big')),
        SendHeaders(pseudo=_GET, stream_id=3, end_stream=True))),

    # -- Stateful sequences ----------------------------------------------------
    Case('lower_stream_id_after_higher', 'stateful', '5.1.1', 'http2/5.1.1', lambda: _scenario(
        'lower_stream_id_after_higher', SendHeaders(pseudo=_GET, stream_id=3, end_stream=True),
        SendHeaders(pseudo=_GET, stream_id=1, end_stream=True))),
    Case('continuation_interleaved_with_ping', 'stateful', '6.10', 'http2/4.3', lambda: _scenario(
        'continuation_interleaved_with_ping',
        SendHeaders(pseudo=_GET, end_stream=True, end_headers=False),
        SendFrame(FrameTypes.PING, data=bytes(8)),
        SendFrame(FrameTypes.CONTINUATION, flags=0x4, stream_id=1))),
    Case('continuation_on_another_stream', 'stateful', '6.10', None, lambda: _scenario(
        'continuation_on_another_stream',
        SendHeaders(pseudo=_GET, end_stream=True, end_headers=False),
        SendFrame(FrameTypes.CONTINUATION, flags=0x4, stream_id=3))),
    Case('hpack_dynamic_entry_reused', 'stateful', '4.3', 'generic/5', lambda: _scenario(
        'hpack_dynamic_entry_reused',
        SendFrame(FrameTypes.HEADERS, flags=0x05, stream_id=1,
                  data=_block(_GET) + _indexed_literal('x-poison', 'one')),
        SendFrame(FrameTypes.HEADERS, flags=0x05, stream_id=3, data=_block(_GET) + b'\xbe'))),
    Case('hpack_index_past_table', 'stateful', '4.3', 'hpack/2.3.3', lambda: _raw_get(
        'hpack_index_past_table', _block(_GET) + b'\xbe')),
    Case('hpack_reference_after_eviction', 'stateful', '4.3', None, lambda: _scenario(
        'hpack_reference_after_eviction',
        SendFrame(FrameTypes.HEADERS, flags=0x05, stream_id=1,
                  data=_block(_GET) + _indexed_literal('x-poison', 'one')),
        SendFrame(FrameTypes.HEADERS, flags=0x05, stream_id=3,
                  data=b'\x20' + _block(_GET) + b'\xbe'))),
    Case('zero_window_then_update', 'stateful', '6.9.2', 'http2/6.9.1', lambda: _scenario(
        'zero_window_then_update', SendHeaders(pseudo=_GET, end_stream=True), Sleep(0.3),
        SendFrame(FrameTypes.WINDOW_UPDATE, stream_id=1, data=(1024).to_bytes(4, 'big')),
        settings=SendFrame(FrameTypes.SETTINGS, data=b'\x00\x04\x00\x00\x00\x00'))),
    Case('header_table_size_zero_before_ack', 'stateful', '6.5.3', None, lambda: _get(
        'header_table_size_zero_before_ack',
        settings=SendFrame(FrameTypes.SETTINGS, data=b'\x00\x01\x00\x00\x00\x00'))),
    Case('connection_window_overflow', 'stateful', '6.9.1', 'http2/6.9.1', lambda: _scenario(
        'connection_window_overflow',
        SendFrame(FrameTypes.WINDOW_UPDATE, data=(0x7fffffff).to_bytes(4, 'big')),
        SendHeaders(pseudo=_GET, end_stream=True))),
    Case('data_on_idle_stream', 'stateful', '5.1', 'http2/5.1', lambda: _scenario(
        'data_on_idle_stream', SendFrame(FrameTypes.DATA, flags=0x1, stream_id=1, data=b'x'))),
)

BROKEN_CLIENTS: dict[str, Case] = {case.name: case for case in _CASES}


# -- Broken servers: the answer to one GET on stream 1 ------------------------

def _head(*fields, status: tuple = ((':status', '200'),), suffix: bytes = b'') -> bytes:
    block = _block(status + fields) + suffix
    return h2s.frame_bytes(len(block), FrameTypes.HEADERS.value, 0x4, 1, block)


def _data(body: bytes, end_stream: bool = True) -> bytes:
    return h2s.frame_bytes(len(body), FrameTypes.DATA.value, 0x1 if end_stream else 0, 1, body)


def _response(*fields, body: bytes = b'ok', status: tuple = ((':status', '200'),)) -> bytes:
    return _head(*fields, status=status) + _data(body)


def _answering(name: str, *answer: bytes, initial_settings=()) -> h2s.ScenarioH2:
    return h2s.ScenarioH2(name=name, initial_settings=initial_settings, steps=(
        h2s.WaitForClientFrame(match={'type': 'HEADERS', 'stream_id': 1}, timeout=5.0),
        h2s.SendRawBytes(b''.join(answer)), h2s.Sleep(1.0),
        h2s.CloseGracefully(error_code=0, last_stream_id=1)))


def _rst(code: int) -> bytes:
    return h2s.frame_bytes(4, FrameTypes.RST_STREAM.value, 0, 1, code.to_bytes(4, 'big'))


_SERVER_CASES = (
    Case('control_valid_response', 'response', '8.3.2', None,
         lambda: _answering('control_valid_response', _response())),
    Case('response_uppercase_field', 'response', '8.2.1', None, lambda: _answering(
        'response_uppercase_field', _response(('X-Upper', 'v')))),
    Case('response_connection_header', 'response', '8.2.2', None, lambda: _answering(
        'response_connection_header', _response(('connection', 'close')))),
    Case('response_without_status', 'response', '8.3.2', None, lambda: _answering(
        'response_without_status', _response(status=()))),
    Case('response_status_two_digits', 'response', '8.3.2', None, lambda: _answering(
        'response_status_two_digits', _response(status=((':status', '20'),)))),
    Case('response_pseudo_after_regular', 'response', '8.3', None, lambda: _answering(
        'response_pseudo_after_regular',
        _response(status=(('x-a', 'b'), (':status', '200'))))),
    Case('response_body_beyond_content_length', 'response', '8.1.1', None, lambda: _answering(
        'response_body_beyond_content_length', _response(('content-length', '1')))),
    Case('response_dtsu_after_field', 'response', '4.3.1', None, lambda: _answering(
        'response_dtsu_after_field', _head(suffix=b'\x20'), _data(b'ok'))),
    Case('goaway_before_response', 'response', '6.8', None, lambda: _answering(
        'goaway_before_response', h2s.CloseGracefully(error_code=0, last_stream_id=0).wire())),
    Case('reset_mid_body', 'response', '6.4', None, lambda: _answering(
        'reset_mid_body', _head(), _data(b'o', end_stream=False), _rst(2))),
    Case('data_frame_over_max_frame_size', 'response', '4.2', None, lambda: _answering(
        'data_frame_over_max_frame_size', _response(body=b'x' * 16_385))),
    Case('half_closed_after_headers', 'response', '5.1', None,
         _h2_servers.half_closed_after_headers),
    Case('half_closed_stream_no_data', 'response', '5.1', None,
         _h2_servers.half_closed_stream_no_data),
    Case('settings_max_frame_size_below_minimum', 'response', '6.5.2', None,
         _h2_servers.settings_max_frame_size_below_minimum),
    Case('headers_continuation_dropped', 'response', '6.10', None,
         _h2_servers.headers_continuation_dropped),
)

BROKEN_SERVERS: dict[str, Case] = {case.name: case for case in _SERVER_CASES}
