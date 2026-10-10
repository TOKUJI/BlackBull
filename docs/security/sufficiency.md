# Sufficiency population tables (BLA-526 M5, G3)

Sufficiency is defined by a finite, enumerated population: every element
maps to a check that is actually run and can detect a defect, to a stated
out-of-scope reason, or to the unsupported list recorded on BLA-526. The
tables are A. normative requirements, B. state x input, C. resource ledger,
D. threat corpus (E. reachability and G. detection power are measurement
machinery — see the gate). Check IDs are validated against the registry by
`tests/unit/test_security_probe.py`.

## A. Normative requirements (receiver-side MUST / MUST NOT)

| Requirement | Maps to |
|---|---|
| Request-line syntax (method, target, version) | H1-ROBUST-003 |
| Field-line syntax; no bare CR; token field names | H1-ROBUST-004, H1-ROBUST-006 |
| Obs-fold is obsolete and must not be produced | H1-ROBUST-007 |
| Host is mandatory and singular | H1-ROBUST-008, HOST-001 |
| Absolute-form request target is routed by authority | H1-ROBUST-009 |
| NUL and overlong request targets are refused | H1-ROBUST-010 |
| Absurd request lines are refused without parsing a request | H1-ROBUST-005 |
| Unknown methods are answered 501 Not Implemented | H1-ROBUST-001 records the fixture's 405; tracked as BLA-527 (unsupported list until fixed) |
| Oversized header lines/fields earn 431 | H1-ROBUST-002 |
| Content-Length + Transfer-Encoding together must be refused | SMUGGLE-001 (RFC 9112 §6.3) |
| Obfuscated or duplicated Transfer-Encoding must be refused | SMUGGLE-002 |
| Duplicate or invalid Content-Length must be refused | SMUGGLE-003 |
| Chunked chunk-size syntax | CHUNK-002 |
| Chunk extensions are bounded and ignored safely | CHUNK-001 |
| Prohibited trailer fields must not be applied | TRAILER-001 (RFC 9112 §7.1.2) |
| No state contamination across abusive exchanges | STATE-001 |
| Expect: 100-continue handling; late final answers must not desync | EXPECT-001 (RFC 9112 §10.1.1) |
| Range: single, overlapping, inverted and unsatisfiable ranges | RANGE-001 |
| Request targets must not escape the static root (encoded or via symlinks) | STATIC-001, SYMLINK-001 |
| Server identification headers must not leak internals | HDR-001 |
| Partial request lines must not be executed as requests | H1-ROBUST-011 (long tier) |
| h2 request pseudo-headers present, well-ordered | H2-ROBUST-001 (RFC 9113 §8.3.1) |
| h2 stream state violations earn stream/connection errors | H2-ROBUST-002 |
| Unknown h2 frame types are ignored or connection-errors per spec | H2-ROBUST-003 |
| Header list size is bounded (SETTINGS_MAX_HEADER_LIST_SIZE) | H2-ROBUST-004 |
| Priority self-dependency is a stream error | H2-ROBUST-005 (RFC 9113 §5.3.1) |
| Rapid Reset must not amplify resource use | H2-ROBUST-006 (bounded; CVE-2023-44487) |
| CONTINUATION floods must terminate | H2-ROBUST-007 (bounded; CVE-2023-45288, CVE-2024-27316) |
| HPACK dynamic table expansion is bounded | H2-ROBUST-008 (RFC 7541; CVE-2016-6581) |
| TLS versions below 1.2 refused; ALPN negotiates h2 on the h2 lane | TLS-001 (RFC 7301; RFC 9113 §9.2) |
| WebSocket handshake validation (key, version) | WS-001 (RFC 6455 §4.2.2) |
| Client WebSocket frames must be masked | WS-001 (RFC 6455 §5.1) |
| Each lane negotiates exactly its protocol (ALPN/preface) | LANE-001, LANE-002 |
| No auto-generated API surface (/docs, /openapi.json stay 404) | ROUTES-001 |
| Deterministic streaming responses keep their exact length | ROUTES-002 |
| JSON→dataclass and form parsing: invalid input is 4xx, never 5xx | ROUTES-003 |
| Error responses leak no traceback or exception text | ROUTES-004 |
| Middleware contracts: compression, CORS, cache, trusted proxy, precompressed static | ROUTES-005 |
| Positive baselines: GET / on HTTP/1.1 and h2 | BASELINE-001, BASELINE-002, H2-BASE-001, H2-BASE-002 |
| Liveness after every probe | BASELINE-003, per-check canary (G2-2) |
| gRPC message bounds (RESOURCE_EXHAUSTED on oversize) | Unsupported list: gRPC lanes arrive with G4-3/G4-4 |
| CONNECT tunnelling and upgrade relays | Out of scope: the fixture serves neither |
| Transfer codings other than chunked | Out of scope: the fixture must reject them (covered by SMUGGLE-002's TE matrix) |

## B. State x input (expected behaviour -> mapping)

| Connection state | Input event | Expected | Maps to |
|---|---|---|---|
| Request line wait | stalled bytes | defence times out the header window | H1-ROBUST-011 (long tier) |
| Request line wait | garbage / NUL | 400 or close | H1-ROBUST-003, H1-ROBUST-010 |
| Header read | oversize line/field | 431 | H1-ROBUST-002 |
| Header read | obs-fold / bare CR / bad names | 400 | H1-ROBUST-004, H1-ROBUST-006, H1-ROBUST-007 |
| Header read | duplicate Host / empty Host | 400 | HOST-001, H1-ROBUST-008 |
| Body read (CL) | truncated or oversize body | timeout/close, no desync | STATE-001, RANGE-001 (body caps: resource ledger C) |
| Body read (chunked) | bad sizes, bad extensions | 400 | CHUNK-001, CHUNK-002 |
| Body read (chunked) | trailers | forbidden fields refused, others ignored | TRAILER-001 |
| Body read (CL+TE, dup CL, obfuscated TE) | framing ambiguity | refuse the request | SMUGGLE-001, SMUGGLE-002, SMUGGLE-003 |
| Expect: 100-continue | early body / bogus expect list | no desync of the next request | EXPECT-001 |
| Keep-alive idle | pipelined follow-up after abuse | exactly one clean response | STATE-001, CHUNK-001 |
| Upgraded (WebSocket) | bad handshake / unmasked frame | 400-426 / close 1002 | WS-001 |
| h2 preface | missing preface / stream 0 abuse | GOAWAY PROTOCOL_ERROR | H2-ROBUST-002 |
| h2 open stream | state violations, self-priority | stream/connection error | H2-ROBUST-002, H2-ROBUST-005 |
| h2 open stream | RST floods, CONTINUATION floods | bounded refusal or completion | H2-ROBUST-006, H2-ROBUST-007 |
| h2 connection | HPACK expansion | bounded decode or COMPRESSION_ERROR | H2-ROBUST-008 |
| Any | server death | canary failure marks the run | G2-2 canary rows |
| Response send | client stall / slow read | write timeout defence | Unsupported list: long tier (M5-1 slow-read) |

## C. Resource ledger (attacker-consumable resource -> cap -> verification)

| Resource | Cap | Verification |
|---|---|---|
| Header line / total header bytes | `BB_HEADER_MAX_LINE`, `BB_HEADER_MAX_TOTAL` | H1-ROBUST-002 (431 observed) |
| Header wait | `BB_HEADER_TIMEOUT` (10 s default) | H1-ROBUST-011 long tier (408/close observed in the default window) |
| Body size | `BB_CLIENT_BODY_MAX_TOTAL`, `BB_BODY_CHUNK_MAX` | Unsupported list: load-shaped; long tier (M5-1) |
| Body wait / rate | `BB_BODY_TIMEOUT` (30 s), `BB_CLIENT_MIN_BODY_RATE` | Unsupported list: long tier (M5-1) |
| Write stall | `BB_WRITE_TIMEOUT` (30 s) | Unsupported list: long tier (M5-1 slow-read) |
| Keep-alive idle | `BB_KEEP_ALIVE_TIMEOUT` | Unsupported list: long tier (M5-1 idle) |
| h2 concurrent streams | `BB_H2_MAX_CONCURRENT_STREAMS` | H2-ROBUST-006 (20 resets never exceed the advertised cap) |
| h2 header list | `SETTINGS_MAX_HEADER_LIST_SIZE` | H2-ROBUST-004 |
| h2 idle | `BB_H2_IDLE_TIMEOUT` (300 s default) | Unsupported list: long tier (M5-1 H2 idle + ping) |
| h2 frame rate | `BB_FRAME_RATE_LIMIT`, `BB_FRAME_RATE_WINDOW` | Unsupported list: long tier (M5-1 frame-rate flood) |
| HPACK table and expansion | `SETTINGS_HEADER_TABLE_SIZE`, decoder cap | H2-ROBUST-008 (4 MiB decode cap asserted) |
| Connections | `BB_MAX_CONNECTIONS` (1,048,512 in this environment) | Unsupported list at defaults (exhaustion would be real DoS); G7-2 records the explicit-caps configuration only |
| Handler execution | `BB_REQUEST_TIMEOUT` (0 = disabled by default) | Unsupported list: no default cap (recorded on BLA-526 per G1-5) |
| gRPC message size | `BB_GRPC_MAX_MESSAGE_SIZE` | Unsupported list: with G4-3/G4-4 |

## D. threat corpus (collection: NVD CVE API + GitHub Security Advisories, fetched 2026-10-08; classes CWE-400/770, 444, 113, 22, 59, 409, 835)

| Threat class / source | Status |
|---|---|
| HTTP request smuggling (CVE-2023-37276, CVE-2024-52304, CVE-2025-53643 class) | SMUGGLE-001..003, CHUNK-001/002, STATE-001 |
| Chunked trailer smuggling (CVE-2023-46589, CVE-2023-45648, CVE-2025-12642, CVE-2025-59822) | TRAILER-001 |
| Symlink / traversal escape (CVE-2024-23334, CVE-2024-42367) | STATIC-001, SYMLINK-001 |
| Range amplification (CVE-2011-3192, CVE-2005-2728) | RANGE-001 |
| 100-continue desync / abuse (CVE-2024-24791, CVE-2026-103399, CVE-2020-10705) | EXPECT-001 |
| Duplicate Host smuggling (CVE-2026-71554, CVE-2026-34525) | HOST-001, H1-ROBUST-008 |
| WebSocket handshake/frame abuse (CVE-2024-37890, CVE-2026-69243, CVE-2018-1000518) | WS-001 |
| h2 Rapid Reset (CVE-2023-44487; CVE-2019-9514 reset flood) | H2-ROBUST-006 |
| h2 CONTINUATION / header flood (CVE-2023-45288, CVE-2024-27316, CVE-2024-27983) | H2-ROBUST-007, H2-ROBUST-004 |
| HPACK bomb (CVE-2016-6581, CVE-2022-41723, CVE-2019-9512) | H2-ROBUST-008 |
| h2 data floods / buffering (CVE-2019-9511, CVE-2019-9517) | Unsupported list: load-shaped, long tier (M5-1) |
| h2 settings/empty-frame floods (CVE-2019-9515, CVE-2019-9518) | Unsupported list: load-shaped, long tier (M5-1 frame-rate) |
| h2 resource loops via priority (CVE-2019-9513) | H2-ROBUST-005 (self-dependency half; loops are load-shaped) |
| h2 0-length header leak (CVE-2019-9516) | H2-ROBUST-004 (header-list bound) |
| MadeYouReset (CVE-2025-8671) | Unsupported list: needs elicted-reset fuzzing (G6-3) |
| TLS renegotiation injection (CVE-2009-3555 class) | PARTIAL: TLS-001 floor only (probe.md open questions) |
| Regex denial of service (CVE-2024-24762, CVE-2024-7592) | Unsupported list: timing oracle, not mechanical |
| Decompression bombs (CVE-2025-69223 class) | Out of scope: the fixture never decompresses request bodies |
| Unbounded pipelining/queues (CVE-2026-54273 class) | Unsupported list: load-shaped |
| WebSocket frame memory limits (CVE-2026-54274 class) | Unsupported list: needs fragment-state probing (G6-3) |

The unsupported list above is consolidated into one comment on BLA-526 at
the end of the milestone, with reasons, per G1-5/G5-2/G6-3/G3-3/G3-4.
