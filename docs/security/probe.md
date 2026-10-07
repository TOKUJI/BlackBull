# Local robustness probe (`just vuln-check`)

`just vuln-check [base_url="http://127.0.0.1:8000"]` runs
`tools/security/probe.py` against an **already-running** local server —
normally the [minimal fixture app](fixture-app.md) started with
`just vuln-target-up` — and prints a results table (check id, severity,
verdict, detail). The same run writes a markdown report to
`bench/results/security/<UTC-timestamp>.md` (created on demand). Exit code:
`0` all PASS, `1` any FAIL/TIMEOUT, `2` the safety gate refused the target.
This harness is deliberately **not** part of the normal `pytest` run
(`pytest.ini` keeps `tools/` out of collection); the checks need a live
server.

## Safety gates (enforced in code, not just here)

- **Loopback allow-list.** `parse_target()` refuses any host that is not
  exactly `127.0.0.1`, `::1`, or `localhost` (case-insensitive), and refuses
  non-`http` schemes and URLs with userinfo — before any socket is opened.
  Hostnames are matched as strings; DNS is never consulted. A refusal exits
  with code 2.
- **Timeouts.** Every check is bounded by `--check-timeout` (default 5 s)
  and the whole run by `--run-timeout` (default 120 s). Each check runs its
  whole session — connect, exchange, teardown — under one hard asyncio
  deadline, and every scenario closes with an RST abort so a peer that stops
  reading cannot trap teardown; a check that hits its deadline records a
  TIMEOUT verdict and is cancelled. The run cannot hang. The run budget
  stops later checks from starting; an in-flight check may overshoot it by
  its connect/teardown slack.
- **Connection caps.** At most 4 concurrent and 32 total connections per run
  (`ConnectionBudget`); every connection is closed in a `finally` block.

## Verdicts and severity

`PASS` — the check's mechanical oracle held. `FAIL` — the oracle was
violated. `TIMEOUT` — no answer within the bound (counts as failure for the
exit code). The severity column is the rank a *failure* of that check
carries, per [severity criteria](severity.md); a hang or crash escalates the
H1-ROBUST checks to High, as that document's M1 defaults table prescribes.

## Checks and oracles

| Check | Oracle (mechanical) |
|---|---|
| BASELINE-001/003 | `GET /` → 200 and body exactly `ok` (003 runs after all abuse) |
| BASELINE-002 | `GET /json` → 200 and body is exactly `{"ok": true}` JSON |
| H1-ROBUST-001 | request line `FOO / HTTP/1.1` → 4xx or 501/505 or connection close; 2xx/3xx and other 5xx = FAIL |
| H1-ROBUST-002 | 100 KiB header value → 4xx (431/400) or close; 5xx = FAIL |
| H1-ROBUST-003 | garbage request-line bytes → 400 or close |
| H1-ROBUST-004 | CRLF in a header value (a literal `value\r\nX-Injected: 1`, and a bare-CR variant) → reject/close, or the value is kept opaque/sanitized with no injected response header and no CR/LF in the echo (RFC 9110 §5.5); a bare CR that splits into a second field line = FAIL |
| H1-ROBUST-005 | `Content-Length: 100` with 5 body bytes then FIN → 4xx/408 or close |
| SMUGGLE-001 | `Content-Length` and `Transfer-Encoding: chunked` together → 400 or close (RFC 9112 §6.3); any other status = FAIL |
| STATIC-001 | `GET /static/../fixture_app.py` and `/static/%2e%2e/fixture_app.py` → 400/403/404; any 200 = FAIL |
| HDR-001 | the `server:` response header of `GET /`, if present, must not match: a POSIX path under `/home`, `/users`, `/usr`, `/var`, `/etc`, `/opt`, `/tmp`, `/root`, `/srv`, `/app`, `/workspace`; a Windows drive path; `site-packages`/`dist-packages`; or `python`, `cpython`, `py/<digit>` |

## Implementation note

The wire-level checks are driven by `blackbull.fault_injection`'s HTTP/1.1
client scenario machinery (`scenario_h1` steps — `SendRawBytes`, `HalfClose`,
`ReadResponse` — executed by `HTTP1Client.execute_scenario`) against the
external running server. That machinery demonstrably drives servers other
than BlackBull (`tests/unit/test_fault_h1_client_third_party.py` runs the
same executor against CPython's `http.server`), so no in-process coupling is
involved. `SMUGGLE-001` reuses the catalogued scenario
`blackbull.fault_injection.catalogue.h1_client.content_length_and_transfer_encoding`
verbatim, with its read step retimed to `--check-timeout`. The baseline,
static, and header checks use the same executor with plain well-formed raw
requests so one `asyncio.wait_for` bounds every check identically.

## Recording findings

- Run results: comment on YouTrack issue BLA-526 (`just yt-comment`).
- A confirmed defect: its own issue, with repro steps and the severity rank
  from [severity criteria](severity.md).
- Fixes: land as PRs plus a fast deterministic regression test added to the
  normal `pytest` suite.

## Next

- [Minimal fixture app](fixture-app.md) — the probe target definition.
- [Severity criteria](severity.md) — how findings are ranked.
