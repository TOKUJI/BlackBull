"""blackbull-client end to end: a subprocess, real reference servers, exit codes.

The JUnit reports land in ``BB_CLIENT_CLI_REPORTS`` when it is set (the CI
job keeps them as an artifact), else in the test's temporary directory.
"""
from __future__ import annotations

import asyncio
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from blackbull.fault_injection.catalogue.h2_differential import BROKEN_CLIENTS
from blackbull.fault_injection.cli import EXIT_DIVERGED, EXIT_OK
from blackbull.fault_injection.scenario_h1 import ReadResponse, Scenario, SendRawBytes
from blackbull.fault_injection.scenario_h2_client import scenario_to_json

from ._reference_peers import PEER_BUDGET
from .test_h2_differential import _ok_app

pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group('reference_peers')]


@pytest.fixture
def reports(tmp_path) -> Path:
    path = Path(os.environ.get('BB_CLIENT_CLI_REPORTS') or tmp_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _client(*argv) -> int:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'blackbull.fault_injection.cli', *map(str, argv))
    return await asyncio.wait_for(proc.wait(), 60)


@pytest.mark.timeout(PEER_BUDGET)
async def test_run_against_a_reference_server(tmp_path, reports, nginx_peer):
    scenario = tmp_path / 'get.jsonl'
    scenario.write_text(Scenario(name='h1_get', steps=(
        SendRawBytes(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'), ReadResponse())).to_json())
    junit = reports / 'run-h1-nginx.xml'
    host, port = nginx_peer
    assert await _client('run', scenario, '--protocol', 'h1', '--target', f'{host}:{port}',
                         '--junit', junit) == EXIT_OK
    assert ET.parse(junit).getroot().get('failures') == '0'


@pytest.mark.parametrize('case,reference,expected', [
    pytest.param('control_valid_get', 'nginx', EXIT_OK, id='agree-nginx'),
    pytest.param('te_gzip', 'nghttpd', EXIT_OK, id='both-reject'),
    # Listed in test_h2_differential's KNOWN_SERVER: the CLI must gate on it.
    pytest.param('te_gzip', 'nginx', EXIT_DIVERGED, id='diverge-nginx'),
])
@pytest.mark.timeout(PEER_BUDGET)
async def test_oracle_against_a_reference_server(tmp_path, reports, nginx_peer, nghttpd_peer,
                                                 case, reference, expected):
    from blackbull.testing.native import NativeTestServer

    scenario = tmp_path / f'{case}.jsonl'
    scenario.write_text(scenario_to_json(BROKEN_CLIENTS[case].build()))
    host, port = {'nginx': nginx_peer, 'nghttpd': nghttpd_peer}[reference]
    junit = reports / f'oracle-h2-{case}-{reference}.xml'
    async with NativeTestServer(_ok_app()) as server:
        code = await _client('oracle', scenario, '--protocol', 'h2',
                             '--target', f'127.0.0.1:{server.port}',
                             '--reference', f'{host}:{port}', '--junit', junit)
    assert code == expected
    assert ET.parse(junit).getroot().get('failures') == str(int(expected == EXIT_DIVERGED))
