# Minimal fixture app (最小フィクスチャアプリ)

The minimal fixture app (`tools/security/fixture_app.py`) is the deterministic
probe target for BLA-526 local robustness research. It is not an example of
application architecture and not part of the framework: its only job is to
give the probes (`just vuln-check`) a request-handling surface whose expected
behavior is completely known, so any deviation a probe reports can only come
from the wire handling, never from application logic.

## Invariants

- **Stateless.** No handler keeps or reads state between requests.
- **Deterministic responses.** The same request produces the same status,
  body, and content type. (Transport-level framing headers such as `date`
  are the framework's, not the app's.)
- **No dependencies beyond blackbull.** Standard library only.
- **No disk writes and no network egress.** Static files are read-only
  inputs.
- **Loopback only.** Started as below it binds `127.0.0.1:8000` and nothing
  else — `Listener(Tcp(8000, host='127.0.0.1'))`, no wildcard interface.
- **Stable error surface.** The 404 and 500 handlers render fixed JSON
  bodies, so probe oracles against error responses are stable.

## Route table

| Route | Response | Probe surface |
|---|---|---|
| `GET /` | `200`, `text/plain`, body `ok` | smallest healthy response: BASELINE-001/003, HDR-001 |
| `GET /json` | `200`, `application/json`, `{"ok": true}` | JSON serialization: BASELINE-002 |
| `GET /echo-headers` | `200`, JSON `{"headers": [[name, value], ...]}` (wire order) | request-header parsing: H1-ROBUST-004 |
| `POST /echo-body` | `200`, `application/octet-stream`, request body echoed | request-body framing: H1-ROBUST-005 |
| `GET /square/{n:int}` | `200`, `{"n": n, "square": n*n}` | path parameters and type coercion |
| `QUERY /search` | `200`, `{"echo": "<request body>"}` | QUERY-method surface with a request body (RFC 10008) |
| `GET`/`HEAD` `/static/<path>` | file under `tools/security/static/` | path traversal: STATIC-001 |
| 404 handler | `404`, `{"error": "not found"}` | stable not-found oracle |
| 500 handler | `500`, `{"error": "internal server error"}` | stable server-error oracle |

`tools/security/static/hello.txt` (one line) is the only static file; it is
the "inside the root" reference the traversal check contrasts against.

## Running it

```console
$ just vuln-target-up
vuln-target up (pid 12345): http://127.0.0.1:8000
```

The recipe starts the app in the background on `127.0.0.1:8000`, records the
server PID in `/tmp/bb-vuln-target.pid` and its output in
`/tmp/bb-vuln-target.log`, and waits (bounded, ~10 s) for the port to accept
connections. If the target is already running the recipe says so and does
nothing. `just vuln-target-down` stops it via the PID file and verifies the
port is free. The app always binds loopback; the port is the only knob
(`--port`, used by the recipe).

## Next

- [Local robustness probe](probe.md) — what runs against this target.
- [Severity criteria](severity.md) — how probe findings are ranked.
