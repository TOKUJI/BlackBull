#!/usr/bin/env bash
# bench/httparena/ready_check.sh — minimal EC2 correctness smoke, used in place
# of the full validate.sh when the wheel was already validated locally (WSL2,
# bench/httparena/validate_local.sh).
#
# It intentionally checks ONLY the EC2-environment residues that WSL2
# validation cannot see: container starts, ports bind, the uploaded wheel is
# intact, the docker-bench shim env applies, and the TLS / h2c / gRPC
# listeners answer.  The 20-profile contract itself was validated on WSL2 and
# is not re-checked here.
#
# Usage (on the instance, from the repo root):
#   bash bench/httparena/ready_check.sh <framework> [image]
#     framework  harness framework name (default: blackbull)
#     image      container image (default: httparena-<framework>)
#
# Env:
#   HARENA_DIR   harness clone root (default ~/HttpArena)
#   REPO_DIR     BlackBull repo root (default ~/BlackBull) — cert fallback
#   WAIT_SECS    max seconds to wait for :8080 (default 60)
#
# Exit 0 when every probe passes, 1 otherwise.  Always removes its container.
set -euo pipefail

FW="${1:-blackbull}"
IMAGE="${2:-httparena-${FW}}"
HARENA_DIR="${HARENA_DIR:-$HOME/HttpArena}"
REPO_DIR="${REPO_DIR:-$HOME/BlackBull}"
WAIT_SECS="${WAIT_SECS:-60}"
CID="httparena-readycheck-${FW}"

# docker invocation: WSL2 runs it as the user; the EC2 instance runs it via
# sudo (the shim at /usr/bin/docker passes non-run subcommands through, and
# the harness runs everything as root).  Pick whichever actually works.
if docker info >/dev/null 2>&1; then
    DOCKER="docker"
elif sudo -n docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
else
    echo "ERROR: no working docker access (tried 'docker' and 'sudo docker')" >&2
    exit 1
fi

_fail=0
probe() {  # probe <name> <expected-in-output> <cmd...>
    local name="$1" want="$2"; shift 2
    if "$@" 2>/dev/null | grep -q "$want"; then
        echo "  PASS [$name]"
    else
        echo "  FAIL [$name]"
        _fail=1
    fi
}

# --- container start --------------------------------------------------------
# WSL2/Docker-Desktop compat path (patch_wsl2_docker.sh created httparena-net):
# bridge network + published ports + the compat volumes — `--network host`
# does not expose ports to the WSL2 host on Docker Desktop, and bind-mounts
# are mangled.  Otherwise (EC2, working mounts): --network host + harness dirs.
_HAVE_CERTS=0
if docker network inspect httparena-net >/dev/null 2>&1 \
   && docker volume inspect httparena-data >/dev/null 2>&1 \
   && docker volume inspect httparena-certs >/dev/null 2>&1; then
    DOCKER_ARGS=(run -d --name "$CID" --network httparena-net \
                 -p 8080:8080 -p 8081:8081 -p 8082:8082 -p 8443:8443 \
                 -v httparena-data:/data:ro -v httparena-certs:/certs:ro)
    _HAVE_CERTS=1
else
    DATA_DIR="$(find "$HARENA_DIR" -maxdepth 2 -type d -name data 2>/dev/null | head -1 || true)"
    CERTS_DIR="$(find "$HARENA_DIR" -maxdepth 2 -type d -name certs 2>/dev/null | head -1 || true)"
    if [ -z "$CERTS_DIR" ] || [ ! -f "$CERTS_DIR/server.crt" ]; then
        CERT_TMP="$(mktemp -d)"
        cp "$REPO_DIR/tests/cert.pem" "$CERT_TMP/server.crt" 2>/dev/null || true
        cp "$REPO_DIR/tests/key.pem"  "$CERT_TMP/server.key" 2>/dev/null || true
        CERTS_DIR="$CERT_TMP"
    fi
    DOCKER_ARGS=(run -d --name "$CID" --network host)
    [ -n "$DATA_DIR" ] && DOCKER_ARGS+=(-v "$DATA_DIR:/data:ro")
    [ -f "$CERTS_DIR/server.crt" ] && { DOCKER_ARGS+=(-v "$CERTS_DIR:/certs:ro"); _HAVE_CERTS=1; }
fi

echo ">>> starting $IMAGE ..."
$DOCKER rm -f "$CID" >/dev/null 2>&1 || true
$DOCKER "${DOCKER_ARGS[@]}" "$IMAGE" >/dev/null
trap '$DOCKER rm -f "$CID" >/dev/null 2>&1 || true; [ -n "${CERT_TMP:-}" ] && rm -rf "$CERT_TMP"' EXIT

# --- wait for :8080 ---------------------------------------------------------
echo ">>> waiting for :8080 (up to ${WAIT_SECS}s) ..."
for _ in $(seq 1 "$WAIT_SECS"); do
    if curl -sf -o /dev/null http://127.0.0.1:8080/pipeline 2>/dev/null; then
        break
    fi
    sleep 1
done
if ! curl -sf -o /dev/null http://127.0.0.1:8080/pipeline; then
    echo "  FAIL [server up on :8080]"
    echo "=== ready_check FAILED (server did not come up) ==="
    exit 1
fi
echo "  PASS [server up on :8080]"

# --- probes ---------------------------------------------------------------
probe "/pipeline 200"                "ok"      curl -s http://127.0.0.1:8080/pipeline
probe "/baseline11 sum"              "^3$"     curl -s "http://127.0.0.1:8080/baseline11?int=1&int=2"
probe "h2c :8082 (prior-knowledge)"  "ok"      curl -s --http2-prior-knowledge http://127.0.0.1:8082/pipeline
if [ "$_HAVE_CERTS" = 1 ]; then
    probe "TLS h2 :8443"             "ok"      curl -sk --http2 https://127.0.0.1:8443/pipeline
    probe "TLS h1 :8081"             "ok"      curl -sk https://127.0.0.1:8081/pipeline
else
    echo "  SKIP [TLS probes — no certs mounted]"
fi

# WebSocket echo — stdlib-only raw client (masked text frame "ping" → "ping").
if WS_OUT="$(python3 - "$CID" <<'PYEOF' 2>&1
import socket, os, base64, sys
host, port = '127.0.0.1', 8080
s = socket.create_connection((host, port), timeout=10)
key = base64.b64encode(os.urandom(16)).decode()
req = ('GET /ws HTTP/1.1\r\n'
       'Host: %s:%d\r\n'
       'Upgrade: websocket\r\n'
       'Connection: Upgrade\r\n'
       'Sec-WebSocket-Key: %s\r\n'
       'Sec-WebSocket-Version: 13\r\n\r\n') % (host, port, key)
s.sendall(req.encode())
resp = s.recv(4096)
if b' 101 ' not in resp.split(b'\r\n', 1)[0]:
    print('handshake-failed'); sys.exit(1)
payload = b'ping'
mask = b'\x37\xfa\x21\x3d'
frame = bytes([0x81, 0x80 | len(payload)]) + mask + \
        bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
s.sendall(frame)
hdr = s.recv(2)
opcode, ln = hdr[0] & 0x0f, hdr[1] & 0x7f
data = b''
while len(data) < ln:
    data += s.recv(ln - len(data))
print('OK' if (opcode == 1 and data == payload) else 'mismatch')
s.close()
PYEOF
)"; then
    case "$WS_OUT" in
        *OK*) echo "  PASS [ws echo on /ws]" ;;
        *)    echo "  FAIL [ws echo on /ws] ($WS_OUT)"; _fail=1 ;;
    esac
else
    echo "  FAIL [ws echo on /ws] (client error)"
    _fail=1
fi

# gRPC unary — raw frame over h2c: SumRequest{a=1,b=2} → SumReply{result=3}.
# Frame = 0x00 (uncompressed) + BE length + payload (08 01 10 02) →
# response body 00 00 00 00 02 08 03 (payload "08 03" is 2 bytes;
# grpc-status rides the trailers).  The request frame is written to a temp
# file: neither bash $'...' nor $(...) can carry NUL bytes.
GRPC_REQ="$(mktemp)"
printf '\x00\x00\x00\x00\x04\x08\x01\x10\x02' > "$GRPC_REQ"
GRPC_BODY="$(curl -s --http2-prior-knowledge \
    -H 'content-type: application/grpc' -H 'te: trailers' \
    --data-binary @"$GRPC_REQ" \
    http://127.0.0.1:8080/benchmark.BenchmarkService/GetSum \
    | od -An -tx1 | tr -d ' \n')"
rm -f "$GRPC_REQ"
if [ "$GRPC_BODY" = "00000000020803" ]; then
    echo "  PASS [gRPC unary GetSum on :8080]"
else
    echo "  FAIL [gRPC unary GetSum on :8080] (body=$GRPC_BODY)"
    _fail=1
fi

echo
if [ "$_fail" -eq 0 ]; then
    echo "=== ready_check $FW: ALL PROBES PASSED ==="
else
    echo "=== ready_check $FW: FAILED ==="
fi
exit "$_fail"
