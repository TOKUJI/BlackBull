# BlackBull on HttpArena — local-only prep

Sprint 27 Task 4 scaffold for cross-checking BlackBull numbers against
the [HttpArena](https://www.http-arena.com/) benchmark harness.
**Not yet a leaderboard submission** — `meta.json` keeps
`"enabled": false`; the directory is here so future sprints can lift
the contents into a `frameworks/blackbull/` PR against
[`MDA2AV/HttpArena`](https://github.com/MDA2AV/HttpArena) without
re-deriving the integration.

## Layout

```
bench/httparena/
  Dockerfile         multi-stage-free image; build context = repo root
  .dockerignore      keeps the image small
  app.py             BlackBull entrypoint with the HttpArena endpoint contract
  launcher.py        spawns cleartext (:8080) + TLS (:8081) processes
  meta.json          HttpArena framework metadata
  README.md          this file
```

## Profiles implemented

The endpoint contract in `app.py` covers the H1 / H2 / WebSocket
profiles BlackBull's runtime supports today:

| Profile         | Endpoint               | Status |
|-----------------|------------------------|--------|
| baseline        | GET/POST `/baseline11` | ✓ |
| pipelined       | GET/POST `/baseline11` | ✓ (same endpoint, pipeline-driven workload) |
| limited-conn    | GET/POST `/baseline11` | ✓ |
| json            | GET `/json/{count}`    | ✓ |
| json-tls        | GET `/json/{count}` on :8081 | ✓ |
| upload          | POST `/upload`         | ✓ |
| baseline-h2     | GET/POST `/baseline11` on :8081 (TLS, ALPN h2) | ✓ |
| baseline-h2c    | GET `/baseline11`  on :8080 (h2 prior-knowledge) | ✓ |
| json-h2c        | GET `/json/{count}` on :8080 (h2 prior-knowledge) | ✓ |
| echo-ws         | GET `/ws` upgrade      | ✓ (re-exported via the standard BlackBull WS handler — TODO mount at `/ws` here) |

Profiles **not** implemented (intentional carve-out):

| Profile | Why not |
|---|---|
| `static-h3` | HTTP/3 / QUIC transport is intentionally out-of-scope per the project roadmap. |
| `async-db`, `crud`, `fortunes`, `api-4`, `api-16` | Out of scope.  BlackBull is a protocol-layer framework, not a full-stack one — Postgres-backed implementations belong in a separate submission if we ever pursue the leaderboard (see Flask's HttpArena entry for the pattern). |
| `*-h3`           | HTTP/3 / QUIC is out-of-scope. |
| `*-grpc`         | No gRPC. |
| `gateway-*`, `production-stack` | Need sidecar + DB work first; same scope rationale as the DB profiles. |

## Local smoke test

The `_local/` subdir holds throwaway mounts that mirror what
HttpArena's harness provides; create it on first use.

```bash
mkdir -p bench/httparena/_local/data bench/httparena/_local/certs

# Dataset — pull HttpArena's authoritative 50-item file or use our own.
curl -fL -o bench/httparena/_local/data/dataset.json \
  https://raw.githubusercontent.com/MDA2AV/HttpArena/master/data/dataset.json

# Reuse the project's mkcert dev cert as the TLS pair.
cp tests/cert.pem bench/httparena/_local/certs/server.crt
cp tests/key.pem  bench/httparena/_local/certs/server.key

# Build (context = repo root).
docker build -f bench/httparena/Dockerfile -t blackbull-httparena .

# Run with the documented mounts.
docker run --rm --network host \
  -v $PWD/bench/httparena/_local/data:/data:ro \
  -v $PWD/bench/httparena/_local/certs:/certs:ro \
  --name blackbull-httparena \
  blackbull-httparena

# In another terminal:
curl -s "http://localhost:8080/pipeline"                  # → ok
curl -s "http://localhost:8080/baseline11?a=1&b=2&c=3"     # → 6
curl -s "http://localhost:8080/json/3?m=2.0" | python3 -m json.tool
curl -sk "https://localhost:8081/baseline11?a=10"          # → 10
```

`_local/` is gitignored — local sandbox only.

## End-to-end HttpArena run

To actually drive the validation + benchmark scripts:

```bash
# One-time: clone HttpArena somewhere outside the BlackBull repo.
git clone https://github.com/MDA2AV/HttpArena.git ~/work/HttpArena

# Vendor this directory under HttpArena's frameworks/ tree:
mkdir -p ~/work/HttpArena/frameworks/blackbull
cp -r bench/httparena/* ~/work/HttpArena/frameworks/blackbull/

# Flip enabled=true in the vendored meta.json, then:
cd ~/work/HttpArena
./scripts/validate.sh blackbull           # 18-point correctness check
./scripts/benchmark.sh blackbull baseline # one profile
```

A future sprint will automate the vendoring step
(`bench/httparena/sync.sh`) once we commit to a leaderboard PR.

## Versioning + apples-to-apples

The container build pulls `pyproject.toml` from the repo root, so the
image's `blackbull.__version__` matches whatever sprint commit was
checked out at `docker build` time.  Record the commit SHA in
`bench/results/httparena/<scenario>-<ts>/` alongside any captured
numbers.

`BB_ACCESS_LOG=0` is set by `app.py` to match the peer benchmark
convention documented in
[.claude/patterns/cautions.md](../../.claude/patterns/cautions.md)
(Sprint 9 lesson — every peer disables access logging during
benchmarks).
