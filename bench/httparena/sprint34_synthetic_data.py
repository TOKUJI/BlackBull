"""Regenerate Sprint 33's synthetic static dataset.

Mirrors the inline Python in
``bench/aws/sprint33_phase_trace_httparena_shape.sh`` (the script
that produced the 36,961 r/s number).  The text content is a
``1024``-byte ascii pattern repeated ``kb`` times — brotli/gzip
compress this to near-zero (most ``.br`` siblings end up ~50 bytes),
which is what produced the unrealistic throughput.

Usage:
    python3 sprint34_synthetic_data.py [out_dir]

Default out_dir: /tmp/static
"""
from __future__ import annotations

import gzip
import os
import sys


# 1024-byte pattern, same as Sprint 33 used.
_PATTERN = (b'abcdefghijklmnopqrstuvwxyz0123456789' * 30)[:1024]


# (filename, kilobytes, compress?)
_TEXT_FILES = [
    ('reset.css', 8),
    ('layout.css', 25),
    ('theme.css', 18),
    ('components.css', 200),
    ('utilities.css', 60),
    ('analytics.js', 12),
    ('helpers.js', 22),
    ('app.js', 200),
    ('vendor.js', 300),
    ('router.js', 35),
    ('header.html', 120),
    ('footer.html', 55),
    ('icon-sprite.svg', 70),
    ('logo.svg', 15),
    ('manifest.json', 2),
]


# Binary files — no precompressed siblings.  The static-rotate.lua
# rotation requests these 5 paths uncompressed.
_BINARY_FILES = [
    ('regular.woff2', 18),
    ('bold.woff2', 18),
    ('hero.webp', 45),
    ('thumb1.webp', 8),
    ('thumb2.webp', 6),
]


def write(out: str, name: str, content: bytes, *, compress: bool) -> None:
    """Write *name* (and optionally .br / .gz siblings) into *out*."""
    with open(os.path.join(out, name), 'wb') as fh:
        fh.write(content)
    if not compress:
        return
    try:
        import brotli  # type: ignore[import-untyped]
        with open(os.path.join(out, name + '.br'), 'wb') as fh:
            fh.write(brotli.compress(content))
    except ImportError:
        sys.stderr.write('WARN: brotli not installed; skipping .br siblings\n')
    with open(os.path.join(out, name + '.gz'), 'wb') as fh:
        fh.write(gzip.compress(content))


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else '/tmp/static'
    os.makedirs(out, exist_ok=True)

    for name, kb in _TEXT_FILES:
        body = _PATTERN * kb
        write(out, name, body, compress=True)
        print(f'  wrote {name:20s} {len(body):>9d} bytes (with .br/.gz)')

    for name, kb in _BINARY_FILES:
        body = _PATTERN * kb
        write(out, name, body, compress=False)
        print(f'  wrote {name:20s} {len(body):>9d} bytes (binary, no siblings)')

    # Summary: spot-check the .br sizes — these are the actual response
    # bytes wrk sees on a precompressed-sibling hit.
    print()
    print('  precompressed sibling sizes (these are the actual r/s payloads):')
    for name, _ in _TEXT_FILES:
        br = os.path.join(out, name + '.br')
        if os.path.exists(br):
            print(f'    {name + ".br":24s} {os.path.getsize(br):>6d} bytes')


if __name__ == '__main__':
    main()
