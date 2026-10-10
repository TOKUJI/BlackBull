#!/usr/bin/env python3
"""Run an existing Atheris harness unchanged under Python 3.14 (BLA-526 G6).

The harnesses fork their BlackBull server with a lambda ``Process`` target,
which only pickles under the ``fork`` start method — Python 3.14 defaults
to ``forkserver`` on Linux.  This runner forces ``fork``, drops into the
harness directory (its ``corpus/`` paths are cwd-relative), and executes
the file as ``__main__`` so its server bootstrap runs.  No harness code
is modified or duplicated.

Usage::

    python tools/security/fuzz_run.py http1 [-max_total_time=60] [corpus/]
    python tools/security/fuzz_run.py http2 [...]

Extra arguments are passed to the harness verbatim.
"""
import multiprocessing as mp
import os
import runpy
import sys

HARNESSES = {
    'http1': ('tests/conformance/http1/fuzz', 'fuzz_http1.py'),
    'http2': ('tests/conformance/http2/fuzz', 'fuzz_http2.py'),
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in HARNESSES:
        print(__doc__)
        return 2
    name = sys.argv[1]
    directory, script = HARNESSES[name]
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    os.chdir(os.path.join(root, directory))
    sys.path.insert(0, root)
    mp.set_start_method('fork')
    sys.argv = [script, *sys.argv[2:]]
    runpy.run_path(os.path.abspath(script), run_name='__main__')
    return 0


if __name__ == '__main__':
    sys.exit(main())
