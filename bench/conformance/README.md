# RFC conformance harness

Runs published HTTP/2 / HPACK conformance suites against a BlackBull server.
Currently wires up [h2spec](https://github.com/summerwind/h2spec) (RFC 7540 +
RFC 7541 conformance, ~150 test cases).

**This is a work in progress.**  Coverage is limited to h2spec for now; we
have not yet wired up Autobahn|Testsuite for WebSocket (RFC 6455) or any
HTTP/1.1 conformance suite.  First-run results are stored locally for
reference and are not published.

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
