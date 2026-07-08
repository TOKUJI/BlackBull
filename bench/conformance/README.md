# RFC conformance harness

Runs published HTTP/2 / HPACK conformance suites against a BlackBull server.
Currently wires up [h2spec](https://github.com/summerwind/h2spec) (RFC 7540 +
RFC 7541 conformance, ~150 test cases).

## Current status

[![RFC conformance](https://github.com/TOKUJI/BlackBull/actions/workflows/conformance.yml/badge.svg)](https://github.com/TOKUJI/BlackBull/actions/workflows/conformance.yml)

CI-verified on every push and PR to master by
[`.github/workflows/conformance.yml`](../../.github/workflows/conformance.yml)
(plus a weekly scheduled run that picks up upstream tool releases).  A green
badge means every job passed in full:

| CI job | What it verifies |
|---|---|
| `h2spec` | RFC 7540 + RFC 7541 (HTTP/2 + HPACK), full suite, zero failures |
| `autobahn` | RFC 6455 WebSocket (Autobahn\|Testsuite), zero failing cases |
| `corpus-replay` | HTTP/1.1 differential corpus replay (captured divergences) |
| `h2-flow-control` | HTTP/2 large bidirectional payload flow-control gate |
| `grpc-interop` | Real `grpcio` client against the h2c gRPC transport |

The badge — not a number quoted in any doc — is the authoritative
conformance claim; per-job artefacts (JUnit XML, reports) attach to each
workflow run.

The sections below cover running the suites **locally**.  Local first-run
results are stored under `bench/conformance/results/` (gitignored) and are
not published.

## Install h2spec

```bash
# Linux amd64 binary release
curl -L -sS https://github.com/summerwind/h2spec/releases/download/v2.6.0/h2spec_linux_amd64.tar.gz \
    | tar -xz -C ~/.local/bin h2spec
chmod +x ~/.local/bin/h2spec
h2spec --version
```

## Running

Start any BlackBull-served HTTPS+HTTP/2 endpoint on port 8443, then:

```bash
bash bench/conformance/h2spec_run.sh           # full suite (~4 min)
bash bench/conformance/h2spec_run.sh hpack    # HPACK section only
bash bench/conformance/h2spec_run.sh http2/6.5  # specific section
```

Output is teed to `bench/conformance/results/h2spec_<timestamp>.txt` and a
JUnit XML report is written alongside.  Both are gitignored.
