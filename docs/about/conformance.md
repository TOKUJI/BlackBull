# Conformance

BlackBull is exercised against three published RFC conformance
suites in addition to the in-tree pytest tests under
`tests/conformance/`.

## Coverage summary

| Layer | Suite | Standard | Where it runs |
|---|---|---|---|
| HTTP/1.1 | in-tree `tests/conformance/http1/` | RFC 9110, RFC 9112 | `pytest` |
| HTTP/2 + HPACK | [h2spec](https://github.com/summerwind/h2spec) (external) | RFC 9113, RFC 7541 | local harness under `bench/conformance/` |
| WebSocket | [Autobahn|Testsuite](https://github.com/crossbario/autobahn-testsuite) (external) | RFC 6455, RFC 7692 | local harness (Docker) |
| WebSocket over HTTP/2 | in-tree `tests/conformance/http2/test_rfc8441.py` | RFC 8441 | `pytest` |

The external suites are not yet wired into CI — they're kept as
local harnesses to re-run after any protocol-level change.

## HTTP/1.1 (in-tree pytest)

Covers RFC 9110 (HTTP Semantics) and RFC 9112 (HTTP/1.1 message
framing).  ~250 conformance test functions across the
`tests/conformance/http1/` tree, organised by RFC section:

| File | Covers |
|---|---|
| [`test_rfc9112_body_length.py`](https://github.com/TOKUJI/BlackBull/blob/master/tests/conformance/http1/test_rfc9112_body_length.py) | `Content-Length`, body framing, `HEAD` / `GET` body disagreement (RFC 9110 §9.3) |
| [`test_rfc9112_chunked.py`](https://github.com/TOKUJI/BlackBull/blob/master/tests/conformance/http1/test_rfc9112_chunked.py) | `Transfer-Encoding: chunked` framing, trailers |
| [`test_rfc9112_connection.py`](https://github.com/TOKUJI/BlackBull/blob/master/tests/conformance/http1/test_rfc9112_connection.py) | Keep-alive, `Connection: close`, half-close |
| [`test_rfc9112_pipelining.py`](https://github.com/TOKUJI/BlackBull/blob/master/tests/conformance/http1/test_rfc9112_pipelining.py) | HTTP/1.1 pipelining with and without bodies |
| [`test_rfc9112_smuggling.py`](https://github.com/TOKUJI/BlackBull/blob/master/tests/conformance/http1/test_rfc9112_smuggling.py) | Request smuggling — CL.CL, CL.TE, TE.CL, TE.TE |
| [`test_rfc9112_slowloris.py`](https://github.com/TOKUJI/BlackBull/blob/master/tests/conformance/http1/test_rfc9112_slowloris.py) | Slowloris partial-headers defence (`BB_HEADER_TIMEOUT`) |
| [`test_http1_dispatch.py`](https://github.com/TOKUJI/BlackBull/blob/master/tests/conformance/http1/test_http1_dispatch.py) | ASGI dispatch — 1xx / 204 / 304 body suppression (RFC 9110 §15), auto-headers |

```bash
pytest tests/conformance/http1/ -q
```

Invalid HTTP raises `BadRequestError` at parse time in
[`blackbull/server/parser.py`](https://github.com/TOKUJI/BlackBull/blob/master/blackbull/server/parser.py)
— before the request reaches any application code.  The
smuggling tests above cover the CVE classes that follow from
`Content-Length` / `Transfer-Encoding` disagreement.

## HTTP/2 — `h2spec`

`h2spec` is the de-facto external conformance suite for HTTP/2
and HPACK — ~146 numbered cases covering frame format, stream
state, flow control, error codes, and header-block decoding.

Install (one-time):

```bash
curl -L -sS https://github.com/summerwind/h2spec/releases/download/v2.6.0/h2spec_linux_amd64.tar.gz \
    | tar -xz -C ~/.local/bin h2spec
chmod +x ~/.local/bin/h2spec
```

Run against a locally-running TLS server on `:8443`:

```bash
# Start any BlackBull HTTPS server, then:
bash bench/conformance/h2spec_run.sh                 # full suite (~2-5 min)
bash bench/conformance/h2spec_run.sh hpack           # HPACK section only
bash bench/conformance/h2spec_run.sh http2/6.5       # specific section
```

Output is teed to
`bench/conformance/results/h2spec_<timestamp>.{txt,xml}` (both
gitignored).  The XML is JUnit-format and machine-readable; the
TXT ends with a `N tests, P passed, S skipped, F failed` line
you can grep for the headline number.

In-tree pytest tests under `tests/conformance/http2/` cover
BlackBull-specific behaviour h2spec does not exercise (RFC 8441
Extended CONNECT, CONTINUATION boundary cases, server-response
shapes), and run in normal `pytest` runs.

## WebSocket — Autobahn|Testsuite

The de-facto external conformance suite for WebSocket — ~500
numbered cases over framing, control frames, UTF-8 validation,
close codes, fragmentation, and `permessage-deflate`.

The harness drives the suite from a Docker image against a
plaintext WebSocket echo server.  Docker is required.

```bash
# Terminal 1 — start the echo server BlackBull provides for the test
python bench/conformance/autobahn_app.py --port 9001

# Terminal 2 — run Autobahn against it
bash bench/conformance/autobahn_run.sh               # full fuzzingclient run
CASES='1.*' bash bench/conformance/autobahn_run.sh   # subset (e.g. all of §1.x)
```

Reports land in `bench/conformance/results/autobahn_<timestamp>/`
with an HTML index — open `index.html` in a browser for the
case-by-case breakdown.

## WebSocket over HTTP/2 (RFC 8441)

There is no external h2spec-style harness for RFC 8441 yet.
The in-tree pytest tests under
`tests/conformance/http2/test_rfc8441.py` are the current source
of truth for this surface.  RFC 8441 is also opt-in via
`BB_H2_ENABLE_WEBSOCKET=1` (see
[WebSockets](../guide/websockets.md#transport-http11-upgrade-vs-http2-extended-connect)).

## Filing a non-conformance

If a conformance run regresses (a case that previously passed
starts failing), re-run the latest harness, attach the failing
case's verbatim transcript to the report, and file an issue on
the GitHub repo with:

- the RFC section citation (e.g. RFC 9113 §6.5.2);
- the case ID from h2spec or Autobahn (e.g. `http2/6.5/2`,
  Autobahn `1.1.5`);
- the transcript and any wireshark / `tshark` capture if
  available.

## Fuzz and property-based tests

In addition to the RFC suites, the codebase exercises the parser
and protocol layers with two unstructured-input harnesses.

### atheris coverage-guided fuzz

[`tests/conformance/http1/fuzz/fuzz_http1.py`](https://github.com/TOKUJI/BlackBull/blob/master/tests/conformance/http1/fuzz/fuzz_http1.py)
drives BlackBull's HTTP/1.1 parser with random byte sequences
via [atheris](https://github.com/google/atheris).  The harness
has run 100k+ iterations across corpus seeds without a process
crash.  Targets:

- `blackbull/server/parser.py` — request-line + header parsing
- `blackbull/protocol/` — frame and HPACK decoding

### Differential corpus vs nginx

`tests/conformance/http1/fuzz/user-corpus/` holds 7 captured
input/response pairs where BlackBull and nginx differ on the
same input, each categorised:

| Category | Meaning | Count |
|---|---|---|
| `STATUS_DIFFER` | RFC-defensible divergence (BlackBull is RFC-correct; nginx is permissive) | 2 |
| `BOTH_REJECTED` | Both servers reject the malformed input | 4 |
| `OK` | Both servers respond identically | 1 |

The two `STATUS_DIFFER` entries are documented as known
divergences from nginx behaviour in
[`KNOWN_LIMITATIONS.md`](https://github.com/TOKUJI/BlackBull/blob/master/KNOWN_LIMITATIONS.md).

### Hypothesis property tests

[`tests/properties/`](https://github.com/TOKUJI/BlackBull/tree/master/tests/properties)
uses [hypothesis](https://hypothesis.readthedocs.io/) to
generate structured random inputs for header parsing
([`test_headers.py`](https://github.com/TOKUJI/BlackBull/blob/master/tests/properties/test_headers.py))
and HTTP/2 frame round-tripping
([`test_http2_frame.py`](https://github.com/TOKUJI/BlackBull/blob/master/tests/properties/test_http2_frame.py)),
checking invariants (round-trip equality, header-name
case-insensitivity) across many shapes.

## Other testing surfaces

- [Testing](../guide/testing.md) — how to write tests for your
  own application using BlackBull's clients or
  `httpx.ASGITransport`.
- The differential fuzz corpus above records RFC-defensible
  divergences from nginx; see
  [`KNOWN_LIMITATIONS.md`](https://github.com/TOKUJI/BlackBull/blob/master/KNOWN_LIMITATIONS.md)
  for the documented entries.
