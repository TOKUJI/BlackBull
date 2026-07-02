#!/usr/bin/env bash
# Minimal reproduction of the HttpArena gRPC readiness false-positive.
#
# HttpArena's _wait_grpc treats `ghz ... GetSum` EXIT CODE 0 as "server ready".
# But ghz is a load tester: it exits 0 whenever the run completes, even if the
# single RPC failed with Unavailable (connection refused / nothing listening).
# So readiness passes on attempt 1 against a dead port.
#
# Requires: ghz on PATH (or set GHZ=/path/to/ghz), and benchmark.proto alongside.
set -u

GHZ="${GHZ:-ghz}"
PROTO="$(dirname "$0")/benchmark.proto"
DEAD_TARGET="127.0.0.1:59999"   # nothing is listening here

echo "== ghz $("$GHZ" --version 2>&1) =="
echo "Probing $DEAD_TARGET (nothing listening) with the exact _wait_grpc command..."
echo

if "$GHZ" --insecure --proto "$PROTO" \
       --call benchmark.BenchmarkService/GetSum -d '{"a":1,"b":2}' \
       -c 1 -n 1 "$DEAD_TARGET" >/dev/null 2>&1; then
    echo "RESULT: _wait_grpc would print 'gRPC server ready'  <-- FALSE POSITIVE"
    echo "        (exit code 0 even though nothing is listening)"
else
    echo "RESULT: not ready (this is what a correct gate should say)"
fi

echo
echo "-- ghz's own status distribution for the same probe --"
"$GHZ" --insecure --proto "$PROTO" \
    --call benchmark.BenchmarkService/GetSum -d '{"a":1,"b":2}' \
    -c 1 -n 1 "$DEAD_TARGET" 2>&1 | grep -A1 -i 'status code distribution' || true
