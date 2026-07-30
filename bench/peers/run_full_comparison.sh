#!/usr/bin/env bash
# Comprehensive peer benchmark — BlackBull(no-uvloop) vs FastAPI vs Sanic
# Uses bench/peers/run_peer.sh for server lifecycle
set -e

REPO="/home/toshio/work/BlackBull"
PORT=8444
DUR=15
WARMUP=5
THREADS=8
STACKS=("blackbull" "fastapi" "sanic")
ENDPOINTS=("/plaintext" "/json" "/ping" "/echo")
CONCURRENCIES=(64 256 512 1024)
RESULTS="$REPO/bench/results/peer_cmp_$(date +%Y%m%d-%H%M%S).txt"

cd "$REPO"

echo "# Peer Benchmark: BlackBull(no-uvloop) vs FastAPI vs Sanic" | tee "$RESULTS"
echo "# $(date)" | tee -a "$RESULTS"
echo "# wrk -t$THREADS -c{N} -d${DUR}s --timeout 8" | tee -a "$RESULTS"
echo "" | tee -a "$RESULTS"

for stack in "${STACKS[@]}"; do
    echo "" | tee -a "$RESULTS"
    echo "## $stack" | tee -a "$RESULTS"
    
    # Launch server
    pkill -9 -f "run_peer\|benchmarker\|uvicorn.*8444\|sanic.*8444\|blackbull.*8444" 2>/dev/null || true
    sleep 1
    
    if [ "$stack" = "blackbull" ]; then
        BB_UVLOOP=0 bash bench/peers/run_peer.sh blackbull-cleartext $PORT &
    else
        bash bench/peers/run_peer.sh ${stack}-cleartext $PORT &
    fi
    PID=$!
    
    # Wait for ready
    for i in $(seq 1 30); do
        if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$PORT/ping 2>/dev/null | grep -q 200; then
            break
        fi
        sleep 0.5
    done
    echo "  ready (PID=$PID)" | tee -a "$RESULTS"
    
    # Warmup
    wrk -t$THREADS -c64 -d${WARMUP}s --timeout 8 http://127.0.0.1:$PORT/plaintext >/dev/null 2>&1 || true
    
    for ep in "${ENDPOINTS[@]}"; do
        url="http://127.0.0.1:$PORT$ep"
        echo "  $ep" | tee -a "$RESULTS"
        
        for c in "${CONCURRENCIES[@]}"; do
            out=$(wrk -t$THREADS -c$c -d${DUR}s --timeout 8 "$url" 2>&1)
            rps=$(echo "$out" | grep "Requests/sec:" | awk '{print $2}')
            lat=$(echo "$out" | grep -E "^\s+Latency" | awk '{printf "%s/%s/%s", $2, $3, $4}')
            printf "    c=%-5s  rps=%-10s  lat=%s\n" "$c" "$rps" "$lat" | tee -a "$RESULTS"
        done
        
        # oha --disable-keepalive (The Benchmarker method) at c=64
        oha_out=$(oha --no-tui --disable-keepalive --latency-correction -c 64 -z ${DUR}s "$url" 2>&1)
        oha_rps=$(echo "$oha_out" | grep "Requests/sec:" | awk '{print $2}')
        printf "    oha-c=64 rps=%-10s\n" "$oha_rps" | tee -a "$RESULTS"
    done
    
    # Stop
    kill $PID 2>/dev/null || true
    wait $PID 2>/dev/null || true
    sleep 1
done

echo "" | tee -a "$RESULTS"
echo "## Done — results saved to $RESULTS" | tee -a "$RESULTS"
