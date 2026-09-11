"""Wire-format machinery: HTTP/2 frames, structured fields, listening sockets.

Four things share this package, and they are independent of one another.
[`frame_types`][blackbull.protocol.frame_types], ``frame`` and
``hpack_fastpath`` are the HTTP/2 frame hierarchy and its codec; ``stream``
holds per-stream state; ``structured_fields`` is the RFC 9651 parser the
header layer uses, which HTTP/1.1 reaches for just as much;
[`rsock`][blackbull.protocol.rsock] creates the bound, listening sockets a
server is started on.

Nothing is re-exported here — import the submodule you want, and pay for
only that one.
"""
