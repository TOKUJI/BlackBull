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
- **No committed key material.** The TLS certificate is generated at startup
  (see below); only the public part is published to a scratch directory.
- **No disk writes and no network egress.** Static files are read-only
  inputs.
- **Loopback only.** Started as below it binds `127.0.0.1:8000` and
  `127.0.0.1:8443` and nothing else — `Tcp(..., host='127.0.0.1')`, no
  wildcard interface.
- **Stable error surface.** The 404 and 500 handlers render fixed JSON
  bodies, so probe oracles against error responses are stable.

## Topology

One process serves both probe lanes through two `Listener`s on the same
`BlackBull` app (the route table below is identical on both):

```
                    ┌──────────────────────────────────────────────┐
  127.0.0.1:8000 ──▶│ Listener(Tcp(8000, host='127.0.0.1'))        │
  (probe h1 lane)   │   HTTP/1.1 (also h2c preface detection)      │
                    │                                              │
  127.0.0.1:8443 ──▶│ Listener(Tcp(8443, host='127.0.0.1'), tls=…) │──▶ same app
  (probe h2 lane)   │   TLS 1.2+ , ALPN 'h2' first, http/1.1 alt.  │
                    └──────────────────────────────────────────────┘
```

Two listeners in one process is the same shape as BlackBull's own
cleartext-plus-TLS deployments; "HTTP/1.1 versus h2c is preface detection, and
TLS h1 versus h2 is ALPN, both already handled downstream"
(`docs/about/architecture.md`).

### TLS certificate

`make_tls_context()` reuses `blackbull.fault_injection._tls` — one
implementation, not a second — to generate an ephemeral RSA self-signed
certificate at startup. The key material lives in the helper's own tempdir and
is removed when the context is collected; it is never written into the
repository or the shared scratch directory. The **public** certificate is
copied to `/tmp/bb-vuln-target-tls/cert.pem`, the well-known path the probe
(`tools/security/probe.py`, `--tls-ca`) loads to verify the TLS lane's
handshake — so no step of the probe disables certificate verification. The
fixture pins `minimum_version = TLSv1_2`; ALPN advertises `h2` then
`http/1.1`.

## Route table

| Route | Response | Probe surface |
|---|---|---|
| `GET /` | `200`, `text/plain`, body `ok` | smallest healthy response: BASELINE-001/003, HDR-001, H2-BASE-001/002, STATE-001 follow-up |
| `GET /json` | `200`, `application/json`, `{"ok": true}` | JSON serialization: BASELINE-002 |
| `GET /echo-headers` | `200`, JSON `{"headers": [[name, value], ...]}` (wire order) | request-header parsing: H1-ROBUST-004/006/007 |
| `POST /echo-body` | `200`, `application/octet-stream`, request body echoed | request-body framing: H1-ROBUST-005, SMUGGLE-*, CHUNK-* |
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
vuln-target up (pid 12345): http://127.0.0.1:8000 + https://127.0.0.1:8443
```

The recipe starts the app in the background on both ports, records the server
PID in `/tmp/bb-vuln-target.pid` and its output in `/tmp/bb-vuln-target.log`,
and waits (bounded, ~10 s) for the fixture's own `--health` oracle to pass:
`GET /` must answer exactly `ok` on the cleartext lane **and** a verified-TLS
HTTP/2 `GET /` must answer `200 ok` on the TLS lane. If the target is already
running the recipe says so and does nothing; if either port is held by a
foreign process it refuses to start. `just vuln-target-down` stops it via the
PID file (PID-reuse guard intact), removes the generated certificate, and
verifies both ports are free. The ports are the only knobs (`--port`,
`--tls-port`, used by the recipe).

## Next

- [Local robustness probe](probe.md) — what runs against this target.
- [Severity criteria](severity.md) — how probe findings are ranked.
