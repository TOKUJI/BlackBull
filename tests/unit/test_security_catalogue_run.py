"""G1-2: an answer or a close passes, silence fails, a harness fault is no close."""
from __future__ import annotations

import socket

import pytest

from blackbull.fault_injection.scenario_h1 import ScenarioResult
from blackbull.fault_injection.scenario_h2_client import ScenarioH2ClientResult
from tools.security.catalogue_run import judge, run_h1


@pytest.mark.parametrize('result_cls', [ScenarioResult, ScenarioH2ClientResult])
def test_a_close_by_the_peer_passes(result_cls):
    assert judge(result_cls(peer_closed=True), 1.0)[0] == 'PASS'


@pytest.mark.parametrize('result_cls', [ScenarioResult, ScenarioH2ClientResult])
def test_silence_fails(result_cls):
    assert judge(result_cls(timed_out=True), 1.0)[0] == 'FAIL'
    assert judge(result_cls(), 1.0)[0] == 'FAIL'


@pytest.mark.asyncio
async def test_a_harness_fault_is_a_runner_error_not_a_close():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(RuntimeError, match='runner'):
        await run_h1('127.0.0.1', port, 'absent_host')
