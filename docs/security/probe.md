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
| `h1` | `http://127.0.0.1:8000` | HTTP/1.1 over cleartext | BASELINE-\*, H1-ROBUST-\*, SMUGGLE-\*, CHUNK-\*, STATE-\*, STATIC-\*, HDR-\* |
| `h2` | `https://127.0.0.1:8443` | HTTP/2 over TLS (ALPN `h2`) | H2-BASE-\*, H2-ROBUST-\*, TLS-\* |

`--lane h1|h2|all` selects the lane(s); `--run-timeout` bounds the whole run
across all lanes.

## Safety gates (enforced in code, not just here)

- **Loopback allow-list.** `parse_target()` refuses any host that is not
  exactly `127.0.0.1`, `::1`, or `localhost` (case-insensitive), and refuses
  schemes other than `http`/`https` and URLs with userinfo — before any
  socket is opened. Hostnames are matched as strings; DNS is never consulted.
  The gate covers **both** lane URLs on every run, even when `--lane`
  selects one. A refusal exits with code 2.
- **TLS verification is never disabled.** The h2 lane verifies the server
  certificate against `--tls-ca` (default
  `/tmp/bb-vuln-target-tls/cert.pem`, the fixture's published certificate).
  An https target without a verifiable CA file is refused (exit 2) rather
  than probed unverified.
- **Timeouts.** Every check is bounded by `--check-timeout` (default 5 s)
  and the whole run by `--run-timeout` (default 120 s). Each check runs its
  whole session — connect, exchange, teardown — under one hard asyncio
  deadline, and every scenario closes with an RST abort so a peer that stops
  reading cannot trap teardown; a check that hits its deadline records a
  TIMEOUT verdict and is cancelled. The run cannot hang. The run budget
  stops later checks from starting; an in-flight check may overshoot it by
  its connect/teardown slack.
- **Connection caps.** At most 4 concurrent and 96 total connections per run
  (`ConnectionBudget`), TLS handshake attempts included; every connection is
  closed in a `finally` block.
- **No real DoS.** H1-ROBUST-011 is slow-send *lite*: 2 connections, one
  bounded hold of at most 5 s, then abort. Nothing in the harness floods,
  loops, or holds more than the per-check deadline.

## Verdicts and severity

`PASS` — the check's mechanical oracle held. `FAIL` — the oracle was
violated. `TIMEOUT` — no answer within the bound (counts as failure for the
exit code). The severity column is the rank a *failure* of that check
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
| H2-BASE-002 | after all h2 abuse a fresh HTTP/2 request → 200 `ok` (server survived) |
| TLS-001 | TLS 1.0 and 1.1 handshake attempts must fail; TLS 1.2 and 1.3 must succeed negotiating ALPN `h2` at that version. **Documented caveat:** on this Python/OpenSSL build the client stack refuses to offer TLS 1.0/1.1 outright (`NO_PROTOCOLS_AVAILABLE`), so those attempts fail before reaching the wire; the fixture additionally pins `minimum_version = TLSv1_2` server-side. A server-side refusal oracle would need a hand-rolled ClientHello (open question, below). |

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
