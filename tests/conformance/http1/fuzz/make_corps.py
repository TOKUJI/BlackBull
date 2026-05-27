#!/usr/bin/env python3
"""
L5 corpus generator for HTTP/1.1 fuzzing (BlackBull / Nginx comparison)
- raw byte-level mutation
- CRLF / LF / CR corruption
"""

import os

OUT_DIR = "corpus"

os.makedirs(OUT_DIR, exist_ok=True)


def write_case(name: str, data: bytes):
    path = os.path.join(OUT_DIR, name)
    with open(path, "wb") as f:
        f.write(data)


# -------------------------
# Base valid request
# -------------------------
BASE = b"GET /echo HTTP/1.1\r\nHost: example.com\r\n\r\nhello"


# -------------------------
# L5 cases
# -------------------------

cases = []

# 1. LF injection inside header block
cases.append((
    "l5_lf_in_header.txt",
    b"GET /echo HTTP/1.1\r\nHost: exa\nmple.com\r\n\r\nbody"
))

# 2. CR injection (broken line ending)
cases.append((
    "l5_cr_in_header.txt",
    b"GET /echo HTTP/1.1\rHost: example.com\r\n\r\nbody"
))

# 3. mixed CRLF + LF (parser ambiguity)
cases.append((
    "l5_mixed_crlf_lf.txt",
    b"GET /echo HTTP/1.1\r\nHost: example.com\nX: 1\r\n\r\nbody"
))

# 4. header split corruption (no proper CRLF)
cases.append((
    "l5_no_crlf_split.txt",
    b"GET /echo HTTP/1.1 Host: example.com\r\n\r\nbody"
))

# 5. empty host
cases.append((
    "l5_empty_host.txt",
    b"GET /echo HTTP/1.1\r\nHost:\r\n\r\nbody"
))

# 6. duplicate host header
cases.append((
    "l5_duplicate_host.txt",
    b"GET /echo HTTP/1.1\r\nHost: a.com\r\nHost: b.com\r\n\r\nbody"
))

# 7. newline injected inside header value
cases.append((
    "l5_header_injection.txt",
    b"GET /echo HTTP/1.1\r\nHost: example.com\r\nX: a\r\nb\r\n\r\nbody"
))

# 8. body before header termination (no blank line)
cases.append((
    "l5_body_no_separator.txt",
    b"GET /echo HTTP/1.1\r\nHost: example.com\r\nbody-is-here"
))

# 9. extreme CRLF explosion (parser stress)
cases.append((
    "l5_crlf_storm.txt",
    b"GET /echo HTTP/1.1" + b"\r\n" * 1000 + b"Host: example.com\r\n\r\nbody"
))

# 10. NULL byte injection
cases.append((
    "l5_null_byte.txt",
    b"GET /echo HTTP/1.1\r\nHost: ex\x00ample.com\r\n\r\nbody"
))


# -------------------------
# write all cases
# -------------------------
for name, data in cases:
    write_case(name, data)

print(f"[+] L5 corpus generated: {len(cases)} files in {OUT_DIR}")
