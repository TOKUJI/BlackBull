# Local robustness probe (`just vuln-check`)

`just vuln-check [base_url="http://127.0.0.1:8000"] [h2_url="https://127.0.0.1:8443"] [lane="all"]`
(recipe arguments are positional, so `just vuln-check http://127.0.0.1:8000
https://127.0.0.1:8443 h2` runs one lane; `tools/security/probe.py --lane h2`
does the same) runs `tools/security/probe.py` against **already-running** local servers —
normally the [minimal fixture app](fixture-app.md) started with
`just vuln-target-up` — and prints one results table per lane (check id,
severity, verdict, detail). The same run writes a markdown report with both
tables to `bench/results/security/<UTC-timestamp>.md` (created on demand).
Exit code: `0` all PASS, `1` any FAIL/TIMEOUT, `2` the safety gate refused a
target. This harness is deliberately **not** part of the normal `pytest` run
(`pytest.ini` keeps `tools/` out of collection); the checks need live servers.

## Lanes

| Lane | Default target | Transport | Checks |
|---|---|---|---|
| `h1` | `http://127.0.0.1:8000` | HTTP/1.1 over cleartext | BASELINE-\*, H1-ROBUST-\*, SMUGGLE-\*, CHUNK-\*, TRAILER-\*, RANGE-\*, EXPECT-\*, HOST-\*, STATE-\*, STATIC-\*, SYMLINK-\*, WS-\*, HDR-\* |
| `h2` | `https://127.0.0.1:8443` | HTTP/2 over TLS (ALPN `h2`) | H2-BASE-\*, H2-ROBUST-\*, TLS-\* |

`--lane h1|h2|all` selects the lane(s); `--run-timeout` bounds the whole run
across all lanes.

## Safety gates (enforced in code, not just here)

- **Loopback allow-list.** `parse_target()` refuses any host that is not
  exactly `127.0.0.1`, `::1`, or `localhost` (case-insensitive), and refuses
  schemes other than `http`/`https` and URLs with userinfo — before any
  socket is opened. A host name is then resolved exactly once; every
  resolved address must itself be loopback, and the probes dial the literal
  resolved address (the name stays only in request authorities and TLS SNI).
  The gate covers **both** lane URLs on every run, even when `--lane`
  selects one. A refusal exits with code 2.
- **TLS verification is never disabled.** The h2 lane verifies the server
  certificate against `--tls-ca` (default: the fixture's published
  certificate in the per-user private runtime directory that
  `tools/security/paths.py` names — created 0700 with an owner check, so no
  other local user can substitute the trust anchor). An https target without
  a verifiable CA file is refused (exit 2) rather than probed unverified.
- **Timeouts.** Every check is bounded by `--check-timeout` (default 5 s)
  and the whole run by `--run-timeout` (default 120 s). Each check gets one
  absolute asyncio deadline; every session of that check — multi-variant
  checks included — spends only the time remaining until it, and every
  scenario closes with an RST abort so a peer that stops reading cannot trap
  teardown; a check that hits its deadline records a TIMEOUT verdict and is
  cancelled. The run cannot hang. The run budget stops later checks from
  starting; an in-flight check may overshoot it by one connect/teardown
  slack.
- **WebSocket exchanges are raw TCP against the same gated target.**
  WS-001 writes its upgrade request to the h1 lane's host:port directly (the
  scenario vocabulary parses HTTP responses, not post-upgrade frames) —
  `Probe.ws_attempt` uses the same `parse_target`-gated `Target`, one
  connection budget slot, and the same session deadline. The `/ws` path is a
  route on the fixture, not a new URL form; no other host or port is ever
  contacted.
- **Connection caps.** At most 4 concurrent and 96 total connections per run
  (`ConnectionBudget`, one instance shared by every lane of the run), TLS
  handshake attempts included; every connection is closed in a `finally`
  block.
- **No real DoS.** H1-ROBUST-011 is slow-send *lite*: 2 connections, one
  bounded hold of at most 5 s, then abort. The flood-shaped h2 checks are
  *lite* by construction: H2-ROBUST-006 resets exactly 20 streams one at a
  time (never more than one open, so the advertised
  `MAX_CONCURRENT_STREAMS` is never exceeded), H2-ROBUST-007 sends 30
  CONTINUATION frames on one stream, and H2-ROBUST-008's HPACK bomb decodes
  to at most 4 MiB from ~4 KiB of encoded block (the cap is asserted in
  code and in the unit tests). RANGE-001 sends at most 15 ranges per
  request. Nothing in the harness floods, loops, or holds more than the
  per-check deadline.

## Verdicts and severity

`PASS` — the check's mechanical oracle held. `FAIL` — the oracle was
violated. `TIMEOUT` — no answer within the bound (counts as failure for the
exit code). `SKIP` — the check could not be exercised by this client (for
example its TLS stack refuses to offer the version under test); it is
recorded, never reported as `PASS`, and does not fail the run. The severity column is the rank a *failure* of that check
carries, per [severity criteria](severity.md); a hang or crash escalates the
H1-ROBUST/H2-ROBUST/CHUNK checks to High, as that document's defaults table
prescribes.

## Checks and oracles — h1 lane

| Check | Oracle (mechanical) |
|---|---|
| BASELINE-001/003 | `GET /` → 200 and body exactly `ok` (003 runs after all abuse) |
| BASELINE-002 | `GET /json` → 200 and body is exactly `{"ok": true}` JSON |
| H1-ROBUST-001 | request line `FOO / HTTP/1.1` → 4xx or 501/505 or connection close; 2xx/3xx and other 5xx = FAIL |
| H1-ROBUST-002 | 100 KiB header value → 4xx (431/400) or close; 5xx = FAIL |
| H1-ROBUST-003 | garbage request-line bytes → 400 or close |
| H1-ROBUST-004 | CRLF in a header value (a literal `value\r\nX-Injected: 1`, and a bare-CR variant) → reject/close, or the value is kept opaque/sanitized with no injected response header and no CR/LF in the echo (RFC 9110 §5.5); a bare CR that splits into a second field line = FAIL |
| H1-ROBUST-005 | `Content-Length: 100` with 5 body bytes then FIN → 4xx/408 or close |
| H1-ROBUST-006 | field name with whitespace (`Foo Bar: x`, `Foo : x`, `Foo\tBar: x`) → 400 or close (RFC 9112 §5.1) |
| H1-ROBUST-007 | obs-fold continuation (`Foo: a` + ` b`; injection variant ` + X-Smuggle: 1`) → 400 or close, **or** safe coalescing: exactly one `foo` field whose value is `a b`/`a\tb` with no CR/LF kept and no separate injected field (RFC 9112 §5.2). The fixture refuses obs-fold with 400; the coalescing branch exists for peers that accept it. |
| H1-ROBUST-008 | HTTP/1.1 request without `Host` → 400 or close (RFC 9112 §3.2) |
| H1-ROBUST-009 | absolute-form request line (same-authority and foreign-authority variants) → 200 with body `ok`, or 400, or close; anything else = FAIL. **Documented actual behavior:** the fixture accepts absolute-form and routes by the target *path* for both variants — a 200 `ok` — without comparing the absolute authority against `Host`. |
| H1-ROBUST-010 | NUL byte and overlong UTF-8 in the path (raw `\x00`, NUL-truncation bait `/\x00/static/hello.txt`, raw `\xc0\xaf`, `/%c0%af`) → 400/404 or close. A 200 is always FAIL: the target carried bytes the router must never see, whatever the body. |
| SMUGGLE-001 | `Content-Length` and `Transfer-Encoding: chunked` together → 400 or close (RFC 9112 §6.3); any other status = FAIL |
| SMUGGLE-002 | `Content-Length` plus obfuscated/duplicated TE (`chunked, identity`; `xchunked`; `Transfer-Encoding: chunked` + `Transfer-Encoding: x`; TE listed first) → 400 or close only (RFC 9112 §6.1/§6.3), then the STATE-001 follow-up |
| SMUGGLE-003 | duplicate `Content-Length` with different values → 400 or close (RFC 9112 §6.3; catalogued `two_content_lengths`), then the STATE-001 follow-up |
| CHUNK-001 | chunk extension (`1;x=y`, `4;foo="a b"`) + valid chunked body → 200 with the body echoed **exactly**, or 400, or close; then the STATE-001 follow-up |
| CHUNK-002 | malformed chunk sizes (`-1`, `0x10`, `FFFFFFFFFFFFFFFF`) then FIN → 400 or close (RFC 9112 §7.1); then the STATE-001 follow-up. The FIN is part of the oracle: a size that swallows the pipelined `GET /` as chunk data cannot fake a hang-free exchange. |
| STATE-001 | standalone battery: after **each** abusive exchange (chunk extension, obs-fold, garbage line, TE obfuscation, truncated body) a `GET /` pipelined on the same connection must yield exactly one clean `200 ok`, or the connection must be closed; a garbled/mixed response, a second response, or `GET /` ignored on a live connection = FAIL. The same oracle runs inside every SMUGGLE-\*/CHUNK-\* variant. |
| H1-ROBUST-011 | slow-send lite: 2 connections each send a partial request line (`GET / HT`) and hold for `min(check-timeout, 5s)`; acceptable outcomes are no response (the server may wait), 408, or close. Any other answer, or a check-deadline overrun, = FAIL. Server survival after the hold is BASELINE-003's row. |
| STATIC-001 | `GET /static/../fixture_app.py` and `/static/%2e%2e/fixture_app.py` → 400/403/404; any 200 = FAIL |
| TRAILER-001 | chunked request with trailer fields: forbidden ones (`Content-Length`, `Transfer-Encoding`, `Host`, RFC 9110 §6.5.1) → 400 or close, then the STATE-001 follow-up; a permitted custom trailer (`X-Smuggle: 1`) is ignored safely — 200 with the body echoed **exactly** or 400, and the pipelined `GET /echo-headers` echo must not contain the trailer name and must keep no CR/LF (RFC 9112 §7.1.2; CVE-2023-46589 / CVE-2025-53643 / CVE-2026-22815 class) |
| RANGE-001 | `Range` abuse on `/static/hello.txt` (≤ 15 ranges: `bytes=0-0,-1,1-99999999999`, inverted `10-5`, suffix `-0`, junk `--3`, 15 single-byte ranges) → **200/206/416 only**, nothing else: 200 must carry the whole file (an ignored Range is legal), a single-range 206 must match its `Content-Range` exactly (start ≤ end < size, body length = end−start+1), `multipart/byteranges` 206 is accepted as-is, 416 is accepted (its `Content-Range`, when present, must use `bytes */size`); a close without a response, a 5xx, or a deadline overrun is FAIL/TIMEOUT (CVE-2011-3192 class) |
| EXPECT-001 | `Expect: 100-continue` with the body sent immediately, and a bogus `Expect: 100-continue, x`, each pipelined with `GET /`: informational responses must be `100` at most (or absent — RFC 9112 §10.1.1 lets a server omit it once the body arrived); the request earns 200 with the body echoed **exactly**, 417, or 400; the pipelined `GET /` must then be exactly one clean `200 ok` or the connection must close — a leftover body read as the next request is the CVE-2026-103399 / CVE-2024-24791 class and FAILs |
| HOST-001 | duplicate `Host` (identical and conflicting) and empty `Host: ` → 400 or close (RFC 9112 §3.2: "more than one Host header field or … an invalid field value"), then the STATE-001 follow-up (host-confusion class: CVE-2026-71554, CVE-2026-34525) |
| SYMLINK-001 | `GET /static/escape-link.txt` — a symlink under the static root whose target is outside it — → 400/403/404 or close; any 200 = FAIL (CWE-59, CVE-2024-23334 / CVE-2024-42367 class). Positive control `hello-link.txt` → `hello.txt` (in-root): 200 with that file, or a safe refusal — both designs acceptable |
| WS-001 | raw WebSocket upgrade on `/ws`: a bad `Sec-WebSocket-Key` and `Sec-WebSocket-Version: 12` → 400/426 or close (a 101 = FAIL); a flooded handshake (100-entry `Sec-WebSocket-Protocol` list + 200 extra headers, CVE-2024-37890 class) → 101/4xx or close, never a hang; and after a valid 101 (its `Sec-WebSocket-Accept` must be present) an **unmasked** TEXT frame → close with code **1002** or a bare connection close (RFC 6455 §5.1/§5.3) — an echo, a masked server frame, any other close code, or a connection left open = FAIL |
| HDR-001 | the `server:` response header of `GET /`, if present, must not match: a POSIX path under `/home`, `/users`, `/usr`, `/var`, `/etc`, `/opt`, `/tmp`, `/root`, `/srv`, `/app`, `/workspace`; a Windows drive path; `site-packages`/`dist-packages`; or `python`, `cpython`, `py/<digit>` |

## Checks and oracles — h2 lane

| Check | Oracle (mechanical) |
|---|---|
| H2-BASE-001 | `GET /` over HTTP/2 (TLS + ALPN `h2`) → 200 and body exactly `ok` |
| H2-ROBUST-001 | HEADERS with a missing pseudo-header, or pseudo-headers after a regular field (RFC 9113 §8.1/§8.3) → `RST_STREAM`/`GOAWAY` carrying `PROTOCOL_ERROR`, or close; a dispatched request or any other error code = FAIL |
| H2-ROBUST-002 | DATA on an idle stream, or DATA on stream 0 (RFC 9113 §6.1) → connection error: `GOAWAY` carrying `PROTOCOL_ERROR`, or close. A stream error (`RST_STREAM`) is FAIL — the RFC requires a connection error. |
| H2-ROBUST-003 | unknown frame type `0xfa` with the reserved bit clear (RFC 9113 §4.1) → ignored: a `GET /` sent afterwards on the same connection must still answer 200 `ok` |
| H2-ROBUST-004 | 128 KiB header list, fragmented into HEADERS + CONTINUATION frames of ≤16 KiB (so the *list*, not the frame size, is what overflows) → `431`, or `RST_STREAM REFUSED_STREAM`, or `GOAWAY ENHANCE_YOUR_CALM`/`REFUSED_STREAM`, or close; no crash |
| H2-ROBUST-005 | PRIORITY frame with stream dependency = own stream (RFC 9113 §5.3.1) → `PROTOCOL_ERROR` (stream or connection error), or close |
| H2-ROBUST-006 | bounded Rapid Reset (CVE-2023-44487 class): exactly 20 streams, each `HEADERS` + an immediate `RST_STREAM CANCEL`, opened **one at a time** so the advertised `MAX_CONCURRENT_STREAMS` is never exceeded (it is reported from the server's SETTINGS); the burst is legal traffic, so only `NO_ERROR`/`CANCEL` (graceful) or `REFUSED_STREAM`/`ENHANCE_YOUR_CALM` (flood defense) error frames are acceptable — `PROTOCOL_ERROR`/`INTERNAL_ERROR` over legal frames = FAIL — and a fresh request on the same connection (or, if the burst connection was closed, on a fresh one) must still answer 200 `ok` |
| H2-ROBUST-007 | one legal header block cut into 30 CONTINUATION frames then `END_HEADERS` (CVE-2023-45288 / CVE-2024-27316 / CVE-2024-27983 class): the request **completes** (2xx/4xx) **or** is refused (`431`, or `GOAWAY`/`RST_STREAM` carrying `ENHANCE_YOUR_CALM`/`REFUSED_STREAM`/`PROTOCOL_ERROR`/`NO_ERROR` — a header-block limit may signal PROTOCOL_ERROR per RFC 9113 §4.3) or the connection closes; a crash, a wrong error code, or a deadline overrun = FAIL/TIMEOUT |
| H2-ROBUST-008 | HPACK bomb lite (CVE-2016-6581 / CVE-2022-41723 class): a ~3 KiB seed block fills the dynamic table with one 4 KiB entry, then a ~4 KiB block of 1000 indexed references decodes to ≤ 4 MiB (the cap is asserted in code) on stream 3 of the same connection: bounded **completion** (2xx/4xx) **or** refusal (`431`, `GOAWAY`/`RST_STREAM` with `ENHANCE_YOUR_CALM`/`REFUSED_STREAM`/`PROTOCOL_ERROR`/`COMPRESSION_ERROR`/`NO_ERROR` — a decoder expansion cap may trip COMPRESSION_ERROR) or close; a crash or a wrong error code = FAIL (the observed refusal is recorded verbatim in the detail) |
| H2-BASE-002 | after all h2 abuse a fresh HTTP/2 request → 200 `ok` (server survived) |
| TLS-001 | TLS 1.0 and 1.1 handshake attempts must fail; TLS 1.2 and 1.3 must succeed negotiating ALPN `h2` at that version. A client stack that refuses to offer TLS 1.0/1.1 outright (`NO_PROTOCOLS_AVAILABLE`) is reported as **not exercised** — the check then records `SKIP`, never `PASS`, because no ClientHello reached the server (PR #479 review M2). The fixture additionally pins `minimum_version = TLSv1_2` server-side. A server-side refusal oracle would need a hand-rolled ClientHello (open question, below). |

## Implementation note

The wire-level checks are driven by `blackbull.fault_injection`'s scenario
machinery against the external running servers — one executor style for both
lanes, and no in-process coupling:

- **h1 lane:** `scenario_h1` steps (`SendRawBytes`, `HalfClose`, `Sleep`,
  `ReadResponse`) executed by `HTTP1Client.execute_scenario`. That machinery
  demonstrably drives servers other than BlackBull
  (`tests/unit/test_fault_h1_client_third_party.py` runs the same executor
  against CPython's `http.server`). `SMUGGLE-001` reuses the catalogued
  scenario `content_length_and_transfer_encoding` and `SMUGGLE-003` the
  catalogued `two_content_lengths` verbatim, with their read steps retimed to
  `--check-timeout`. The baseline, static, and header checks use the same
  executor with plain well-formed raw requests so one `asyncio` deadline
  bounds every check identically.
- **h2 lane:** `scenario_h2_client` steps (`SendPreface`, `SendFrame`,
  `SendRawBytes`, `ReadResponse`) executed by `HTTP2Client.execute_scenario`
  in `scenario_mode=True`, so the scenario owns the wire from byte zero and
  no receive loop races its reads. `SendFrame`'s raw frame type is exactly
  what H2-ROBUST-003 needs (an unregistered type); `encode_headers`/
  `encode_frame` build HEADERS frames for the ordered-field-block faults
  (H2-ROBUST-001) and the CONTINUATION fragmentation of H2-ROBUST-004, with
  `hpack` — already a runtime dependency — encoding fields **in the given
  order** so pseudo-header order violations are expressible. The catalogued
  `h2_client` cases informed the shapes but several are 30-second floods,
  deliberately out of scope here; the probe's h2 checks are one-shot frames.
- **TLS-001** uses the standard-library `ssl` module for bounded handshake
  attempts (the task's sanctioned "openssl s_client or python ssl").

## Referenced vulnerability sources

Researched for M4 from the NVD CVE API (keyword and `cveId` lookups) and
GitHub's global advisories (`gh api /advisories`, `?ecosystem=pip&affects=…`);
each entry below was fetched and read, and the vector is quoted from what the
database actually returned. NVD URLs are `https://nvd.nist.gov/vuln/detail/<id>`,
advisory URLs `https://github.com/advisories/<GHSA-id>`.

| Source | Vector (one line) | Mapping |
|---|---|---|
| CVE-2024-23334 (GHSA-5h86-8mv2-jq9f) | aiohttp `follow_symlinks=True` static routes resolve symlinks with no root-boundary check → arbitrary file read | SYMLINK-001, STATIC-001 |
| CVE-2024-42367 (GHSA-jwhx-xcg6-8xhj) | aiohttp: compressed files presented as symlinks also escape the static root | SYMLINK-001 |
| CVE-2023-37276 (GHSA-45c4-8wx5-qw6w) | aiohttp/llhttp: crafted request misparses a header value → request smuggling | SMUGGLE-002 |
| CVE-2024-52304 (GHSA-8495-4g3g-x7pr) | aiohttp pure-Python parser reads newlines in chunk extensions wrong → request smuggling | CHUNK-001 |
| CVE-2025-53643 (GHSA-9548-qrrj-x5pj) | aiohttp parses the chunked **trailer section** wrong → request/response smuggling | TRAILER-001 |
| CVE-2026-22815 (GHSA-w2fm-2cpv-w7v5) | aiohttp accepts unlimited trailer headers → unbounded memory use | TRAILER-001 |
| CVE-2023-46589 | Tomcat: an oversize trailer field splits one request into two → smuggling behind a reverse proxy | TRAILER-001 |
| CVE-2023-45648 | Tomcat: specially crafted invalid trailer header treated as multiple requests → smuggling | TRAILER-001 |
| CVE-2025-12642 | lighttpd 1.4.80 merges trailer fields into headers after parsing → header smuggling | TRAILER-001 |
| CVE-2025-59822 | http4s: improper trailer-section handling → request smuggling / cache poisoning | TRAILER-001 |
| CVE-2023-44487 | HTTP/2 Rapid Reset: streams cancelled faster than work can be shed → server resource exhaustion | H2-ROBUST-006 |
| CVE-2023-45288 | HTTP/2 CONTINUATION flood (Nowotarski): excessive CONTINUATION frames keep the endpoint parsing headers | H2-ROBUST-007 |
| CVE-2024-27316 | nghttp2 buffers over-limit CONTINUATION headers while building the 413 → memory exhaustion | H2-ROBUST-007 |
| CVE-2024-27983 | Node.js HTTP/2: CONTINUATION headers + abrupt close → race/memory crash | H2-ROBUST-007 |
| CVE-2016-6581 (GHSA-ffq8-576r-v26g) | HPACK bomb: a tiny block expands via the dynamic table (Python `hpack` 1.0.0–2.2.0) | H2-ROBUST-008 |
| CVE-2022-41723 | Go: malicious HTTP/2 stream → excessive HPACK-decoder CPU from small requests | H2-ROBUST-008 |
| CVE-2011-3192 | Apache: overlapping byte ranges in `Range` → memory/CPU exhaustion | RANGE-001 |
| CVE-2005-2728 | Apache byte-range filter: huge `Range` field → memory consumption | RANGE-001 |
| CVE-2020-10705 | Undertow: `Expect: 100-continue` requests can exhaust memory → DoS | EXPECT-001 |
| CVE-2024-24791 | Go net/http: a final (non-1xx) answer to `Expect: 100-continue` leaves the connection invalid → desync at the next request | EXPECT-001 |
| CVE-2026-103399 | libsoup: early final response before the body is read; leftover body bytes are parsed as the next request | EXPECT-001 |
| CVE-2019-20372 | nginx `error_page` misconfiguration → request smuggling | SMUGGLE-002 |
| CVE-2022-41721 | Go `MaxBytesHandler`: unconsumed body bytes read as HTTP/2 frames → request tunneling | SMUGGLE-001 (class) |
| CVE-2024-1135 (GHSA-w3h3-4rj7-4ph4) | gunicorn request smuggling → endpoint restriction bypass | SMUGGLE-001 |
| CVE-2024-6827 (GHSA-hc5x-x2vx-497g) | gunicorn HTTP request/response smuggling | SMUGGLE-002 |
| CVE-2018-1000164 | gunicorn: CRLF sequences in HTTP headers not neutralized | H1-ROBUST-004 |
| CVE-2020-7695 | uvicorn HTTP response splitting | H1-ROBUST-004 |
| CVE-2024-37890 | ws (Node.js): a handshake with more headers than `maxHeadersCount` crashes the server | WS-001 |
| CVE-2026-69243 (GHSA-mfx4-hv73-q22v) | aiohttp: HTTP request smuggling via the WebSocket upgrade | WS-001 |
| CVE-2026-54274 (GHSA-xcgm-r5h9-7989) | aiohttp: incomplete WebSocket frame payloads bypass memory limits | WS-001 (class) |
| CVE-2018-1000518 (GHSA-6g87-ff9q-v847) | websockets: memory-exhaustion DoS during handshake/parsing | WS-001 (class) |
| CVE-2026-71554 (GHSA-6hr6-w5qg-qmwg) | Python `h2`: duplicate `Host` header can facilitate request smuggling | HOST-001 |
| CVE-2026-34525 (GHSA-c427-h43c-vf67) | aiohttp accepts duplicate `Host` headers (host confusion) | HOST-001 |
| CVE-2025-57804 (GHSA-847f-9342-265h) | Python `h2`: illegal characters in headers → request smuggling | H2-ROBUST-001 (class) |
| CVE-2009-3555 | TLS renegotiation: unauthenticated request prefix injection into HTTPS sessions | TLS-001 (class) |
| CVE-2024-24762 | python-multipart (uvicorn's form parser): `Content-Type` regex → ReDoS stall | consulted — not judged (timing oracle) |
| CVE-2024-7592 | CPython `http.cookies`: quadratic cookie parsing → CPU exhaustion | consulted — not judged (timing oracle) |

One requested lookup did **not** match its assumed class: CVE-2024-27351 is
Django's `Truncator.words` regex DoS (per NVD), not an aiohttp smuggling
case; the aiohttp smuggling trail is CVE-2023-37276, CVE-2024-52304 and the
2025/2026 trailer advisories above. Hypercorn returned zero pip
advisories (`gh api /advisories?ecosystem=pip&affects=hypercorn`).

## Coverage mapping (M4 research → checks)

| Vector class | Source (CVE/advisory) | Coverage | Check |
|---|---|---|---|
| CL.TE / CL-vs-TE smuggling | CVE-2023-37276, CVE-2024-1135, CVE-2022-41721 | COVERED | SMUGGLE-001/002, STATE-001 |
| duplicate/ambiguous Content-Length | GHSA-xx9p-xxvh-7g8j class | COVERED | SMUGGLE-003 |
| TE obfuscation | CVE-2019-20372, CVE-2024-6827 | COVERED | SMUGGLE-002 |
| chunk extensions and sizes | CVE-2024-52304 | COVERED | CHUNK-001/002 |
| chunked trailer section | CVE-2023-46589, CVE-2023-45648, CVE-2025-53643, CVE-2025-12642, CVE-2025-59822, CVE-2026-22815 | COVERED (M4) | TRAILER-001 |
| CRLF / obs-fold injection | CVE-2018-1000164, CVE-2020-7695 | COVERED | H1-ROBUST-004/006/007 |
| path traversal | CVE-2024-23334 (traversal form) | COVERED | STATIC-001 |
| symlink escape from static root | CVE-2024-23334, CVE-2024-42367 | COVERED (M4) | SYMLINK-001 |
| Range abuse | CVE-2011-3192, CVE-2005-2728 | COVERED (M4) | RANGE-001 |
| Expect: 100-continue | CVE-2020-10705, CVE-2024-24791, CVE-2026-103399 | COVERED (M4) | EXPECT-001 |
| duplicate/conflicting Host | CVE-2026-71554, CVE-2026-34525 | COVERED (M4) | HOST-001 |
| Rapid Reset | CVE-2023-44487 | COVERED (M4, bounded) | H2-ROBUST-006 |
| CONTINUATION flood | CVE-2023-45288, CVE-2024-27316, CVE-2024-27983 | COVERED (M4, bounded) | H2-ROBUST-007 |
| HPACK bomb | CVE-2016-6581, CVE-2022-41723 | COVERED (M4, bounded) | H2-ROBUST-008 |
| header-list floods | CVE-2023-36478 class | COVERED | H2-ROBUST-004, H1-ROBUST-002 |
| pseudo-header / frame-order faults | RFC 9113, CVE-2025-57804 class | COVERED | H2-ROBUST-001..005 |
| WebSocket handshake validation, masking | CVE-2024-37890, CVE-2026-69243, RFC 6455 §5.1/§5.3 | COVERED (M4) | WS-001 |
| TLS version floor / ALPN | CVE-2009-3555 (class) | PARTIAL | TLS-001 (floor only; no renegotiation probe — see open questions) |
| slow-send / slowloris | class | PARTIAL | H1-ROBUST-011 (lite) |
| h1→h2 request tunneling | CVE-2022-41721 | PARTIAL | framing checks cover the h1 side; no MaxBytesHandler analog in the fixture surface |
| WS frame memory limits / compression | CVE-2026-54274, GHSA-mq44-7p77-q5h7 | GAP (skipped) | needs fragment/deflate state juggling and a memory oracle — not one-shot mechanical |
| regex DoS (Content-Type, cookies) | CVE-2024-24762, CVE-2024-7592 | GAP (skipped) | judged by timing, not a mechanical accept set |
| unbounded pipelining / body-size limits | CVE-2026-54273 class | GAP (skipped) | capacity behavior, not attack semantics — a load test |
| decompression bombs | CVE-2025-69223 class | GAP (skipped) | the fixture never decompresses request bodies — out of surface |

## Open questions (documented, not checked)

- **Server-side TLS 1.0/1.1 refusal.** The python.org/OpenSSL build refuses
  to *offer* TLS 1.0/1.1 (`NO_PROTOCOLS_AVAILABLE`), so TLS-001's
  sub-1.2 attempts cannot reach the server. A raw ClientHello probe with
  modern extensions would close this gap; attempts to hand-roll one showed
  bare closes for even valid hellos, so it is parked as an open question.
- **Embedded colon in a field name.** HTTP/1.1 splits the field name at the
  *first* colon, so a "colon in the name" is unobservable on the wire —
  `Foo:Bar: x` is a legal `Foo` field with value `Bar: x`. H1-ROBUST-006
  therefore tests the crisp cases (whitespace in the name).
- **Chunk-size overflow outcome.** `FFFFFFFFFFFFFFFF` is valid *hex* per
  RFC 9112 §7.1's grammar, so "400-or-close" accepts a silent close; the
  fixture answers `-1`/`0x10` with 400 but closes silently on the overflow
  size. Both outcomes are on the safe side but inconsistent — severity.md's
  Info rank describes exactly this, and it is recorded in BLA-526's findings
  rather than as a check failure.

## Recording findings

- Run results: comment on YouTrack issue BLA-526 (`just yt-comment`).
- A confirmed defect: its own issue, with repro steps and the severity rank
  from [severity criteria](severity.md).
- Fixes: land as PRs plus a fast deterministic regression test added to the
  normal `pytest` suite.

## Next

- [Minimal fixture app](fixture-app.md) — the probe target definition.
- [Severity criteria](severity.md) — how findings are ranked.
