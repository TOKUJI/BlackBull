#!/usr/bin/env python3
"""Generate Atheris seed corpus for HTTP/2 fuzz harness — expanded with
known attacks, CVE patterns, and Sprint 57 test failures.

Categories:
  A. Valid baseline (GET, POST, multi-stream)
  B. Sprint 57 flow-control deadlock patterns (bug #2 boundary)
  C. RFC 9113 gap test failures (G13 — CR/LF/NUL in field values)
  D. Known CVE patterns (Rapid Reset, CONTINUATION flood, SETTINGS flood)
  E. Protocol edge cases (WU=0, RST on 0, GOAWAY on 1, DATA after RST)
  F. Pure garbage (random bytes, all zeros, max values)
"""
from __future__ import annotations
from pathlib import Path

OUT_DIR = Path(__file__).parent / 'corpus'
OUT_DIR.mkdir(parents=True, exist_ok=True)

def _h2_frame(type_byte: int, flags: int, stream_id: int, payload: bytes) -> bytes:
    length = len(payload)
    return (length.to_bytes(3, 'big') + bytes([type_byte])
            + bytes([flags]) + stream_id.to_bytes(4, 'big') + payload)

def _write(name: str, data: bytes) -> None:
    (OUT_DIR / name).write_bytes(data)
    print(f'  {name} ({len(data)} bytes)')

# HPACK pre-encoded common header blocks
_M_GET   = bytes.fromhex('82')
_M_POST  = bytes.fromhex('83')
_P_ROOT  = bytes.fromhex('84')
_S_HTTPS = bytes.fromhex('87')
_A_LOCAL = bytes.fromhex('418b9788e0b0db17a7638fb1cf4f2f4f3495db178f2f4f')
_P_ECHO  = bytes.fromhex('0488aec4e0')

_GET_HD  = _M_GET + _P_ROOT + _S_HTTPS + _A_LOCAL
_POST_HD = _M_POST + _P_ECHO + _S_HTTPS + _A_LOCAL

# Literal headers with prohibited field value chars (G13 attack vectors)
# Each: 0x40 = literal incremental, then name len + name + value len + value
_FIELD_CR  = bytes.fromhex('40') + b'\x08x-cr-tst'  + b'\x07val\rxxx'
_FIELD_LF  = bytes.fromhex('40') + b'\x08x-lf-tst'  + b'\x07val\nxxx'
_FIELD_NUL = bytes.fromhex('40') + b'\x09x-nul-tst' + b'\x08val\x00xxx'

# Uppercase field name (Content-Type = 'C' 0x43 in 0x41-0x5a range)
_FIELD_UC  = bytes.fromhex('40') + b'\x0cContent-Type\x09text/html'

print('Generating H2 fuzz seeds (expanded):')

# ── A. Valid baseline ──
_write('a01_get.bin',             _h2_frame(0x01,0x05,1,_GET_HD))
_write('a02_post.bin',            _h2_frame(0x01,0x04,3,_POST_HD)+_h2_frame(0x00,0x01,3,b'hello'))
_write('a03_two_gets.bin',        _h2_frame(0x01,0x05,1,_GET_HD)+_h2_frame(0x01,0x05,3,_GET_HD))
_write('a04_five_gets.bin',       b''.join(_h2_frame(0x01,0x05,s,_GET_HD) for s in [1,3,5,7,9]))
_write('a05_large_body.bin',      _h2_frame(0x01,0x04,1,_POST_HD)+_h2_frame(0x00,0x01,1,b'\x42'*16384))
_write('a06_empty_body.bin',      _h2_frame(0x01,0x04,1,_POST_HD)+_h2_frame(0x00,0x01,1,b''))

# ── B. Sprint 57 flow-control boundary ──
for sz,label in [(65530,'65530'),(65531,'65531'),(65535,'65535'),(131070,'131070')]:
    _write(f'b{["30","31","35","70"][[65530,65531,65535,131070].index(sz)]}_window_{label}.bin',
           _h2_frame(0x01,0x04,1,_POST_HD)+_h2_frame(0x00,0x01,1,b'\x00'*sz))

# ── C. RFC 9113 G13 field validation attacks ──
_write('c01_field_cr_in_value.bin', _h2_frame(0x01,0x05,1,_GET_HD+_FIELD_CR))
_write('c02_field_lf_in_value.bin', _h2_frame(0x01,0x05,1,_GET_HD+_FIELD_LF))
_write('c03_field_nul_in_value.bin',_h2_frame(0x01,0x05,1,_GET_HD+_FIELD_NUL))
_write('c04_uppercase_field_name.bin',_h2_frame(0x01,0x05,1,_GET_HD+_FIELD_UC))

# ── D. Known CVE patterns ──
# CVE-2023-44487 Rapid Reset: 30 RST_STREAM on odd stream IDs
_write('d01_rapid_reset_30.bin',
    b''.join(_h2_frame(0x03,0x00,sid,(0x08).to_bytes(4,'big'))
             for sid in range(1,60,2)))
# CONTINUATION flood without END_HEADERS
_write('d02_continuation_flood.bin',
    _h2_frame(0x01,0x00,1,_GET_HD[:10])
    + b''.join(_h2_frame(0x09,0x00,1,b'\x00'*100) for _ in range(15)))
# HEADERS with END_STREAM + CONTINUATION after (illegal per spec)
_write('d03_headers_es_then_cont.bin',
    _h2_frame(0x01,0x01,1,_GET_HD[:10])
    + _h2_frame(0x09,0x04,1,_GET_HD[10:]))
# SETTINGS flood
_write('d04_settings_flood.bin',
    b''.join(_h2_frame(0x04,0x00,0,(0x0003).to_bytes(2,'big')+(100).to_bytes(4,'big'))
             for _ in range(25)))
# PING flood
_write('d05_ping_flood.bin',
    b''.join(_h2_frame(0x06,0x00,0,b'\x00'*8) for _ in range(15)))
# HEADERS on server-initiated stream (even stream ID)
_write('d06_headers_on_server_stream.bin', _h2_frame(0x01,0x05,2,_GET_HD))

# ── E. Protocol edge cases ──
_write('e01_wu_zero_stream.bin',   _h2_frame(0x01,0x05,1,_GET_HD)+_h2_frame(0x08,0x00,1,(0).to_bytes(4,'big')))
_write('e02_wu_zero_conn.bin',     _h2_frame(0x08,0x00,0,(0).to_bytes(4,'big')))
_write('e03_rst_stream0.bin',      _h2_frame(0x03,0x00,0,(0x08).to_bytes(4,'big')))
_write('e04_goaway_stream1.bin',   _h2_frame(0x07,0x00,1,(0).to_bytes(4,'big')+(0).to_bytes(4,'big')))
_write('e05_data_stream0.bin',     _h2_frame(0x00,0x01,0,b'bad'))
_write('e06_headers_stream0.bin',  _h2_frame(0x01,0x05,0,_GET_HD))
_write('e07_settings_stream99.bin',_h2_frame(0x04,0x00,99,(0x0003).to_bytes(2,'big')+(100).to_bytes(4,'big')))
_write('e08_data_after_rst.bin',
    _h2_frame(0x01,0x04,1,_POST_HD)
    + _h2_frame(0x03,0x00,1,(0x08).to_bytes(4,'big'))
    + _h2_frame(0x00,0x01,1,b'more'))
_write('e09_padded_overlong.bin',
    _h2_frame(0x01,0x04,1,_POST_HD)
    + _h2_frame(0x00,0x08,1,b'\xff'+b'x'*3))  # pad=255, only 3 data bytes
_write('e10_data_unopened.bin',
    _h2_frame(0x01,0x05,1,_GET_HD)
    + _h2_frame(0x00,0x01,3,b'data_on_closed'))
_write('e11_wu_max_increment.bin',_h2_frame(0x08,0x00,1,(0x7FFFFFFF).to_bytes(4,'big')))
_write('e12_wu_overflow.bin',      _h2_frame(0x08,0x00,1,(0x80000000).to_bytes(4,'big')))

# ── F. Pure garbage ──
_write('f01_zeros_256.bin',    b'\x00'*256)
_write('f02_ff_256.bin',       b'\xff'*256)
_write('f03_range_0_255.bin',  bytes(range(256)))
_write('f04_empty.bin',        b'')
_write('f05_partial_header.bin',b'\x00\x00\x10')
_write('f06_ascii_noise.bin',  b'GET / HTTP/1.1\r\n\r\n' * 10)

total = len(list(OUT_DIR.iterdir()))
print(f'\nDone — {total} seeds in {OUT_DIR}')
