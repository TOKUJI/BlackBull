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
    FAIL,
    LANES,
    PASS,
    SEVERITIES,
    TIMEOUT,
    Check,
    CheckResult,
    H2Info,
    Lane,
    UnsafeTargetError,
    Verdict,
    WsAttempt,
    abuse_accept_verdict,
    checks_for,
    exit_code,
    expect_verdict,
    h2_error_verdict,
    h2_info,
    h2_ok_verdict,
    h2_recover_verdict,
    h2_settings,
    main,
    parse_target,
    rapid_reset_verdict,
    range_verdict,
    render_markdown,
    render_table,
    run_checks,
    split_interims,
    state_verdict,
    tls_verdict,
    trailer_followup_verdict,
    ws_bad_handshake_verdict,
    ws_flood_verdict,
    ws_head_parse,
    ws_mask_verdict,
    ws_scan_frames,
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


# ------------------------------------------------------------------
# M4 judges — WebSocket
# ------------------------------------------------------------------


def _ws_attempt(status=101, frames=b'', closed=True, timed_out=False):
    return WsAttempt(name='t', status=status, accept=b'a', head=b'', frames=frames,
                     closed=closed, timed_out=timed_out)


def test_ws_head_parse_status_and_accept():
    head = (b'HTTP/1.1 101 Switching Protocols\r\n'
            b'Upgrade: websocket\r\nSec-WebSocket-Accept: xyz==\r\n\r\n')
    assert ws_head_parse(head) == (101, b'xyz==')
    assert ws_head_parse(b'HTTP/1.1 400 Bad Request\r\n\r\n') == (400, b'')
    assert ws_head_parse(b'garbage') == (None, b'')


def test_ws_scan_frames_reads_close_codes_and_echo():
    close1002 = b'\x88\x02\x03\xea'
    echo = b'\x81\x01X'
    assert ws_scan_frames(close1002) == ((1002,), False, False)
    assert ws_scan_frames(echo) == ((), True, False)
    assert ws_scan_frames(b'\x88\x02\x03\xe8') == ((1000,), False, False)
    assert ws_scan_frames(b'\x81\x81\x37\xfa\x21\x3d\x7f') == ((), False, True)
    assert ws_scan_frames(b'\x81') == ((), False, False)  # truncated tail ignored


def test_ws_mask_verdict_oracles():
    assert ws_mask_verdict(_ws_attempt(frames=b'\x88\x02\x03\xea'), 5.0).verdict == PASS
    assert ws_mask_verdict(_ws_attempt(closed=True), 5.0).verdict == PASS
    echoed = ws_mask_verdict(_ws_attempt(frames=b'\x81\x01X'), 5.0)
    assert echoed.verdict == FAIL and 'echoed' in echoed.detail
    wrong = ws_mask_verdict(_ws_attempt(frames=b'\x88\x02\x03\xe8'), 5.0)
    assert wrong.verdict == FAIL and '1000' in wrong.detail
    untested = ws_mask_verdict(_ws_attempt(status=400), 5.0)
    assert untested.verdict == FAIL and 'untested' in untested.detail
    open_conn = ws_mask_verdict(_ws_attempt(closed=False), 5.0)
    assert open_conn.verdict == FAIL


def test_ws_bad_handshake_verdict_rejects_101():
    ok = ws_bad_handshake_verdict(_ws_attempt(status=400), 5.0,
                                  allow=frozenset({400, 426}))
    assert ok.verdict == PASS
    assert ws_bad_handshake_verdict(_ws_attempt(status=101), 5.0,
                                    allow=frozenset({400})).verdict == FAIL
    closed = ws_bad_handshake_verdict(_ws_attempt(status=None), 5.0,
                                      allow=frozenset({400}))
    assert closed.verdict == PASS


def test_ws_flood_verdict_accepts_upgrade_or_refusal():
    assert ws_flood_verdict(_ws_attempt(status=101), 5.0).verdict == PASS
    assert ws_flood_verdict(_ws_attempt(status=431), 5.0).verdict == PASS
    assert ws_flood_verdict(_ws_attempt(status=None), 5.0).verdict == PASS
    assert ws_flood_verdict(_ws_attempt(status=500), 5.0).verdict == FAIL
    stalled = ws_flood_verdict(_ws_attempt(status=None, closed=False,
                                           timed_out=True), 5.0)
    assert stalled.verdict == TIMEOUT


# ------------------------------------------------------------------
# M4 judges — H1 trailers / Range / Expect
# ------------------------------------------------------------------


def test_trailer_followup_verdict_detects_smuggling():
    ok_body = b'{"headers": [["host", "probe"]]}'
    clean = trailer_followup_verdict(((200, b'Z'), (200, ok_body)), 'timeout')
    assert clean.verdict == PASS
    smuggled = trailer_followup_verdict(
        ((200, b'Z'), (200, b'{"headers": [["x-smuggle", "1"]]}')), 'timeout')
    assert smuggled.verdict == FAIL and 'smuggled' in smuggled.detail
    assert trailer_followup_verdict(((200, b'Z'), (500, b'x')), 'timeout').verdict == FAIL
    assert trailer_followup_verdict(((200, b'Z'), (200, b'Z'), (200, ok_body)),
                                    'timeout').verdict == FAIL
    assert trailer_followup_verdict(((200, b'Z'),), 'closed').verdict == PASS
    assert trailer_followup_verdict((), 'timeout').verdict == TIMEOUT
    assert trailer_followup_verdict(((200, b'Z'),), 'timeout').verdict == FAIL


def test_range_verdict_shapes():
    hello = b'hello from the BlackBull static fixture\n'
    size = len(hello)
    full = range_verdict(200, [], hello, size=size, closed=False, timed_out=False)
    assert full.verdict == PASS
    wrong_body = range_verdict(200, [], b'nope', size=size, closed=False,
                               timed_out=False)
    assert wrong_body.verdict == FAIL
    ok206 = range_verdict(
        206, [(b'content-range', b'bytes 1-3/40'), (b'content-type', b'text/plain')],
        b'ell', size=size, closed=False, timed_out=False)
    assert ok206.verdict == PASS
    bad_len = range_verdict(
        206, [(b'content-range', b'bytes 1-3/40')], b'el', size=size,
        closed=False, timed_out=False)
    assert bad_len.verdict == FAIL
    out_of_file = range_verdict(
        206, [(b'content-range', b'bytes 1-3/99')], b'ell', size=size,
        closed=False, timed_out=False)
    assert out_of_file.verdict == FAIL
    multipart = range_verdict(
        206, [(b'content-type', b'multipart/byteranges; boundary=x')],
        b'--x', size=size, closed=False, timed_out=False)
    assert multipart.verdict == PASS
    ok416 = range_verdict(416, [], b'', size=size, closed=False, timed_out=False)
    assert ok416.verdict == PASS
    bad416 = range_verdict(416, [(b'content-range', b'bytes 0-1/40')], b'',
                           size=size, closed=False, timed_out=False)
    assert bad416.verdict == FAIL
    assert range_verdict(500, [], b'', size=size, closed=False,
                         timed_out=False).verdict == FAIL
    assert range_verdict(None, [], b'', size=size, closed=True,
                         timed_out=False).verdict == FAIL
    assert range_verdict(None, [], b'', size=size, closed=False,
                         timed_out=True).verdict == TIMEOUT


def test_split_interims_and_expect_verdict():
    assert split_interims([(100, b''), (200, b'hello'), (200, b'ok')]) == (
        ((100, b''),), ((200, b'hello'), (200, b'ok')))
    good = expect_verdict(((100, b''), (200, b'hello'), (200, b'ok')), 'timeout',
                          ok_body=b'hello')
    assert good.verdict == PASS and '100-then' in good.detail
    no_interim = expect_verdict(((200, b'hello'), (200, b'ok')), 'timeout',
                                ok_body=b'hello')
    assert no_interim.verdict == PASS and 'no 100' in no_interim.detail
    rejected = expect_verdict(((417, b'x'), (200, b'ok')), 'timeout',
                              ok_body=b'hello')
    assert rejected.verdict == PASS
    weird_interim = expect_verdict(((103, b''), (200, b'hello')), 'timeout',
                                   ok_body=b'hello')
    assert weird_interim.verdict == FAIL
    wrong_body = expect_verdict(((200, b'wrong'), (200, b'ok')), 'timeout',
                                ok_body=b'hello')
    assert wrong_body.verdict == FAIL
    dropped = expect_verdict(((200, b'ok'),), 'timeout', ok_body=b'hello')
    assert dropped.verdict == FAIL


# ------------------------------------------------------------------
# M4 judges — H2
# ------------------------------------------------------------------


def _h2_fake(frame_type, stream_id=None, error_code=None, body=b''):
    return H2Info(kind=frame_type, stream_id=stream_id, error_code=error_code,
                  body=body)


def test_h2_settings_parses_advertised_parameters():
    payload = (3).to_bytes(2, 'big') + (100).to_bytes(4, 'big')
    payload += (5).to_bytes(2, 'big') + (16384).to_bytes(4, 'big')
    settings = h2_settings([_h2_fake('SETTINGS', 0, body=payload),
                            _h2_fake('SETTINGS', 0)])
    assert settings == {3: 100, 5: 16384}
    assert h2_settings([_h2_fake('HEADERS', 1)]) == {}


def test_h2_recover_verdict_accepts_completion_or_refusal():
    done = [_h2_fake('HEADERS', 3, body=b''), ]
    done[0] = H2Info(kind='HEADERS', stream_id=3, status=200)
    assert h2_recover_verdict(done, 3, accept_codes=frozenset(),
                              accept_statuses=frozenset({200}),
                              expected='x').verdict == PASS
    refused = [_h2_fake('GOAWAY', 0, error_code=11)]
    assert h2_recover_verdict(refused, 3, accept_codes=frozenset({11}),
                              accept_statuses=frozenset({200}),
                              expected='x').verdict == PASS
    wrong_code = [_h2_fake('GOAWAY', 0, error_code=2)]
    assert h2_recover_verdict(wrong_code, 3, accept_codes=frozenset({11}),
                              accept_statuses=frozenset({200}),
                              expected='x').verdict == FAIL
    bad_status = [H2Info(kind='HEADERS', stream_id=3, status=500)]
    assert h2_recover_verdict(bad_status, 3, accept_codes=frozenset({11}),
                              accept_statuses=frozenset({200}),
                              expected='x').verdict == FAIL
    assert h2_recover_verdict([_h2_fake('EOF')], 3, accept_codes=frozenset({11}),
                              accept_statuses=frozenset({200}),
                              expected='x').verdict == PASS
    assert h2_recover_verdict([], 3, accept_codes=frozenset({11}),
                              accept_statuses=frozenset({200}),
                              expected='x').verdict == TIMEOUT


def test_rapid_reset_verdict_flags_illegal_error_codes():
    ok = [_h2_fake('RST_STREAM', 1, error_code=8), _h2_fake('GOAWAY', 0, error_code=0)]
    assert rapid_reset_verdict(ok, 41, 20).verdict == PASS
    bad = [_h2_fake('GOAWAY', 0, error_code=1)]
    verdict = rapid_reset_verdict(bad, 41, 20)
    assert verdict.verdict == FAIL and 'PROTOCOL_ERROR' in verdict.detail
    assert rapid_reset_verdict([], 41, 20).verdict == PASS


def test_h2_bomb_stays_within_its_declared_cap():
    import hpack
    from tools.security.probe import (
        _H2_BOMB_CAP, _H2_BOMB_NAME, _H2_BOMB_REFS, _H2_BOMB_VALUE)
    enc = hpack.Encoder()
    fields = ((':method', 'GET'), (':path', '/'),
              (':scheme', 'http'), (':authority', '127.0.0.1:8443'))
    enc.encode(list(fields) + [(_H2_BOMB_NAME, _H2_BOMB_VALUE)])
    bomb = enc.encode(list(fields) + [(_H2_BOMB_NAME, _H2_BOMB_VALUE)] * _H2_BOMB_REFS)
    decoded = hpack.Decoder(max_header_list_size=_H2_BOMB_CAP + 1).decode(bomb)
    total = sum(len(n) + len(v) for n, v in decoded)
    assert len(bomb) < 8192
    assert total <= _H2_BOMB_CAP


def test_h2_continuation_steps_cut_one_block_into_thirty_one():
    from tools.security.probe import Probe, Target, _h2_continuation_steps
    probe = Probe(Target(scheme='http', host='127.0.0.1', port=8443), 5.0)
    steps = _h2_continuation_steps(probe, stream_id=1, continuations=30)
    assert len(steps) == 31
    kinds = []
    for step in steps:
        raw = step.data
        frame_type, flags = raw[3], raw[4]
        stream = int.from_bytes(raw[5:9], 'big')
        assert stream == 1
        kinds.append((frame_type, flags))
    assert kinds[0] == (0x01, 0x01)            # HEADERS, END_STREAM
    assert all(t == 0x09 and f == 0x00 for t, f in kinds[1:-1])
    assert kinds[-1] == (0x09, 0x04)           # CONTINUATION, END_HEADERS
    import hpack
    block = b''.join(s.data[9:9 + int.from_bytes(s.data[0:3], 'big')]
                     for s in steps)
    fields = hpack.Decoder().decode(block)
    assert (':method', 'GET') in fields


def test_m4_checks_escalate_hangs_to_high():
    by_id = {check.check_id: check for check in CHECKS}
    for check_id in ('RANGE-001', 'EXPECT-001', 'HOST-001', 'WS-001',
                     'H2-ROBUST-006', 'H2-ROBUST-007', 'H2-ROBUST-008'):
        assert by_id[check_id].timeout_severity() == 'High'
    assert by_id['SYMLINK-001'].severity == 'Critical'
    assert by_id['TRAILER-001'].timeout_severity() == 'High'
