"""compare_table.py's per-run table reads every run, whichever load tool HttpArena used."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# One run's output per tool, as HttpArena's benchmark.sh logs it.
RUN_OUTPUT = {
    'gcannon': '  Throughput: {k:.2f}K req/s\n',
    'h2load': 'finished in 5.05s, {v:.2f} req/s, 3.35MB/s\n'
              'req/s       :     401.47     1380.50      527.97     960.09\n',
    'wrk': 'Requests/sec: {v:.2f}\nTransfer/sec:    429.68MB\n',
    'zrk': '{{\n  "requests": 6016107,\n  "achieved_rate": {v:.2f},\n  "target_rate": 1000000,\n}}\n',
}
RUNS = [100000.0, 110000.0, 120000.0]


def _section(fw: str, profile: str, conns: int, tool: str) -> str:
    out = f'==============\n=== {fw} / {profile} / {conns}c (tool={tool}) ===\n==============\n'
    for i, v in enumerate(RUNS, 1):
        out += f'[run {i}/3]\n' + RUN_OUTPUT[tool].format(v=v, k=v / 1e3)
    return out + f'=== Best: {int(max(RUNS))} req/s (CPU: 100%, Mem: 1GiB) ===\n'


def test_every_tool_contributes_every_run(tmp_path):
    fw = 'blackbull'
    logs = tmp_path / 'logs'
    logs.mkdir()
    for tool in RUN_OUTPUT:
        profile = f'p-{tool}'
        result = tmp_path / 'httparena-tree/results' / profile / '512'
        result.mkdir(parents=True)
        (result / f'{fw}.json').write_text(json.dumps({'rps': int(max(RUNS))}))
        (logs / f'benchmark-{fw}-{profile}.log').write_text(_section(fw, profile, 512, tool))

    done = subprocess.run([sys.executable, str(ROOT / 'bench/httparena/compare_table.py'), str(tmp_path)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr

    rows = {m[1]: (int(m[2]), m[3], m[4]) for m in re.finditer(
        r'^\| (p-\w+)/512 \| blackbull \| (\d+) \| ([\d,]+) \| ([\d,]+) \|',
        (tmp_path / 'COMPARISON.md').read_text(), re.M)}
    assert rows == {f'p-{tool}': (3, '110,000', '120,000') for tool in RUN_OUTPUT}
