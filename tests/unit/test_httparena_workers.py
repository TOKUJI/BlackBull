import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ARENA = Path(__file__).resolve().parents[2] / 'bench/httparena'


@pytest.mark.parametrize('override,affinity,host,expected', [
    ('', 64, 128, 64),
    ('', 8, 128, 8),
    ('', 256, 256, 128),
    (' 4 ', 8, 128, 4),
    ('32', 8, 128, 32),
    ('256', 8, 128, 128),
    ('0', 8, 128, 8),
    ('-1', 8, 128, 8),
    ('invalid', 8, 128, 8),
    ('', None, 8, 8),
    ('', 'error', 8, 8),
    ('', None, 256, 128),
    ('', None, None, 1),
])
def test_launcher_and_pool_use_the_same_worker_count(tmp_path, override, affinity, host, expected):
    probe = '''
import json, multiprocessing, os, runpy, subprocess, sys
from types import SimpleNamespace
from unittest.mock import patch

arena, affinity, host = json.loads(sys.argv[1])
sys.path.insert(0, arena)
def cpu_count():
    if host is None:
        raise NotImplementedError
    return host
def get_affinity(pid):
    if affinity == 'error':
        raise OSError('unavailable')
    return set(range(affinity))
def spawn(argv):
    calls.append({'argv': argv, 'env': dict(os.environ)})
    return SimpleNamespace(wait=lambda: 0, terminate=lambda: None)
calls = []
with patch.object(multiprocessing, 'cpu_count', cpu_count), patch.object(subprocess, 'Popen', spawn):
    if affinity is None:
        if hasattr(os, 'sched_getaffinity'):
            del os.sched_getaffinity
        context = patch.dict(os.environ, {})
    else:
        context = patch.object(os, 'sched_getaffinity', get_affinity, create=True)
    with context:
        import db
        standalone_pool = db.POOL_MAX_PER_WORKER
        try:
            runpy.run_path(arena + '/launcher.py')
        except SystemExit as result:
            assert result.code == 0
        del sys.modules['db']
        import db
        print(json.dumps({'calls': calls, 'pool_size': db.POOL_MAX_PER_WORKER,
                          'standalone_pool': standalone_pool}))
'''
    env = {**os.environ, 'WEB_WORKERS': override, 'BB_HTTPARENA_PORTS': 'http',
           'TLS_CERT': str(tmp_path / 'missing.crt'), 'TLS_KEY': str(tmp_path / 'missing.key'),
           'DATABASE_MAX_CONN': '256'}
    result = subprocess.run([sys.executable, '-c', probe,
                             json.dumps([str(ARENA), affinity, host])],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert len(observed['calls']) == 1
    assert observed['calls'][0]['env']['WEB_WORKERS'] == str(expected)
    argv = observed['calls'][0]['argv']
    assert int(argv[argv.index('--workers') + 1]) == expected
    assert observed['pool_size'] == max(1, 240 // expected)
    assert observed['standalone_pool'] == max(1, 240 // expected)
