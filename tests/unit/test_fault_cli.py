"""blackbull-client: rails before bytes, and exit codes CI can gate on."""
from __future__ import annotations

import asyncio
import json
import socket
import xml.etree.ElementTree as ET
from http import HTTPMethod, HTTPStatus

import pytest

from blackbull import BlackBull, Response
from blackbull.fault_injection import cli
from blackbull.fault_injection.scenario_h1 import (
    ExpectResponse, ReadResponse, Scenario, SendRawBytes as H1SendRawBytes,
)
from blackbull.fault_injection.scenario_h2_client import (
    ScenarioH2Client, SendFrame, SendHeaders, SendPreface, WaitForServerFrame,
)
from blackbull.fault_injection.scenario_h2_client import scenario_to_json as h2_to_json
from blackbull.protocol.frame_types import FrameTypes
from blackbull.testing.native import NativeTestServer

pytestmark = pytest.mark.asyncio

_GET = ((':method', 'GET'), (':scheme', 'http'), (':authority', 'localhost'), (':path', '/'))


def _app(status: int = 200):
    app = BlackBull()

    @app.route(path='/', methods=[HTTPMethod.GET])
    async def _root(conn, receive, send):
        await send(Response(b'ok', status=HTTPStatus(status)))

    return app


def _h1(tmp_path, *, expect_status: int | None = None):
    steps = [H1SendRawBytes(b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n')]
    steps.append(ExpectResponse(match={'status': expect_status}) if expect_status
                 else ReadResponse())
    tmp_path.mkdir(exist_ok=True)
    path = tmp_path / 'h1.jsonl'
    path.write_text(Scenario(name='get', steps=tuple(steps)).to_json())
    return path


def _h2(tmp_path, *, read: bool = True):
    steps = [SendPreface(), SendFrame(FrameTypes.SETTINGS),
             SendHeaders(pseudo=_GET, end_stream=True)]
    if read:
        steps.append(WaitForServerFrame(match={'type': 'HEADERS', 'stream_id': 1}, timeout=5.0))
    path = tmp_path / 'h2.jsonl'
    path.write_text(h2_to_json(ScenarioH2Client(name='get', steps=tuple(steps))))
    return path


async def _cli(*argv) -> int:
    """main() runs its own loop, so it runs beside the servers' loop."""
    return await asyncio.to_thread(cli.main, [str(a) for a in argv])


@pytest.fixture
def server():
    return NativeTestServer(_app())


# -- usage and rails: nothing reaches the target ------------------------------

class _Listener:
    """Counts connections, to prove a refused invocation sent nothing."""

    def __enter__(self):
        self.sock = socket.create_server(('127.0.0.1', 0))
        self.sock.settimeout(0.3)
        self.port = self.sock.getsockname()[1]
        return self

    def accepted(self) -> bool:
        try:
            self.sock.accept()[0].close()
            return True
        except TimeoutError:
            return False

    def __exit__(self, *exc):
        self.sock.close()


@pytest.mark.parametrize('argv', [
    pytest.param(['run', '{h1}', '--target', '127.0.0.1:{port}'], id='no-protocol'),
    pytest.param(['run', '{h1}', '--protocol', 'h3', '--target', '127.0.0.1:{port}'],
                 id='bad-protocol'),
    pytest.param(['run', '{h1}', '--protocol', 'h1', '--target', 'no-port'], id='bad-target'),
    pytest.param(['run', '{missing}', '--protocol', 'h1', '--target', '127.0.0.1:{port}'],
                 id='missing-file'),
    pytest.param(['run', '{h1expect}', '--protocol', 'h2', '--target', '127.0.0.1:{port}'],
                 id='wrong-vocabulary'),
    pytest.param(['run', '{h1}', '--protocol', 'h1', '--target', '127.0.0.1:{port}', '--tls',
                  '--insecure', '--cafile', '{h1}'], id='insecure-and-cafile'),
    pytest.param(['oracle', '{h2read}', '--protocol', 'h2', '--target', '127.0.0.1:{port}',
                  '--reference', '127.0.0.1:{port}'], id='h2-oracle-read-step'),
    pytest.param(['run', '{h1}', '--protocol', 'h1', '--target', '10.0.0.1:{port}'],
                 id='remote-without-allow'),
    pytest.param(['run', '{h1}', '--protocol', 'h1', '--target', '127.0.0.1:{port}',
                  '--insecure'], id='insecure-without-tls'),
])
async def test_a_refused_invocation_exits_2_and_sends_nothing(tmp_path, argv):
    with _Listener() as listener:
        paths = dict(h1=_h1(tmp_path), h1expect=_h1(tmp_path / 'x', expect_status=200),
                     h2read=_h2(tmp_path), missing=tmp_path / 'none.jsonl', port=listener.port)
        code = await _cli(*(a.format(**paths) for a in argv))
        assert code == cli.EXIT_USAGE
        assert not listener.accepted()


@pytest.mark.parametrize('env', [{}, {'BLACKBULL_ENV': 'production'},
                                 {'BLACKBULL_ENV': 'test', 'BB_PRODUCTION': '1'}])
async def test_a_production_context_is_refused_before_connecting(tmp_path, monkeypatch, env):
    from blackbull.env import reset_settings_cache

    monkeypatch.delenv('BLACKBULL_ENV', raising=False)
    monkeypatch.delenv('BB_PRODUCTION', raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    reset_settings_cache()
    with _Listener() as listener:
        code = await _cli('run', _h1(tmp_path), '--protocol', 'h1',
                          '--target', f'127.0.0.1:{listener.port}')
        assert code == cli.EXIT_USAGE
        assert not listener.accepted()


# -- run ----------------------------------------------------------------------

async def test_run_h1_writes_the_steps_and_the_outcome(tmp_path, server):
    report, junit = tmp_path / 'run.json', tmp_path / 'run.xml'
    async with server:
        code = await _cli('run', _h1(tmp_path), '--protocol', 'h1',
                          '--target', f'127.0.0.1:{server.port}', '--json', report,
                          '--junit', junit)
    assert code == cli.EXIT_OK
    record = json.loads(report.read_text())
    assert [s['op'] for s in record['steps']] == ['SendRawBytes', 'ReadResponse']
    assert all(s['completed'] for s in record['steps'])
    assert record['steps'][0]['wire'] == b'GET / HTTP/1.1\r\nHost: localhost\r\n\r\n'.hex()
    assert record['outcome']['responses'][0]['status'] == 200
    case = ET.parse(junit).getroot().find('.//testcase')
    assert case.get('name') == 'get' and len(case) == 1 and case[0].tag == 'system-out'


async def test_run_h1_whose_expectation_fails_exits_1(tmp_path, server):
    junit = tmp_path / 'run.xml'
    async with server:
        code = await _cli('run', _h1(tmp_path, expect_status=404), '--protocol', 'h1',
                          '--target', f'127.0.0.1:{server.port}', '--junit', junit)
    assert code == cli.EXIT_DIVERGED
    assert ET.parse(junit).getroot().find('.//testcase/failure') is not None


async def test_run_h2_reads_frames(tmp_path, server):
    report = tmp_path / 'run.json'
    async with server:
        code = await _cli('run', _h2(tmp_path), '--protocol', 'h2',
                          '--target', f'127.0.0.1:{server.port}', '--json', report)
    assert code == cli.EXIT_OK
    frames = json.loads(report.read_text())['outcome']['frames']
    assert {'type': 'Headers', 'stream_id': 1} in [
        {k: f[k] for k in ('type', 'stream_id')} for f in frames]


@pytest.mark.parametrize('protocol', ['h1', 'h2'])
async def test_a_target_that_refuses_the_connection_exits_3(tmp_path, protocol):
    with _Listener() as listener:
        port = listener.port
    junit = tmp_path / 'run.xml'
    scenario = _h1(tmp_path) if protocol == 'h1' else _h2(tmp_path)
    code = await _cli('run', scenario, '--protocol', protocol, '--target', f'127.0.0.1:{port}',
                      '--junit', junit)
    assert code == cli.EXIT_TRANSPORT
    assert ET.parse(junit).getroot().find('.//testcase/error') is not None


# -- oracle -------------------------------------------------------------------

@pytest.mark.parametrize('protocol', ['h1', 'h2'])
async def test_oracle_agreement_exits_0(tmp_path, protocol):
    report = tmp_path / 'oracle.json'
    scenario = _h1(tmp_path) if protocol == 'h1' else _h2(tmp_path, read=False)
    async with NativeTestServer(_app()) as target, NativeTestServer(_app()) as reference:
        code = await _cli('oracle', scenario, '--protocol', protocol,
                          '--target', f'127.0.0.1:{target.port}',
                          '--reference', f'127.0.0.1:{reference.port}', '--json', report)
    assert code == cli.EXIT_OK
    assert json.loads(report.read_text())['category'] == 'OK'


@pytest.mark.parametrize('protocol', ['h1', 'h2'])
async def test_oracle_divergence_exits_1(tmp_path, protocol):
    report, junit = tmp_path / 'oracle.json', tmp_path / 'oracle.xml'
    scenario = _h1(tmp_path) if protocol == 'h1' else _h2(tmp_path, read=False)
    async with NativeTestServer(_app()) as target, NativeTestServer(_app(503)) as reference:
        code = await _cli('oracle', scenario, '--protocol', protocol,
                          '--target', f'127.0.0.1:{target.port}',
                          '--reference', f'127.0.0.1:{reference.port}',
                          '--json', report, '--junit', junit)
    assert code == cli.EXIT_DIVERGED
    assert json.loads(report.read_text())['category'] == 'STATUS_DIFFER'
    assert 'STATUS_DIFFER' in ET.parse(junit).getroot().find('.//testcase/failure').get('message')


# -- TLS ----------------------------------------------------------------------

async def test_tls_verifies_unless_told_otherwise(tmp_path):
    pytest.importorskip('cryptography')
    from blackbull.fault_injection import ScenarioH2, make_self_signed_h2_context
    from blackbull.fault_injection.h2_server import H2FaultServer
    from blackbull.fault_injection.scenario_h2 import CloseGracefully

    context = make_self_signed_h2_context()
    scenario = _h2(tmp_path, read=False)
    async with H2FaultServer(ScenarioH2(steps=(CloseGracefully(),)), ssl_context=context) as srv:
        target = f'localhost:{srv.port}'
        base = ('run', scenario, '--protocol', 'h2', '--target', target, '--tls')
        assert await _cli(*base) == cli.EXIT_TRANSPORT
        assert await _cli(*base, '--cafile', context.bb_ca_cert_path) == cli.EXIT_OK
        assert await _cli(*base, '--insecure') == cli.EXIT_OK


@pytest.mark.parametrize('protocol', ['h1', 'h2'])
async def test_a_failure_inside_the_runner_is_a_transport_failure(tmp_path, monkeypatch, protocol,
                                                                  server):
    """HTTP2Client raises its own ClientError, which is no OSError; and
    nothing a runner raises may read as a divergence (exit 1)."""
    from blackbull.client.exceptions import HandshakeError
    from blackbull.client.http2 import HTTP2Client

    async def refuse(self):
        raise HandshakeError('the peer answered the preface with garbage')

    monkeypatch.setattr(HTTP2Client, '__aenter__', refuse)
    monkeypatch.setattr(cli.oracle_h1, 'run_scenario', lambda *a, **k: refuse(None))
    scenario = _h1(tmp_path) if protocol == 'h1' else _h2(tmp_path)
    async with server:
        code = await _cli('run', scenario, '--protocol', protocol,
                          '--target', f'127.0.0.1:{server.port}')
    assert code == cli.EXIT_TRANSPORT


async def test_an_unreadable_cafile_is_a_usage_error(tmp_path):
    with _Listener() as listener:
        code = await _cli('run', _h1(tmp_path), '--protocol', 'h1',
                          '--target', f'127.0.0.1:{listener.port}', '--tls',
                          '--cafile', tmp_path / 'absent.pem')
        assert code == cli.EXIT_USAGE
        assert not listener.accepted()


async def test_cafile_adds_to_the_system_trust_store(tmp_path, monkeypatch):
    """The context starts from the system store; --cafile is loaded on top."""
    import ssl

    pytest.importorskip('cryptography')
    from blackbull.fault_injection import make_self_signed_h2_context

    created, loaded = [], []
    real = ssl.create_default_context

    def spy(*args, **kwargs):
        context = real(*args, **kwargs)
        created.append(kwargs)
        load = context.load_verify_locations
        context.load_verify_locations = lambda **kw: (loaded.append(kw), load(**kw))[1]
        return context

    monkeypatch.setattr('ssl.create_default_context', spy)
    ca = make_self_signed_h2_context().bb_ca_cert_path
    with _Listener() as listener:
        port = listener.port
    await _cli('run', _h1(tmp_path), '--protocol', 'h1', '--target', f'127.0.0.1:{port}',
               '--tls', '--cafile', ca)
    assert created == [{}]                 # no cafile= here: the system CAs load
    assert [{k: str(v) for k, v in kw.items()} for kw in loaded] == [{'cafile': ca}]


async def test_an_unwritable_report_path_is_a_usage_error(tmp_path, server):
    async with server:
        code = await _cli('run', _h1(tmp_path), '--protocol', 'h1',
                          '--target', f'127.0.0.1:{server.port}',
                          '--junit', tmp_path / 'absent-dir' / 'run.xml')
    assert code == cli.EXIT_USAGE
