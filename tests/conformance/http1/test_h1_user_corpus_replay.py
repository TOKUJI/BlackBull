"""Docker-free replay of the user-corpus diff_*.meta.json entries.

The full differential test ([`test_http1_differential.py`](test_http1_differential.py))
runs nginx as the oracle via testcontainers, so it skips at module
collection when Docker isn't reachable — which excludes the CI
runners we'd most like the regression signal from.

This test reads each curated `diff_*.meta.json` sidecar in
`fuzz/user-corpus/`, sends the recorded `wire_request_latin1` to a
live in-process BlackBull, and asserts the response status still
matches the recorded `blackbull_status`.

The signal is one-sided (no nginx comparison), but exactly what
we want for "did BlackBull's HTTP/1.1 behaviour shift on these
curated edge cases?" — without depending on Docker.

When the existing differential test discovers a new divergence, it
writes a new pair into `fuzz/user-corpus/` and this replay test
picks it up automatically on the next run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conformance.http1.conftest import send_raw


_CORPUS_DIR = Path(__file__).parent / 'fuzz' / 'user-corpus'


def _iter_corpus():
    """Yield (name, wire_bytes, expected_status, source_path) for each
    `diff_*.meta.json` that has a usable BlackBull-side recording.
    """
    if not _CORPUS_DIR.exists():
        return
    for meta_path in sorted(_CORPUS_DIR.glob('diff_*.meta.json')):
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            continue
        wire = meta.get('wire_request_latin1')
        status = meta.get('blackbull_status')
        # Skip entries where BlackBull threw before emitting a status
        # line — those need the full differential test to interpret.
        if not isinstance(wire, str) or not isinstance(status, int):
            continue
        if meta.get('blackbull_exception') is not None:
            continue
        yield (meta_path.stem.removesuffix('.meta'),
               wire.encode('latin1'),
               status,
               meta_path)


_CORPUS = list(_iter_corpus())


@pytest.mark.parametrize(
    'name,wire,expected_status,source_path',
    _CORPUS,
    ids=[c[0] for c in _CORPUS] or ['<no corpus entries>'],
)
def test_user_corpus_status_unchanged(h1_app, name, wire, expected_status,
                                      source_path):
    """Send recorded wire bytes; assert BlackBull's status code matches
    the value recorded when the differential test originally captured
    this divergence.

    A failure here means BlackBull's HTTP/1.1 behaviour shifted on one
    of the curated edge cases.  Either:

    * a real RFC regression — investigate the change that moved the
      response status,
    * an intentional behaviour change — re-capture this corpus entry
      (delete the old `.meta.json` / `.jsonl` pair and re-run the
      differential test under Docker to regenerate).

    The source-path field in the assertion message points at the
    sidecar so the operator can inspect the recorded scenario.
    """
    response = send_raw('127.0.0.1', h1_app.port, wire, timeout=2.0)
    assert response.status == expected_status, (
        f'corpus drift in {source_path.name}: '
        f'recorded blackbull_status={expected_status}, '
        f'replay observed status={response.status}'
    )
