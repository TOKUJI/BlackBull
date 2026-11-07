import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.version_info < (3, 12), reason='sys.monitoring needs Python 3.12+')
@pytest.mark.no_audit(reason='The benchmark measures untraced timings and uses its own monitoring.')
def test_compression_instrument_records_controls_and_source_proof(tmp_path):
    environment = {k: v for k, v in os.environ.items() if k != 'BENCH_RUN_DIR'}
    done = subprocess.run([
        sys.executable, 'bench/micro/middleware_stack.py', '--compression',
        '--base', str(ROOT), '--head', str(ROOT), '--rounds', '1', '--aa-rounds', '1',
        '--count', '2', '--gc', 'on', '--cpu', str(min(os.sched_getaffinity(0))),
    ], cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**environment, 'BENCH_RESULTS_ROOT': str(tmp_path)})
    assert done.returncode == 0, done.stderr
    run, = (tmp_path / 'middleware_stack').iterdir()
    rows = json.loads((run / 'samples.json').read_text())
    assert len(rows) == 8
    assert {s['phase'] for s in rows} == {'null', 'real'}
    assert all(s['source'] == str(ROOT / 'blackbull/middleware/compression.py') for s in rows)
    assert all(set(s['metrics']) == {
        'gzip-complete', 'gzip-split', 'identity', 'binary', 'preencoded',
        'range206', 'range206-no-header', 'range200', 'grpc-trailers',
    } for s in rows)
    assert all(v['cpu_us'] > 0 and v['wall_us'] > 0 for s in rows for v in s['metrics'].values())
    instructions = json.loads((run / 'instructions.json').read_text())
    assert instructions['base']['sha256'] == instructions['treat']['sha256']
    assert instructions['base']['metrics']['identity']['instructions']
    assert (run / 'COMPLETE').exists()
