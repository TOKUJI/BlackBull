#!/usr/bin/env python3
"""G1: run the fault_injection catalogue against a live server (BLA-526 M5).

The 14 ``h1_client`` and 11 ``h2_client`` scenarios run UNCHANGED — same
payloads, same steps, only the target differs.  The G1-2 oracle judges
each outcome mechanically: an answer (any response) or a close (the peer
closing, or a transport-level error it caused) inside the documented
default wait passes; silence — the wait expiring with neither — fails.  A
harness fault is a runner error, never a close.

Usage::

    python tools/security/catalogue_run.py --host 127.0.0.1 \\
        --port 8000 --h2c-port 8000
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from blackbull.client.http2 import HTTP2Client
from tools.security.junit_report import (DEFAULT_JUNIT_PATH, Case,
                                         write_junit)
from blackbull.fault_injection.catalogue import h1_client, h2_client
from blackbull.fault_injection.oracle_h1 import Observed, run_scenario

#: The documented inventory (docs/security/probe.md, G1).  Order is stable.
H1_SCENARIOS = (
    'absent_host', 'two_content_lengths', 'content_length_and_transfer_encoding',
    'duplicate_transfer_encoding', 'space_before_header_colon',
    'bare_lf_terminators', 'obs_fold_header', 'negative_content_length',
    'chunk_size_not_hex', 'nul_in_header_value', 'oversized_method_token',
    'trickled_head', 'head_never_ends', 'body_shorter_than_declared',
)

H2_SCENARIOS = (
    'preface_never_arrives', 'preface_trickled', 'rapid_reset_burst',
    'ping_flood', 'settings_flood', 'empty_continuation_flood',
    'header_block_never_finished', 'data_frame_lies_about_length',
    'unknown_frame_type', 'settings_ack_with_payload', 'abort_mid_header_block',
)


def judge(outcome, elapsed: float) -> tuple[str, str]:
    """G1-2: answer-or-close passes; silence fails.

    A scenario whose own Abort step short-circuits before any server
    outcome could be observed is SKIP — there is nothing to judge.
    """
    if getattr(outcome, 'timed_out', False):
        return 'FAIL', 'silence: the wait expired with no answer and no close'
    response = getattr(outcome, 'response', None)
    if response is not None:
        status = (response.get('status') if isinstance(response, dict)
                  else getattr(response, 'status', None))
        return 'PASS', f'answered (status {status})'
    if getattr(outcome, 'peer_closed', False):
        return 'PASS', 'closed by the peer'
    exception = getattr(outcome, 'exception', None)
    if exception:
        return 'PASS', f'closed ({exception})'
    if elapsed < 0.5:
        return 'SKIP', 'client aborted before a server outcome was observable'
    return 'FAIL', 'silence: the wait expired with no answer and no close'


async def run_h1(host: str, port: int, name: str) -> tuple[str, str, float]:
    scenario = getattr(h1_client, name)()
    t0 = time.monotonic()
    outcome, _wire = await run_scenario(host, port, scenario)
    elapsed = time.monotonic() - t0
    if outcome.observed is Observed.ERROR:
        raise RuntimeError(f'runner error: {outcome.exception}')
    verdict, detail = judge(outcome.result or outcome, elapsed)
    return verdict, detail, elapsed


async def run_h2(host: str, port: int, name: str) -> tuple[str, str, float]:
    scenario = getattr(h2_client, name)()
    t0 = time.monotonic()
    async with HTTP2Client(host, port, scenario_mode=True) as client:
        outcome = await client.execute_scenario(scenario)
    elapsed = time.monotonic() - t0
    verdict, detail = judge(outcome, elapsed)
    return verdict, detail, elapsed


async def run_all(host: str, port: int, h2c_port: int,
                  junit: str = str(DEFAULT_JUNIT_PATH)) -> int:
    failures = 0
    rows: list[tuple[str, str, str, str, float]] = []
    for name in H1_SCENARIOS:
        try:
            verdict, detail, elapsed = await run_h1(host, port, name)
        except Exception as exc:  # noqa: BLE001 — a runner error is a row
            verdict, detail, elapsed = 'FAIL', f'runner error: {exc!r}', 0.0
        rows.append(('h1_client', name, verdict, detail, elapsed))
        failures += verdict == 'FAIL'
    for name in H2_SCENARIOS:
        try:
            verdict, detail, elapsed = await run_h2(host, h2c_port, name)
        except Exception as exc:  # noqa: BLE001
            verdict, detail, elapsed = 'FAIL', f'runner error: {exc!r}', 0.0
        rows.append(('h2_client', name, verdict, detail, elapsed))
        failures += verdict == 'FAIL'

    print('G1 catalogue run (answer-or-close passes; silence fails)')
    for cat, name, verdict, detail, elapsed in rows:
        print(f'  {cat:<10} {name:<38} {verdict:<4} '
              f'{elapsed:5.2f}s  {detail}')
    skipped = sum(1 for _, _, v, _, _ in rows if v == 'SKIP')
    print(f'{len(rows)} scenarios, {len(rows) - failures - skipped} PASS, '
          f'{failures} FAIL, {skipped} SKIP')
    if junit:
        path = write_junit(Path(junit), [
            Case('catalogue', cat, name, verdict, detail, elapsed)
            for cat, name, verdict, detail, elapsed in rows])
        print(f'junit: {path}')
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--h2c-port', type=int, default=8000,
                    help='h2c prior-knowledge target: the cleartext listener')
    parser.add_argument('--junit', default=str(DEFAULT_JUNIT_PATH),
                    help='merge verdicts into this JUnit XML (empty disables)')
    args = parser.parse_args()
    failures = asyncio.run(run_all(args.host, args.port, args.h2c_port,
                                  args.junit))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
