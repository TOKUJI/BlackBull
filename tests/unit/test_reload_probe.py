import importlib.util
import socket
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def probe():
    path = Path(__file__).resolve().parents[2] / 'bench/reload/probe.py'
    spec = importlib.util.spec_from_file_location('reload_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('response,expected', [
    (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nv1', 'v1'),
    (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nv2', 'v2'),
    (b'HTTP/1.1 500 Error\r\nContent-Length: 2\r\n\r\nv1', None),
    (b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nv', None),
    (b'', None),
])
def test_only_complete_success_responses_identify_generation(probe, response, expected):
    client, server = socket.socketpair()

    def reply():
        with server:
            server.recv(4096)
            server.sendall(response)

    thread = threading.Thread(target=reply)
    thread.start()
    with client:
        client.settimeout(1)
        if expected is None:
            with pytest.raises((OSError, probe.http.client.HTTPException, ValueError)):
                probe.exchange(client)
        else:
            assert probe.exchange(client) == expected
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_failed_control_cannot_validate_measurement(probe):
    assert probe.summarize([{'fate': 'served-fast', 'generation': 'v1'}])['affected'] == 0
    assert probe.summarize([{'fate': 'invalid-response'}])['affected'] == 1


def test_failed_control_emits_invalid_json_and_nonzero_exit(probe, monkeypatch, capsys):
    phases = iter([[{'fate': 'served-fast', 'generation': 'v1'}],
                   [{'fate': 'invalid-response'}]])
    checked = []
    monkeypatch.setattr(probe, 'sample', lambda *args: next(phases))
    monkeypatch.setattr(probe, 'managed_process',
                        lambda *args: nullcontext(SimpleNamespace(pid=123)))
    monkeypatch.setattr(probe, 'wait_for', lambda client, process, version, budget:
                        checked.append(version))
    assert probe.main(['--family', 'unix', '--reload', '--warm', '0']) == 1
    output = probe.json.loads(capsys.readouterr().out)
    assert output['valid'] is False
    assert 'control failed' in output['error']
    assert checked == ['v1']


def test_trickling_headers_hit_exchange_deadline_as_timeout(probe, monkeypatch):
    client, server = socket.socketpair()
    client.settimeout(1)

    def trickle():
        with server:
            server.recv(4096)
            try:
                for byte in b'HTTP/1.1 200 OK\r\n':
                    server.sendall(bytes([byte]))
                    time.sleep(0.02)
            except OSError:
                pass

    thread = threading.Thread(target=trickle)
    thread.start()
    connection = probe.Client(socket.AF_UNIX, 'unused', 0.05, 0.5)
    monkeypatch.setattr(connection, 'connect', lambda: client)
    started = time.monotonic()
    record = connection.probe(started)
    thread.join(timeout=2)
    assert record['fate'] == 'timeout'
    assert record['completed'] < 0.5
    assert not thread.is_alive()


def test_requests_continue_to_start_while_previous_request_waits(probe):
    starts = []
    barrier = threading.Barrier(3, timeout=2)

    def request(origin, scheduled):
        starts.append(scheduled)
        barrier.wait()
        return {'t': scheduled, 'fate': 'refused'}

    records = probe.sample(request, 3, 0.02)
    assert len(records) == 3
    assert [start - starts[0] for start in starts] == pytest.approx([0, 0.02, 0.04])


def test_exception_terminates_managed_process(probe, tmp_path):
    process = None
    with pytest.raises(RuntimeError, match='measurement failed'):
        with probe.managed_process([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   tmp_path, {}, tmp_path / 'log') as process:
            raise RuntimeError('measurement failed')
    assert process.poll() is not None


@pytest.mark.parametrize('argv', [['--requests', '0'], ['--park', '-1'],
                                  ['--client-timeout', '0'], ['--interval', 'nan'],
                                  ['--warm', '-1'], ['--active-warm']])
def test_invalid_configuration_is_rejected_before_spawn(probe, argv):
    with pytest.raises(SystemExit) as error:
        probe.arguments(argv)
    assert error.value.code == 2
