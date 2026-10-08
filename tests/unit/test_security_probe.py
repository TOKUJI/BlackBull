"""Unit tests for the BLA-526 probe's pure parts: safety gate, accept-set
helpers, dual-lane routing, and the report writer.

The checks themselves need live servers (``just vuln-check``) and are never
run under pytest; pytest.ini keeps tools/ out of collection entirely.
"""
from __future__ import annotations

import time

import pytest

from tools.security.probe import (
    ALLOWED_HOSTS,
    CHECKS,
    LANES,
    PASS,
    SEVERITIES,
    TIMEOUT,
    FAIL,
    Check,
    CheckResult,
    H2Info,
    Lane,
    UnsafeTargetError,
    Verdict,
    abuse_accept_verdict,
    checks_for,
    exit_code,
    h2_error_verdict,
    h2_info,
    h2_ok_verdict,
    main,
    parse_target,
    render_markdown,
    render_table,
    run_checks,
    state_verdict,
    tls_verdict,
    write_report,
)

_LOOPBACK_URLS = (
    'http://127.0.0.1:8000',
    'http://localhost:8000',
    'http://[::1]:8000',
    'http://127.0.0.1',
    'http://LOCALHOST:9000/probe',
    'https://127.0.0.1:8443',
    'https://localhost:8443',
)

_REFUSED_URLS = (
    'http://example.com:8000',
    'http://10.0.0.1:8000',
    'http://192.168.1.1:8000',
    'http://127.0.0.1.evil.example:8000',
    'http://0x7f000001:8000',
    'http://user:pass@127.0.0.1:8000',
    'file:///etc/passwd',
    'gopher://127.0.0.1:8000',
    'http://[::1',
    'http://127.0.0.1:abc',
    'http://127.0.0.1:-1',
)


@pytest.mark.parametrize('url', _LOOPBACK_URLS)
def test_parse_target_accepts_loopback(url):
    target = parse_target(url)
    assert target.host in ALLOWED_HOSTS
    assert isinstance(target.port, int) and 0 < target.port < 65536


@pytest.mark.parametrize('url', _REFUSED_URLS)
def test_parse_target_refuses_non_loopback(url):
    with pytest.raises(UnsafeTargetError):
        parse_target(url)


def test_parse_target_defaults_ports_per_scheme():
    assert parse_target('http://127.0.0.1').port == 80
    assert parse_target('https://127.0.0.1').port == 443


def test_main_exits_2_on_refused_target_without_io():
    assert main(['--base-url', 'http://example.com:8000']) == 2


def test_main_gates_the_h2_lane_url_too():
    assert main(['--h2-url', 'http://10.0.0.1:8443']) == 2


def test_main_exits_2_on_malformed_target():
    assert main(['--base-url', 'http://[::1']) == 2


def test_main_refuses_https_lane_without_a_verifiable_ca(tmp_path):
    assert main(['--lane', 'h2', '--tls-ca', str(tmp_path / 'missing.pem')]) == 2


def test_main_rejects_non_finite_timeouts():
    with pytest.raises(SystemExit) as excinfo:
        main(['--check-timeout', 'nan'])
    assert excinfo.value.code == 2


def test_hang_escalates_h1_robust_checks_to_high():
    by_id = {check.check_id: check for check in CHECKS}
    for check_id in ('H1-ROBUST-001', 'H1-ROBUST-002', 'H1-ROBUST-003',
                     'H1-ROBUST-005', 'H1-ROBUST-006', 'H1-ROBUST-007',
                     'H1-ROBUST-008', 'H1-ROBUST-009', 'H1-ROBUST-010',
                     'H1-ROBUST-011'):
        assert by_id[check_id].timeout_severity() == 'High'


def _results() -> list[CheckResult]:
    return [
        CheckResult('BASELINE-001', 'GET / returns 200 with body "ok"',
                    'High', PASS, '200, body "ok"', 'CWE-400'),
        CheckResult('SMUGGLE-001', 'CL+TE together rejected',
                    'High', FAIL, 'accepted abuse with 200', 'CWE-444'),
        CheckResult('H1-ROBUST-002', '100 KiB header value',
                    'Medium', TIMEOUT, 'no response within 5s', 'CWE-400'),
    ]


def test_render_table_contains_check_rows():
    table = render_table(_results())
    for row in _results():
        assert row.check_id in table
        assert row.verdict in table
    assert table.splitlines()[0].split() == ['check', 'severity', 'verdict', 'detail']


def _lanes() -> list[Lane]:
    return [
        Lane('h1', 'http://127.0.0.1:8000', tuple(_results())),
        Lane('h2', 'https://127.0.0.1:8443', (
            CheckResult('H2-BASE-001', 'GET / over h2', 'High', PASS,
                        '200, body "ok"', 'CWE-400'),
            CheckResult('TLS-001', 'TLS floor', 'Medium', FAIL,
                        'TLS1.0: handshake succeeded', 'CWE-326'),
        )),
    ]


def test_render_markdown_has_one_table_per_lane():
    md = render_markdown(_lanes(), check_timeout=5.0, run_timeout=120.0,
                         timestamp='20260101T000000Z')
    assert '## h1 lane — http://127.0.0.1:8000' in md
    assert '## h2 lane — https://127.0.0.1:8443' in md
    assert '| BASELINE-001 | High | PASS |' in md
    assert '| SMUGGLE-001 | High | FAIL |' in md
    assert '| H2-BASE-001 | High | PASS |' in md
    assert '| TLS-001 | Medium | FAIL |' in md
    assert 'CWE-444' in md and 'CWE-326' in md


def test_write_report_writes_both_lane_tables(tmp_path):
    path = write_report(_lanes(), check_timeout=5.0, run_timeout=120.0,
                        timestamp='20260101T000000Z', out_dir=tmp_path)
    assert path == tmp_path / '20260101T000000Z.md'
    text = path.read_text(encoding='utf-8')
    assert '| BASELINE-001 | High | PASS |' in text
    assert '| TLS-001 | Medium | FAIL |' in text


def test_exit_code_maps_verdicts_across_lanes():
    assert exit_code([Lane('h1', 'http://127.0.0.1:8000', (_results()[0],))]) == 0
    assert exit_code(_lanes()) == 1


def test_check_registry_uses_lane_and_severity_vocabulary():
    ids = [check.check_id for check in CHECKS]
    assert len(ids) == len(set(ids))
    for check in CHECKS:
        assert check.lane in LANES
        assert check.severity in SEVERITIES
        assert (check.severity_on_timeout or check.severity) in SEVERITIES
        assert check.description
        assert check.cwe.startswith('CWE-')
    assert checks_for('h1')[-1].check_id == 'BASELINE-003'
    assert checks_for('h2')[-1].check_id == 'TLS-001'
    assert len(checks_for('h1')) + len(checks_for('h2')) == len(CHECKS)


def test_run_checks_routes_checks_and_records_budget_exhaustion():
    seen: list[str] = []

    def run(probe):
        seen.append(probe.target.host)
        return Verdict(PASS, 'stub ok')

    stub = (Check('STUB-001', 'stub', 'Low', 'CWE-400', run, 'h1'),)
    from tools.security.probe import Probe, Target
    probe = Probe(Target(scheme='http', host='127.0.0.1', port=8000), 5.0)
    results = run_checks(probe, stub, deadline=time.monotonic() + 10)
    assert [(r.check_id, r.verdict) for r in results] == [('STUB-001', PASS)]
    assert seen == ['127.0.0.1']

    late = run_checks(probe, stub, deadline=time.monotonic() - 1)
    assert late[0].verdict == TIMEOUT
    assert late[0].detail == 'run budget exhausted'


def test_run_checks_maps_timeout_to_the_escalated_rank():
    def hang(probe):
        raise TimeoutError('stub deadline')

    stub = (Check('STUB-002', 'stub', 'Info', 'CWE-400', hang, 'h1', 'High'),)
    from tools.security.probe import Probe, Target
    probe = Probe(Target(scheme='http', host='127.0.0.1', port=8000), 5.0)
    results = run_checks(probe, stub, deadline=time.monotonic() + 10)
    assert results[0].verdict == TIMEOUT
    assert results[0].severity == 'High'


# ------------------------------------------------------------------
# STATE-001 / accept-set helpers
# ------------------------------------------------------------------

_OK = (200, b'ok')
_ECHO = (200, b'Z')


def test_state_verdict_accepts_one_clean_followup():
    assert state_verdict((_ECHO, _OK), 'timeout').verdict == PASS
    assert state_verdict((_OK,), 'timeout').verdict == PASS
    assert state_verdict(((400, b'bad'),), 'closed').verdict == PASS
    assert state_verdict((), 'closed').verdict == PASS


def test_state_verdict_rejects_contaminated_sequences():
    assert state_verdict((_ECHO, (200, b'garbled')), 'timeout').verdict == FAIL
    assert state_verdict((_ECHO, _OK, _OK), 'timeout').verdict == FAIL
    assert state_verdict(((400, b'bad'),), 'timeout').verdict == FAIL
    assert state_verdict((_ECHO,), 'error:ValueError').verdict == FAIL


def test_state_verdict_times_out_when_nothing_arrives():
    assert state_verdict((), 'timeout').verdict == TIMEOUT


def test_abuse_accept_verdict_accept_set_and_close():
    assert abuse_accept_verdict(((400, b'bad'), _OK), 'timeout',
                                accept=frozenset({400}), expected='400').verdict == PASS
    assert abuse_accept_verdict((), 'closed', accept=frozenset({400}),
                                expected='400').verdict == PASS
    assert abuse_accept_verdict(((500, b'x'), _OK), 'timeout',
                                accept=frozenset({400}), expected='400').verdict == FAIL


def test_abuse_accept_verdict_echo_body_rule():
    ok = abuse_accept_verdict((_ECHO, _OK), 'timeout', accept=frozenset({200, 400}),
                              expected='400', ok_200_body=b'Z')
    assert ok.verdict == PASS
    bad = abuse_accept_verdict(((200, b'wrong'), _OK), 'timeout',
                               accept=frozenset({200, 400}),
                               expected='400', ok_200_body=b'Z')
    assert bad.verdict == FAIL


def test_abuse_accept_verdict_flags_dropped_abuse():
    dropped = abuse_accept_verdict((_OK,), 'timeout', accept=frozenset({400}),
                                   expected='400')
    assert dropped.verdict == FAIL
    assert 'not rejected' in dropped.detail


# ------------------------------------------------------------------
# h2 frame helpers
# ------------------------------------------------------------------

class _FakeFrame:
    def __init__(self, frame_type, *, stream_id=0, error_code=None,
                 pseudo=None, payload=b'', end_stream=False):
        self._frame_type = frame_type
        self.stream_id = stream_id
        if error_code is not None:
            self.error_code = error_code
        if pseudo is not None:
            self.pseudo_headers = pseudo
        if payload:
            self.payload = payload
        if end_stream:
            self.end_stream = end_stream

    def FrameType(self):
        return self._frame_type


class _FakeType:
    def __init__(self, name):
        self.name = name


def test_h2_info_reduces_frames_and_none():
    headers = h2_info(_FakeFrame(_FakeType('HEADERS'), stream_id=1,
                                 pseudo={':status': '200'}, end_stream=True))
    assert headers == H2Info(kind='HEADERS', stream_id=1, error_code=None,
                             status=200, body=b'', end_stream=True)
    data = h2_info(_FakeFrame(_FakeType('DATA'), stream_id=1, payload=b'ok'))
    assert (data.kind, data.body) == ('DATA', b'ok')
    goaway = h2_info(_FakeFrame(_FakeType('GOAWAY'), error_code=1))
    assert (goaway.kind, goaway.error_code) == ('GOAWAY', 1)
    assert h2_info(None).kind == 'EOF'
    assert h2_info(_FakeFrame(None)).kind == 'UNKNOWN'


def test_h2_ok_verdict_requires_clean_stream_response():
    infos = [h2_info(_FakeFrame(_FakeType('HEADERS'), stream_id=1,
                                pseudo={':status': '200'})),
             h2_info(_FakeFrame(_FakeType('DATA'), stream_id=1, payload=b'ok'))]
    assert h2_ok_verdict(infos, stream_id=1).verdict == PASS
    assert h2_ok_verdict(infos[:1], stream_id=1).verdict == FAIL
    bad = [infos[0], h2_info(_FakeFrame(_FakeType('DATA'), stream_id=1,
                                        payload=b'no'))]
    assert h2_ok_verdict(bad, stream_id=1).verdict == FAIL


def test_h2_error_verdict_accepts_matching_error_frames():
    rst = [h2_info(_FakeFrame(_FakeType('RST_STREAM'), stream_id=1,
                              error_code=1))]
    assert h2_error_verdict(rst, stream_id=1,
                            accept_codes=frozenset({1}),
                            expected='PROTOCOL_ERROR').verdict == PASS
    goaway = [h2_info(_FakeFrame(_FakeType('GOAWAY'), error_code=1))]
    conn = h2_error_verdict(goaway, stream_id=None, accept_codes=frozenset({1}),
                            expected='PROTOCOL_ERROR',
                            accept_kinds=frozenset({'GOAWAY'}))
    assert conn.verdict == PASS


def test_h2_error_verdict_rejects_wrong_code_or_wrong_kind():
    rst = [h2_info(_FakeFrame(_FakeType('RST_STREAM'), stream_id=1,
                              error_code=8))]
    assert h2_error_verdict(rst, stream_id=1, accept_codes=frozenset({1}),
                            expected='PROTOCOL_ERROR').verdict == FAIL
    # A stream error cannot stand in for the required connection error.
    stream_only = h2_error_verdict(rst, stream_id=None, accept_codes=frozenset({8}),
                                   expected='GOAWAY PROTOCOL_ERROR',
                                   accept_kinds=frozenset({'GOAWAY'}))
    assert stream_only.verdict == FAIL


def test_h2_error_verdict_close_and_4xx_and_timeout():
    closed = [h2_info(None)]
    assert h2_error_verdict(closed, stream_id=1, accept_codes=frozenset({1}),
                            expected='PROTOCOL_ERROR').verdict == PASS
    too_large = [h2_info(_FakeFrame(_FakeType('HEADERS'), stream_id=1,
                                    pseudo={':status': '431'}))]
    verdict = h2_error_verdict(too_large, stream_id=1, accept_codes=frozenset(),
                               expected='ENHANCE_YOUR_CALM',
                               accept_4xx=frozenset({431}))
    assert verdict.verdict == PASS
    assert h2_error_verdict([], stream_id=1, accept_codes=frozenset({1}),
                            expected='PROTOCOL_ERROR').verdict == TIMEOUT


def test_tls_verdict_requires_old_failures_and_new_h2():
    good = [('TLS1.0', False, 'SSLError'), ('TLS1.1', False, 'SSLError'),
            ('TLS1.2', True, 'tls=TLSv1.2 alpn=h2'),
            ('TLS1.3', True, 'tls=TLSv1.3 alpn=h2')]
    assert tls_verdict(good).verdict == PASS
    weak = [('TLS1.0', True, 'tls=TLSv1.0 alpn=h2'), ('TLS1.1', False, 'SSLError'),
            ('TLS1.2', True, 'tls=TLSv1.2 alpn=h2'),
            ('TLS1.3', True, 'tls=TLSv1.3 alpn=h2')]
    assert tls_verdict(weak).verdict == FAIL
    no_alpn = [('TLS1.0', False, 'SSLError'), ('TLS1.1', False, 'SSLError'),
               ('TLS1.2', True, 'tls=TLSv1.2 alpn=None'),
               ('TLS1.3', True, 'tls=TLSv1.3 alpn=h2')]
    assert tls_verdict(no_alpn).verdict == FAIL
    dead = [('TLS1.0', False, 'SSLError'), ('TLS1.1', False, 'SSLError'),
            ('TLS1.2', False, 'SSLError'), ('TLS1.3', False, 'SSLError')]
    assert tls_verdict(dead).verdict == FAIL
