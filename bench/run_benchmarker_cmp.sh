#!/bin/bash
# Run wrk against BlackBull, FastAPI+uvicorn, Sanic with same params as The Benchmarker
# wrk -t8 -c{64,256,512} -d15s --timeout 8
set -e

VENV=/home/toshio/work/BlackBull/.venv/bin/python
PORT=8001

bench_one() {
    local name=$1
    local server_cmd=$2
    echo ""
    echo "============================================"
    echo "  $name"
    echo "============================================"

    # Start server
    $server_cmd &
    local pid=$!
    sleep 3

    # Quick health check
    if ! curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:$PORT/ | grep -q 200; then
        echo "ERROR: $name failed to start"
        kill $pid 2>/dev/null
        return 1
    fi
    echo "  Server OK (PID=$pid)"

    # Bench at 3 concurrency levels
    for c in 64 256 512; do
        echo "  --- Concurrency $c ---"
        wrk -t8 -c$c -d15s --timeout 8 http://127.0.0.1:$PORT/ 2>&1 | grep -E '(Requests/sec|Latency|Transfer/sec)'
    done

    kill $pid 2>/dev/null
    wait $pid 2>/dev/null
    sleep 1
}

# 1. BlackBull
bench_one "BlackBull (built-in server)" \
    "$VENV /home/toshio/work/BlackBull/bench/benchmarker_target.py --port $PORT"

# 2. FastAPI + uvicorn
bench_one "FastAPI + uvicorn" \
    "$VENV -m uvicorn bench.benchmarker_target_fastapi:app --host 127.0.0.1 --port $PORT --log-level error"

# 3. Sanic (built-in server)
bench_one "Sanic (built-in, 1 worker)" \
    "$VENV -c \"from bench.benchmarker_target_sanic import app; app.run(host='127.0.0.1', port=$PORT, debug=False, access_log=False, workers=1)\""
