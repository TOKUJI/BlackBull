import re
from collections import defaultdict
from pathlib import Path
from statistics import mean, variance


PHASE_RE = re.compile(
    r'([^\s=]+)=(\d+)w/(\d+)c'
)

REQUEST_MS_RE = re.compile(
    r'(\d+(?:\.\d+)?)ms'
)


def percentile(values, pct):
    if not values:
        return None

    values = sorted(values)

    if len(values) == 1:
        return values[0]

    idx = (len(values) - 1) * pct
    lo = int(idx)
    hi = min(lo + 1, len(values) - 1)

    if lo == hi:
        return values[lo]

    frac = idx - lo
    return values[lo] * (1 - frac) + values[hi] * frac


def summarize(values):
    if not values:
        return None

    return {
        "count": len(values),
        "mean": mean(values),
        "variance": variance(values) if len(values) > 1 else 0,
        "p50": percentile(values, 0.50),
        "p99": percentile(values, 0.99),
        "min": min(values),
        "max": max(values),
    }


def summarize_phase_trace(log_path: str):

    phase_wall = defaultdict(list)
    phase_cpu = defaultdict(list)

    request_ms_samples = []
    wall_total_samples = []
    cpu_total_samples = []

    with Path(log_path).open(
        "r",
        encoding="utf-8",
        errors="replace",
    ) as f:

        for line in f:

            if "[ACCESS]" not in line:
                continue

            ms_match = REQUEST_MS_RE.search(line)
            if not ms_match:
                continue

            request_ms = float(ms_match.group(1))

            wall_sum = 0
            cpu_sum = 0

            for phase, wall, cpu in PHASE_RE.findall(line):

                wall = int(wall)
                cpu = int(cpu)

                phase_wall[phase].append(wall)
                phase_cpu[phase].append(cpu)

                wall_sum += wall
                cpu_sum += cpu

            request_ms_samples.append(request_ms)
            wall_total_samples.append(wall_sum)
            cpu_total_samples.append(cpu_sum)

    result = {
        "overall": {
            "request_ms": summarize(request_ms_samples),
            "phase_wall_total_us": summarize(wall_total_samples),
            "phase_cpu_total_us": summarize(cpu_total_samples),
        },
        "phases": {},
    }

    for phase in sorted(phase_wall):

        result["phases"][phase] = {
            "wall_us": summarize(phase_wall[phase]),
            "cpu_us": summarize(phase_cpu[phase]),
        }

    return result

if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <log_path>")
        sys.exit(1)

    print(log_path:= sys.argv[1])
    summary = summarize_phase_trace(log_path)
    print(json.dumps(summary, indent=2))
