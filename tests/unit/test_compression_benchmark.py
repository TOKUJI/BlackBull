import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.version_info < (3, 12), reason='sys.monitoring needs Python 3.12+')
@pytest.mark.no_audit(reason='The benchmark measures untraced timings and uses its own monitoring.')
@pytest.mark.parametrize('profile,module,cases,selected', [
    ('--compression', 'middleware/compression.py', {
        'gzip-complete', 'gzip-split', 'identity', 'binary', 'preencoded',
        'range206', 'range206-no-header', 'range200', 'grpc-trailers',
    }, ()),
    ('--responses', 'response.py', {
        'response-default', 'response-extra', 'response-explicit', 'response-many',
        'response-strings', 'response-many-strings', 'response-explicit-strings',
        'json-default', 'json-extra', 'json-explicit', 'redirect-default',
        'redirect-extra', 'redirect-explicit', 'stream-default', 'stream-extra',
        'stream-explicit', 'sse-default', 'sse-extra', 'sse-explicit', 'sse-many',
        'response-serialize',
    }, ()),
    ('--compression-init', 'middleware/compression.py', {
        'compression-default', 'compression-partial', 'compression-explicit',
        'compression-legacy', 'compression-settings-cold', 'settings-cold',
    }, ()),
    ('--compression', 'middleware/compression.py', {'binary', 'range206'}, ('binary', 'range206')),
])
def test_middleware_instrument_records_controls_and_source_proof(tmp_path, profile, module, cases, selected):
    environment = {k: v for k, v in os.environ.items() if k != 'BENCH_RUN_DIR'}
    done = subprocess.run([
        sys.executable, 'bench/micro/middleware_stack.py', profile,
        '--base', str(ROOT), '--head', str(ROOT), '--rounds', '1', '--aa-rounds', '1',
        '--count', '2', '--gc', 'on', '--cpu', str(min(os.sched_getaffinity(0))),
        *(arg for case in selected for arg in ('--case', case)),
    ], cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**environment, 'BENCH_RESULTS_ROOT': str(tmp_path)})
    assert done.returncode == 0, done.stderr
    run, = (tmp_path / 'middleware_stack').iterdir()
    rows = json.loads((run / 'samples.json').read_text())
    assert len(rows) == 8
    assert {s['phase'] for s in rows} == {'null', 'real'}
    assert all(s['source'] == str(ROOT / 'blackbull' / module) for s in rows)
    assert all(set(s['metrics']) == cases for s in rows)
    assert all(v['cpu_us'] > 0 and v['wall_us'] > 0 for s in rows for v in s['metrics'].values())
    instructions = json.loads((run / 'instructions.json').read_text())
    assert instructions['base']['sha256'] == instructions['treat']['sha256']
    assert all(v['instructions'] for v in instructions['base']['metrics'].values())
    if profile == '--compression-init':
        assert all(row['metrics']['settings-cold']['instructions']['Settings.__init__'] > 0
                   for row in instructions.values())
    assert (run / 'COMPLETE').exists()
