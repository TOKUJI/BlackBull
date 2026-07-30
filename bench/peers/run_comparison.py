#!/usr/bin/env python3
"""Comprehensive peer benchmark: BlackBull (no uvloop) vs FastAPI vs Sanic.

Uses bench/peers/run_peer.sh to launch each stack, then runs wrk + oha
against all endpoints at multiple concurrency levels.  Cleartext HTTP/1.1
only (no TLS) — fast and apples-to-apples.

Usage:
    BB_UVLOOP=0 python bench/peers/run_comparison.py
"""
import subprocess
import sys
import time
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
RUN_PEER = REPO / "bench" / "peers" / "run_peer.sh"
PORT = 8444  # Avoid conflict with default 8443

STACKS = ["blackbull", "fastapi", "sanic"]
ENDPOINTS = {
    "/ping":      ("GET",  "text/html"),
    "/plaintext": ("GET",  "text/plain"),
    "/json":      ("GET",  "application/json"),
    "/echo":      ("POST", "application/octet-stream"),
    "/1kb":       ("GET",  "text/html"),
    "/16kb":      ("GET",  "text/html"),
}
CONCURRENCIES = [64, 256, 512, 1024]
DURATION = 15
WARMUP = 5
WRK_THREADS = 8


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO, **kwargs)


def launch(stack, cleartext=True):
    """Launch a stack via run_peer.sh. Returns the Popen process."""
    variant = "cleartext" if cleartext else "tls"
    stack_name = f"{stack}-{variant}" if cleartext else stack
    env = os.environ.copy()
    if stack == "blackbull":
        env["BB_UVLOOP"] = "0"
    proc = subprocess.Popen(
        ["bash", str(RUN_PEER), stack_name, str(PORT)],
        env=env, cwd=REPO,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


def wait_ready(url, timeout=30):
    """Wait until the server responds 200."""
    import urllib.request
    import urllib.error
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            resp = urllib.request.urlopen(req, timeout=2)
            if resp.status == 200:
                return True
        except (urllib.error.URLError, OSError, ConnectionRefusedError):
            pass
        time.sleep(0.5)
    return False


def run_wrk(url, concurrency, duration=DURATION):
    """Run wrk and return (req_s, avg_lat_ms, max_lat_ms)."""
    cmd = [
        "wrk", f"-t{WRK_THREADS}", f"-c{concurrency}",
        f"-d{duration}s", "--timeout", "8", url
    ]
    result = run(cmd, timeout=duration + 30)
    out = result.stdout
    req_s = avg_lat = max_lat = None
    for line in out.splitlines():
        if "Requests/sec:" in line:
            try:
                req_s = float(line.strip().split()[1])
            except (ValueError, IndexError):
                pass
        if "Latency" in line and "Avg" not in line:
            parts = line.strip().split()
            try:
                avg_idx = parts.index("Avg") if "Avg" in parts else None
                if avg_idx is None:
                    # Format: Latency  avg  stdev  max  +/-stdev
                    avg_lat = float(parts[1]) if len(parts) > 1 else None
                    # Find max
                    for i, p in enumerate(parts):
                        if p == "Max" or (p.replace('.', '').isdigit() and i > 1):
                            continue
                    # Simpler: just parse known format
                    if len(parts) >= 4:
                        avg_lat = float(parts[1])
                        # max is parts[3] but might have unit
                        max_lat = float(parts[3].rstrip('usms'))
            except (ValueError, IndexError):
                pass
    return req_s, avg_lat, max_lat


def run_oha(url, concurrency, duration=DURATION):
    """Run oha with --disable-keepalive (matching The Benchmarker)."""
    cmd = [
        "oha", "--no-tui", "--disable-keepalive", "--latency-correction",
        "-c", str(concurrency), "-z", f"{duration}s", url
    ]
    result = run(cmd, timeout=duration + 30)
    out = result.stdout
    req_s = None
    for line in out.splitlines():
        if "Requests/sec:" in line:
            try:
                req_s = float(line.strip().split()[1])
            except (ValueError, IndexError):
                pass
    return req_s


def bench_stack(stack_name):
    """Benchmark one stack across all endpoints and concurrencies."""
    print(f"\n{'='*60}")
    print(f"  {stack_name}")
    print(f"{'='*60}")

    # Launch
    proc = launch(stack_name, cleartext=True)
    base_url = f"http://127.0.0.1:{PORT}"

    if not wait_ready(f"{base_url}/ping"):
        print(f"  ERROR: {stack_name} failed to start")
        proc.kill()
        proc.wait()
        return None

    print(f"  Server ready")

    # Warmup
    run(["wrk", f"-t{WRK_THREADS}", "-c64", f"-d{WARMUP}s", "--timeout", "8",
         f"{base_url}/plaintext"], timeout=WARMUP + 10)

    results = {}
    for endpoint, (method, _ct) in ENDPOINTS.items():
        url = f"{base_url}{endpoint}"
        results[endpoint] = {"wrk": {}, "oha": {}}

        for c in CONCURRENCIES:
            rps, avg_lat, max_lat = run_wrk(url, c)
            results[endpoint]["wrk"][c] = {
                "rps": rps,
                "avg_lat_ms": avg_lat,
                "max_lat_ms": max_lat,
            }

        # oha at one concurrency (64) for The Benchmarker comparison
        oha_rps = run_oha(url, 64)
        results[endpoint]["oha"] = {"c64_rps": oha_rps}

    # Stop
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    time.sleep(1)

    return results


def print_summary(all_results):
    """Print side-by-side comparison tables."""
    print("\n\n" + "=" * 80)
    print("  SUMMARY — wrk throughput (req/s)")
    print("=" * 80)

    for endpoint in ENDPOINTS:
        print(f"\n--- {endpoint} ---")
        header = f"{'Stack':>12s}"
        for c in CONCURRENCIES:
            header += f" {'c=' + str(c):>10s}"
        print(header)
        print("-" * len(header))

        for stack in STACKS:
            if stack not in all_results or all_results[stack] is None:
                continue
            row = f"{stack:>12s}"
            for c in CONCURRENCIES:
                rps = all_results[stack].get(endpoint, {}).get("wrk", {}).get(c, {}).get("rps")
                if rps:
                    row += f" {rps:>10,.0f}"
                else:
                    row += f" {'—':>10s}"
            print(row)

        # Ratios vs BlackBull
        if "blackbull" in all_results and all_results["blackbull"] is not None:
            print()
            for stack in ["fastapi", "sanic"]:
                if stack not in all_results or all_results[stack] is None:
                    continue
                row = f"{'  ' + stack + '/BB':>12s}"
                for c in CONCURRENCIES:
                    bb_rps = all_results["blackbull"].get(endpoint, {}).get("wrk", {}).get(c, {}).get("rps")
                    fa_rps = all_results[stack].get(endpoint, {}).get("wrk", {}).get(c, {}).get("rps")
                    if bb_rps and fa_rps:
                        row += f" {fa_rps/bb_rps:>10.2f}x"
                    else:
                        row += f" {'—':>10s}"
                print(row)

    # oha comparison (matching The Benchmarker methodology)
    print(f"\n\n--- oha --disable-keepalive c=64 (The Benchmarker method) ---")
    print(f"{'Stack':>12s} {'/ping':>10s} {'/plaintext':>12s} {'/json':>10s} {'/echo':>10s}")
    for stack in STACKS:
        if stack not in all_results or all_results[stack] is None:
            continue
        row = f"{stack:>12s}"
        for ep in ["/ping", "/plaintext", "/json", "/echo"]:
            rps = all_results[stack].get(ep, {}).get("oha", {}).get("c64_rps")
            if rps:
                row += f" {rps:>10,.0f}"
            else:
                row += f" {'—':>10s}"
        print(row)


if __name__ == "__main__":
    all_results = {}
    for stack in STACKS:
        all_results[stack] = bench_stack(stack)

    print_summary(all_results)
