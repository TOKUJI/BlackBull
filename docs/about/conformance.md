# Conformance

Passing tests establish coverage of their cases, not complete protocol
conformance or absence of defects.

## Coverage summary

| Surface | Check | Scope |
|---|---|---|
| HTTP/1.1 | tests/conformance/http1/ | RFC 9110/9112 framing, dispatch and resource refusals |
| HTTP/2 + HPACK | h2spec plus tests/conformance/http2/ | h2spec's RFC 7540/7541 cases; in-tree RFC 9113 behavior |
| WebSocket | Autobahn plus in-tree tests | RFC 6455/7692 framing, compression and limits |
| WebSocket over HTTP/2 | tests/conformance/http2/test_rfc8441.py | Opt-in RFC 8441 transport |
| Client validation | In-tree client tests | Focused malformed-response and error-scope checks; server h2spec results do not validate clients |

[The conformance workflow](https://github.com/TOKUJI/BlackBull/blob/master/.github/workflows/conformance.yml)
owns external-suite versions, case partitions, budgets and artifact retention.
It runs on pushes, PRs and its schedule. Check the workflow result together
with its artifacts; a partial report is not a passing run.

## In-tree tests

```bash
pytest tests/conformance/ -q
```

The HTTP/1.1 corpus replay needs no Docker and is included above. The live
nginx differential test requires Docker/testcontainers and skips when they
are unavailable. For a changed recorded status, decide whether it is a
regression before regenerating the corpus under Docker; do not update the
expectation merely to make a test pass.

## HTTP/2 — h2spec

Install the version pinned by the workflow and start a BlackBull TLS server
on port 8443. Then run:

```bash
bash bench/conformance/h2spec_run.sh
bash bench/conformance/h2spec_run.sh hpack
bash bench/conformance/h2spec_run.sh http2/6.5
```

Results are saved as bench/conformance/results/h2spec_<timestamp>.txt and
.xml. Read the final test/failure counts and process status; XML is JUnit.
RFC 8441, response validation and BlackBull resource-policy cases also need
the in-tree tests.

## WebSocket — Autobahn

Docker is required. In separate terminals:

```bash
python bench/conformance/autobahn_app.py --port 9001
bash bench/conformance/autobahn_run.sh
```

Use `CASES='1.*'` for a subset. Full limits cases need messages up to 16 MiB;
if you lower frame/message caps, record that configuration when interpreting
those refusals.

Reports are under bench/conformance/results/autobahn_<timestamp>.<unique>/.
Keep index.json/index.html, tester.log, exit-code.txt and container-state.json
when present. Missing reports, timeout, OOM and protocol failure are distinct
outcomes. A successful report from a failed process is not a pass; a failed
container inspection/removal also fails the harness. The workflow and
bench/conformance/autobahn_run.sh own retry and cleanup rules.

## Fuzzing and failure reports

The HTTP/1.1 atheris entry point is
[tests/conformance/http1/fuzz/fuzz_http1.py](https://github.com/TOKUJI/BlackBull/blob/master/tests/conformance/http1/fuzz/fuzz_http1.py).
Property tests under tests/properties/ exercise structured random inputs.
The curated nginx divergence corpus records intentional validation-policy
choices; nginx disagreement alone does not establish a defect.

For a failure, retain the exact build and configuration, failing case ID,
transcript, process status and available packet capture. Cite the violated
requirement when filing an issue. See [Testing](../guide/testing.md) for
application tests and [Security model](security-model.md) for configured bounds.
