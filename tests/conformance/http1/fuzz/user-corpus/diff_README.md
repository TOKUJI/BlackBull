# Differential-test corpus (`diff_*.txt`)

Files named `diff_NN_<short>.txt` are minimised reproducers captured by
the Sprint 17 hardening of
[`tests/conformance/http1/test_http1_differential.py`](../../test_http1_differential.py).
Each file holds the raw HTTP/1.1 wire bytes (CRLF line endings) of one
example where BlackBull's response diverged from nginx (the oracle).

Hash-named files in this directory come from the older atheris
fuzz harness ([`fuzz_http1.py`](../fuzz_http1.py)); they are unrelated
to the differential test.

## Catalogue

| File | Wire summary | Divergence | Root cause | Resolution |
|---|---|---|---|---|
| `diff_01_post_root_method_not_allowed.txt` | `POST / HTTP/1.1` with empty body, only `Host` header | nginx returns 200 "ok"; BlackBull returns 405 (only GET allowed by the test fixture's `/` route) | Test-fixture mismatch.  The `h1_app` ASGI app at [`tests/conformance/http1/conftest.py:_make_app`](../../conftest.py) only registers `GET /`; the nginx oracle's `location /` returns 200 for any method. | Phase 3 of Sprint 17 extends `h1_app` to accept any method on `/`. |

## Format

Plain wire bytes, no comments, no envelope.  `cat -A` shows `^M$` at line
ends.  These are designed to be sent verbatim by `send_raw()`
(conftest.py) or by Phase 2's new `HTTP1Client.send_raw()` primitive.

## Adding a new file

Phase 8 will add automatic capture: any non-`OK`, non-`BOTH_REJECTED`
example minimised by Hypothesis lands here as
`diff_<TIMESTAMP>_<short>.txt` with one row appended to the catalogue.
Phase 1 files (this one) are hand-written.
