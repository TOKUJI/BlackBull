"""Unit tests for the BLA-526 probe's pure parts: safety gate and report writer.

The checks themselves need a live server (``just vuln-check``) and are never
run under pytest; pytest.ini keeps tools/ out of collection entirely.
"""
from __future__ import annotations

import pytest

from tools.security.probe import (
    ALLOWED_HOSTS,
    CHECKS,
    PASS,
    SEVERITIES,
    TIMEOUT,
    FAIL,
    CheckResult,
    UnsafeTargetError,
    exit_code,
    main,
    parse_target,
    render_markdown,
    render_table,
    write_report,
)

_LOOPBACK_URLS = (
    'http://127.0.0.1:8000',
    'http://localhost:8000',
    'http://[::1]:8000',
    'http://127.0.0.1',
    'http://LOCALHOST:9000/probe',
)

_REFUSED_URLS = (
    'http://example.com:8000',
    'http://10.0.0.1:8000',
    'http://192.168.1.1:8000',
    'http://127.0.0.1.evil.example:8000',
    'http://0x7f000001:8000',
    'http://user:pass@127.0.0.1:8000',
    'https://127.0.0.1:8000',
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


def test_main_exits_2_on_refused_target_without_io():
    assert main(['--base-url', 'http://example.com:8000']) == 2


def test_main_exits_2_on_malformed_target():
    assert main(['--base-url', 'http://[::1']) == 2


def test_main_rejects_non_finite_timeouts():
    with pytest.raises(SystemExit) as excinfo:
        main(['--check-timeout', 'nan'])
    assert excinfo.value.code == 2


def test_hang_escalates_h1_robust_checks_to_high():
    by_id = {check.check_id: check for check in CHECKS}
    for check_id in ('H1-ROBUST-001', 'H1-ROBUST-002',
                     'H1-ROBUST-003', 'H1-ROBUST-005'):
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


def test_render_markdown_contains_check_rows():
    md = render_markdown(_results(), base_url='http://127.0.0.1:8000',
                         check_timeout=5.0, run_timeout=120.0,
                         timestamp='20260101T000000Z')
    for row in _results():
        assert f'| {row.check_id} | {row.severity} | {row.verdict} |' in md
    assert 'CWE-444' in md


def test_write_report_writes_markdown_rows(tmp_path):
    path = write_report(_results(), base_url='http://127.0.0.1:8000',
                        check_timeout=5.0, run_timeout=120.0,
                        timestamp='20260101T000000Z', out_dir=tmp_path)
    assert path == tmp_path / '20260101T000000Z.md'
    text = path.read_text(encoding='utf-8')
    assert '| BASELINE-001 | High | PASS |' in text
    assert '| SMUGGLE-001 | High | FAIL |' in text
    assert '| H1-ROBUST-002 | Medium | TIMEOUT |' in text


def test_exit_code_maps_verdicts():
    results = _results()
    assert exit_code([results[0]]) == 0
    assert exit_code(results) == 1


def test_check_registry_uses_severity_vocabulary():
    ids = [check.check_id for check in CHECKS]
    assert len(ids) == len(set(ids))
    for check in CHECKS:
        assert check.severity in SEVERITIES
        assert (check.severity_on_timeout or check.severity) in SEVERITIES
        assert check.description
        assert check.cwe.startswith('CWE-')
    assert ids[-1] == 'BASELINE-003'
