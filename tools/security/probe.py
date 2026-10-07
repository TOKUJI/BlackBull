"""BLA-526 M1 robustness probe against a running local BlackBull server.

Run it with ``just vuln-check`` after ``just vuln-target-up``.  This is a
live-server harness and is deliberately outside the normal pytest run.
Safety gates, verdict semantics, and the check oracles are documented in
docs/security/probe.md; severity ranks in docs/security/severity.md.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import math
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from typing import Callable, Sequence
import urllib.parse

if __package__ in (None, ''):
    # By-path invocation: the project is non-packaged (see pyproject.toml),
    # so blackbull is importable only with the repository root on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from blackbull.client.http1 import HTTP1Client
from blackbull.fault_injection.catalogue.h1_client import (
    content_length_and_transfer_encoding,
)
from blackbull.fault_injection.scenario_h1 import (
    Abort,
    HalfClose,
    ReadResponse,
    Scenario,
    ScenarioResult,
    SendRawBytes,
)

#: The probe may only be pointed at these hosts.  Enforced in
#: [`parse_target`][] before any socket is opened; never resolve DNS.
ALLOWED_HOSTS = frozenset({'127.0.0.1', '::1', 'localhost'})

MAX_CONCURRENT_CONNECTIONS = 4
MAX_TOTAL_CONNECTIONS = 32

PASS, FAIL, TIMEOUT = 'PASS', 'FAIL', 'TIMEOUT'

#: Severity ranks exactly as docs/security/severity.md defines them.
SEVERITIES = ('Critical', 'High', 'Medium', 'Low', 'Info')

_CONNECT_TIMEOUT_S = 2.0
#: Per-check wall-clock margin over ``--check-timeout``.  The whole client
#: session — connect, exchange, teardown — runs under one ``asyncio.timeout``
#: of that total, and every scenario ends in an ``Abort`` (RST) so teardown
#: cannot block on a peer that stopped reading.
_SCENARIO_SLACK_S = 5.0

_REPORT_DIR = Path(__file__).resolve().parents[2] / 'bench' / 'results' / 'security'


class UnsafeTargetError(ValueError):
    """The target URL is not on the loopback allow-list."""


@dataclass(frozen=True)
class Target:
    host: str
    port: int


def parse_target(base_url: str) -> Target:
    """Return the loopback *Target* for *base_url*, or raise [`UnsafeTargetError`][]. """
    try:
        parts = urllib.parse.urlsplit(base_url)
    except ValueError as exc:
        raise UnsafeTargetError(f'refused: unparseable URL {base_url!r}: {exc}') from exc
    if parts.scheme != 'http':
        raise UnsafeTargetError(f'refused: scheme {parts.scheme!r} in {base_url!r} (http only)')
    if parts.username is not None or parts.password is not None:
        raise UnsafeTargetError(f'refused: userinfo in {base_url!r}')
    host = parts.hostname
    if host is None or host not in ALLOWED_HOSTS:
        raise UnsafeTargetError(
            f'refused: host {host!r} in {base_url!r} is not one of {sorted(ALLOWED_HOSTS)}')
    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeTargetError(f'refused: bad port in {base_url!r}: {exc}') from exc
    return Target(host=host, port=port if port is not None else 80)


@dataclass(frozen=True)
class Verdict:
    verdict: str
    detail: str


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    description: str
    severity: str
    verdict: str
    detail: str
    cwe: str


@dataclass(frozen=True)
class Check:
    check_id: str
    description: str
    severity: str
    cwe: str
    run: Callable[['Probe'], Verdict]
    #: Rank a hang/crash carries — High per severity.md's M1 defaults table.
    severity_on_timeout: str | None = None

    def timeout_severity(self) -> str:
        return self.severity_on_timeout or self.severity


class ConnectionBudget:
    """Bounds sockets opened per run.  Every connection must take a slot."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_CONNECTIONS,
                 max_total: int = MAX_TOTAL_CONNECTIONS) -> None:
        self._max_concurrent = max_concurrent
        self._max_total = max_total
        self._live = 0
        self._total = 0

    def acquire(self) -> None:
        if self._total >= self._max_total:
            raise RuntimeError(f'connection budget exhausted ({self._max_total} per run)')
        if self._live >= self._max_concurrent:
            raise RuntimeError(f'too many concurrent connections ({self._max_concurrent})')
        self._live += 1
        self._total += 1

    def release(self) -> None:
        self._live -= 1


class Probe:
    """Deadline-bounded connection helpers shared by every check.

    All wire access goes through [`blackbull.fault_injection.scenario_h1`][]
    steps driven by [`HTTP1Client`][blackbull.client.http1.HTTP1Client] against
    the external target, so protocol-level abuse is expressible and one
    ``asyncio.timeout`` bounds every session.
    """

    def __init__(self, target: Target, check_timeout: float) -> None:
        self.target = target
        self.check_timeout = check_timeout
        self.budget = ConnectionBudget()
        #: Narrowed per check by the runner against the run budget.
        self.effective_timeout = check_timeout

    def scenario(self, name: str, *steps) -> ScenarioResult:
        timeout = self.effective_timeout
        # Abort is always last: the executor short-circuits after it, and an
        # already-aborted transport makes the context exit's close immediate.
        steps = (*steps, Abort())

        async def _go():
            async with asyncio.timeout(timeout + _CONNECT_TIMEOUT_S
                                       + _SCENARIO_SLACK_S):
                async with HTTP1Client(
                        self.target.host, self.target.port,
                        connect_timeout=min(_CONNECT_TIMEOUT_S, timeout)) as client:
                    return await client.execute_scenario(
                        Scenario(name=name, steps=steps))

        self.budget.acquire()
        try:
            return asyncio.run(_go())
        finally:
            self.budget.release()

    def raw_request(self, name: str, raw: bytes, *, half_close: bool = False) -> ScenarioResult:
        steps = [SendRawBytes(raw)]
        if half_close:
            steps.append(HalfClose())
        steps.append(ReadResponse(timeout=self.effective_timeout))
        return self.scenario(name, *steps)


def _get_request(path: str) -> bytes:
    return f'GET {path} HTTP/1.1\r\nHost: probe\r\nConnection: close\r\n\r\n'.encode('latin-1')


def _describe(result: ScenarioResult, timeout: float) -> str:
    if result.timed_out:
        return f'no response within {timeout:g}s'
    if result.response is not None:
        body = result.response.body
        return f'status={result.response.status} body={bytes(body)[:64]!r}'
    return f'connection closed without a response ({result.exception or "EOF"})'


def _reject_or_timeout(result: ScenarioResult, timeout: float) -> Verdict | None:
    if result.timed_out:
        return Verdict(TIMEOUT, f'no response within {timeout:g}s')
    return None


#: Acceptable rejection statuses, per check oracle (docs/security/probe.md).
_ACCEPT_4XX = frozenset(range(400, 500))
_ACCEPT_METHOD = _ACCEPT_4XX | {501, 505}
_ACCEPT_400 = frozenset({400})


def _abuse_oracle(result: ScenarioResult, timeout: float,
                  accept: frozenset[int], expected: str) -> Verdict:
    """PASS only on an accepted rejection status or connection close."""
    early = _reject_or_timeout(result, timeout)
    if early is not None:
        return early
    if result.response is None:
        return Verdict(PASS, 'rejected by connection close')
    status = result.response.status
    if status in accept:
        return Verdict(PASS, f'rejected with {status}')
    return Verdict(FAIL, f'accepted abuse with {status} (expected {expected} or close)')


# ------------------------------------------------------------------
# Checks
# ------------------------------------------------------------------

def _baseline_001(probe: Probe) -> Verdict:
    result = probe.raw_request('baseline-001', _get_request('/'))
    early = _reject_or_timeout(result, probe.effective_timeout)
    if early is not None:
        return early
    if result.response is not None and result.response.status == 200 \
            and bytes(result.response.body) == b'ok':
        return Verdict(PASS, '200, body "ok"')
    return Verdict(FAIL, _describe(result, probe.effective_timeout))


def _baseline_002(probe: Probe) -> Verdict:
    result = probe.raw_request('baseline-002', _get_request('/json'))
    early = _reject_or_timeout(result, probe.effective_timeout)
    if early is not None:
        return early
    resp = result.response
    if resp is not None and resp.status == 200:
        try:
            parsed = json.loads(bytes(resp.body))
        except ValueError as exc:
            return Verdict(FAIL, f'200 but body is not JSON: {exc}')
        if parsed == {'ok': True}:
            return Verdict(PASS, '200, JSON {"ok": true}')
        return Verdict(FAIL, f'200 JSON differs: {parsed!r}')
    return Verdict(FAIL, _describe(result, probe.effective_timeout))


def _baseline_003(probe: Probe) -> Verdict:
    result = probe.raw_request('baseline-003', _get_request('/'))
    early = _reject_or_timeout(result, probe.effective_timeout)
    if early is not None:
        return early
    if result.response is not None and result.response.status == 200 \
            and bytes(result.response.body) == b'ok':
        return Verdict(PASS, '200, body "ok" — server survived all probes')
    return Verdict(FAIL, f'server degraded after probes: '
                         f'{_describe(result, probe.effective_timeout)}')


def _h1_robust_001(probe: Probe) -> Verdict:
    raw = b'FOO / HTTP/1.1\r\nHost: probe\r\nConnection: close\r\n\r\n'
    return _abuse_oracle(probe.raw_request('h1-robust-001', raw),
                         probe.effective_timeout, _ACCEPT_METHOD, '4xx/501/505')


def _h1_robust_002(probe: Probe) -> Verdict:
    raw = (b'GET / HTTP/1.1\r\nHost: probe\r\nX-Oversized: '
           + b'a' * 100_000
           + b'\r\nConnection: close\r\n\r\n')
    return _abuse_oracle(probe.raw_request('h1-robust-002', raw),
                         probe.effective_timeout, _ACCEPT_4XX, '4xx')


def _h1_robust_003(probe: Probe) -> Verdict:
    raw = b'\x00\x01garbage\r\n\r\n'
    return _abuse_oracle(probe.raw_request('h1-robust-003', raw),
                         probe.effective_timeout, _ACCEPT_400, '400')


#: (label, request head) — the CRLF cases of H1-ROBUST-004.  A is the
#: spec-literal pair of field lines; B puts a bare CR inside a field value,
#: the RFC 9110 §5.5 "MUST reject or replace with SP" case.
_CRLF_VARIANTS = (
    ('crlf',
     b'GET /echo-headers HTTP/1.1\r\nHost: probe\r\n'
     b'X-Probe: value\r\nX-Injected: 1\r\nConnection: close\r\n\r\n'),
    ('bare-cr',
     b'GET /echo-headers HTTP/1.1\r\nHost: probe\r\n'
     b'X-Probe: value\rX-Injected: 1\r\nConnection: close\r\n\r\n'),
)


def _crlf_variant_verdict(probe: Probe, label: str, raw: bytes) -> Verdict:
    timeout = probe.effective_timeout
    result = probe.raw_request('h1-robust-004', raw)
    early = _reject_or_timeout(result, timeout)
    if early is not None:
        return Verdict(early.verdict, f'{label}: {early.detail}')
    resp = result.response
    if resp is None:
        return Verdict(PASS, f'{label}: rejected by connection close')
    if resp.status == 400:
        return Verdict(PASS, f'{label}: rejected with 400')
    if resp.status < 200 or resp.status >= 300:
        return Verdict(FAIL, f'{label}: unexpected status {resp.status} '
                             f'(expected 400, close, or an opaque 2xx)')
    if any(name.lower() == b'x-injected' for name, _ in resp.headers):
        return Verdict(FAIL, f'{label}: injected response header smuggled (CWE-113)')
    try:
        echoed = json.loads(bytes(resp.body))['headers']
        if not (isinstance(echoed, list)
                and all(isinstance(item, list) and len(item) == 2
                        and all(isinstance(part, str) for part in item)
                        for item in echoed)):
            raise TypeError('not a list of [name, value] string pairs')
        names = [name.lower() for name, _ in echoed]
        values = [value for _, value in echoed]
    except (ValueError, KeyError, TypeError):
        return Verdict(FAIL, f'{label}: {resp.status} but echo body is not a header list')
    if any('\r' in value or '\n' in value for value in values):
        return Verdict(FAIL, f'{label}: echoed value kept CR/LF (RFC 9110 §5.5)')
    if 'x-injected' in names and label == 'bare-cr':
        return Verdict(FAIL, f'{label}: bare CR split the value into a second field '
                             f'line (smuggle primitive, CWE-113)')
    if 'x-probe' in names and 'x-injected' not in names:
        return Verdict(PASS, f'{label}: value kept opaque ({resp.status})')
    return Verdict(PASS, f'{label}: split into separate field lines, '
                         f'response unharmed ({resp.status})')


def _h1_robust_004(probe: Probe) -> Verdict:
    verdicts = [_crlf_variant_verdict(probe, label, raw)
                for label, raw in _CRLF_VARIANTS]
    detail = '; '.join(v.detail for v in verdicts)
    for wanted in (FAIL, TIMEOUT):
        if any(v.verdict == wanted for v in verdicts):
            return Verdict(wanted, detail)
    return Verdict(PASS, detail)


def _h1_robust_005(probe: Probe) -> Verdict:
    raw = (b'POST /echo-body HTTP/1.1\r\nHost: probe\r\nContent-Length: 100\r\n'
           b'Content-Type: application/octet-stream\r\n\r\nshort')
    return _abuse_oracle(probe.raw_request('h1-robust-005', raw, half_close=True),
                         probe.effective_timeout, _ACCEPT_4XX, '4xx')


def _smuggle_001(probe: Probe) -> Verdict:
    # Reuses the fault-injection catalogue case verbatim, retimed to the
    # probe's bound (see docs/security/probe.md).
    catalogued = content_length_and_transfer_encoding()
    steps = tuple(
        ReadResponse(timeout=probe.effective_timeout) if isinstance(step, ReadResponse) else step
        for step in catalogued.steps)
    return _abuse_oracle(probe.scenario('smuggle-001', *steps),
                         probe.effective_timeout, _ACCEPT_400, '400')


_TRAVERSAL_PATHS = (
    '/static/../fixture_app.py',
    '/static/%2e%2e/fixture_app.py',
)

_HELLO_BODY = b'hello from the BlackBull static fixture\n'


def _static_001(probe: Probe) -> Verdict:
    timeout = probe.effective_timeout
    details = []
    verdict = PASS
    for path in _TRAVERSAL_PATHS:
        result = probe.raw_request('static-001', _get_request(path))
        early = _reject_or_timeout(result, timeout)
        if early is not None:
            return Verdict(early.verdict, f'{path}: {early.detail}')
        resp = result.response
        if resp is None:
            details.append(f'{path}: closed without a response')
            continue
        status = resp.status
        body = bytes(resp.body)
        if status in (400, 403, 404):
            details.append(f'{path}: rejected with {status}')
        elif status == 200 and body != _HELLO_BODY:
            verdict = FAIL
            details.append(f'{path}: 200 leaked file content {body[:32]!r} (CWE-22)')
        elif status == 200:
            verdict = FAIL
            details.append(f'{path}: 200 served a file on a traversal path (CWE-22)')
        else:
            verdict = FAIL
            details.append(f'{path}: unexpected status {status}')
    return Verdict(verdict, '; '.join(details))


#: HDR-001 oracle: the ``server`` response header must not contain a
#: filesystem path or a Python version.  Patterns, verbatim: a POSIX path
#: under a system-ish root, a Windows drive path, site/dist-packages, or
#: any of "python", "cpython", "py/<digit>".
_HDR_PATH_LEAK = re.compile(
    r'(?i)(?:/(?:home|users|usr|var|etc|opt|tmp|root|srv|app|workspace)(?:/|\b))'
    r'|(?:[a-z]:\\)'
    r'|(?:site-packages|dist-packages)')
_HDR_PY_LEAK = re.compile(r'(?i)(?:python|cpython|py/\d)')


def _hdr_001(probe: Probe) -> Verdict:
    result = probe.raw_request('hdr-001', _get_request('/'))
    early = _reject_or_timeout(result, probe.effective_timeout)
    if early is not None:
        return early
    resp = result.response
    if resp is None or resp.status != 200:
        return Verdict(FAIL, f'no 200 from GET /: '
                             f'{_describe(result, probe.effective_timeout)}')
    server_headers = [value.decode('latin-1')
                      for name, value in resp.headers if name.lower() == b'server']
    if not server_headers:
        return Verdict(PASS, 'no server: header present')
    for value in server_headers:
        if _HDR_PATH_LEAK.search(value) or _HDR_PY_LEAK.search(value):
            return Verdict(FAIL, f'server: {value[:64]!r} leaks internals (CWE-200)')
    return Verdict(PASS, f'server: {server_headers[0][:64]!r} carries no path or Python detail')


CHECKS: tuple[Check, ...] = (
    Check('BASELINE-001', 'GET / returns 200 with body "ok"', 'High', 'CWE-400', _baseline_001),
    Check('BASELINE-002', 'GET /json returns 200 with JSON {"ok": true}', 'High', 'CWE-400', _baseline_002),
    Check('H1-ROBUST-001', 'unknown method FOO answered with 4xx/501 or close', 'Info', 'CWE-755', _h1_robust_001, 'High'),
    Check('H1-ROBUST-002', '100 KiB header value answered with 4xx or close', 'Medium', 'CWE-400', _h1_robust_002, 'High'),
    Check('H1-ROBUST-003', 'garbage request-line bytes answered with 400 or close', 'Info', 'CWE-755', _h1_robust_003, 'High'),
    Check('H1-ROBUST-004', 'CRLF in a header value rejected, sanitized, or kept opaque (RFC 9110 §5.5)', 'High', 'CWE-113', _h1_robust_004),
    Check('H1-ROBUST-005', 'Content-Length with truncated body then FIN answered with 4xx/408 or close', 'Info', 'CWE-755', _h1_robust_005, 'High'),
    Check('SMUGGLE-001', 'Content-Length and Transfer-Encoding together rejected (RFC 9112 §6.3)', 'High', 'CWE-444', _smuggle_001),
    Check('STATIC-001', 'traversal via /static/../ and %2e%2e/ never serves files outside the root', 'High', 'CWE-22', _static_001),
    Check('HDR-001', 'server: header carries no filesystem path or Python version', 'Low', 'CWE-200', _hdr_001),
    Check('BASELINE-003', 'GET / still returns 200 after every probe', 'High', 'CWE-400', _baseline_003),
)


# ------------------------------------------------------------------
# Runner and reporting
# ------------------------------------------------------------------

def run_checks(probe: Probe, checks: Sequence[Check] = CHECKS,
               run_timeout: float = 120.0) -> list[CheckResult]:
    deadline = time.monotonic() + run_timeout
    results: list[CheckResult] = []
    for check in checks:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append(CheckResult(check.check_id, check.description,
                                       check.timeout_severity(), TIMEOUT,
                                       'run budget exhausted', check.cwe))
            continue
        probe.effective_timeout = min(probe.check_timeout, remaining)
        try:
            verdict = check.run(probe)
        except (TimeoutError, asyncio.TimeoutError):
            verdict = Verdict(TIMEOUT,
                              f'no result within {probe.effective_timeout:g}s')
        except Exception as exc:  # noqa: BLE001 — a probe bug must not kill the run
            verdict = Verdict(FAIL, f'probe error: {exc!r}')
        severity = (check.timeout_severity() if verdict.verdict == TIMEOUT
                    else check.severity)
        results.append(CheckResult(check.check_id, check.description, severity,
                                   verdict.verdict, _one_line(verdict.detail),
                                   check.cwe))
    return results


def _one_line(text: str) -> str:
    return text.replace('|', '/').replace('\r', ' ').replace('\n', ' ').strip()


def render_table(results: Sequence[CheckResult]) -> str:
    rows = [(r.check_id, r.severity, r.verdict, r.detail) for r in results]
    header = ('check', 'severity', 'verdict', 'detail')
    widths = [max(len(str(row[i])) for row in (header, *rows)) for i in range(4)]
    lines = [
        '  '.join(h.ljust(w) for h, w in zip(header, widths)),
        '  '.join('-' * w for w in widths),
    ]
    lines.extend('  '.join(str(cell).ljust(w) for cell, w in zip(row, widths))
                 for row in rows)
    return '\n'.join(lines)


def render_markdown(results: Sequence[CheckResult], *,
                    base_url: str, check_timeout: float, run_timeout: float,
                    timestamp: str) -> str:
    lines = [
        f'# BLA-526 M1 robustness probe — {timestamp}',
        '',
        f'- Target: {base_url}',
        f'- Bounds: check timeout {check_timeout:g}s, run cap {run_timeout:g}s, '
        f'connections {MAX_CONCURRENT_CONNECTIONS} concurrent / {MAX_TOTAL_CONNECTIONS} per run',
        '- Verdicts: PASS = mechanical oracle held; FAIL = oracle violated; '
        'TIMEOUT = no answer within the bound.',
        '- Severity is the rank a failure of that check carries '
        '(docs/security/severity.md). Recording of findings: docs/security/probe.md.',
        '',
        '| Check | Severity | Verdict | Detail | CWE |',
        '|---|---|---|---|---|',
    ]
    for r in results:
        lines.append(f'| {r.check_id} | {r.severity} | {r.verdict} | {r.detail} | {r.cwe} |')
    lines.append('')
    return '\n'.join(lines)


def write_report(results: Sequence[CheckResult], *, base_url: str,
                 check_timeout: float, run_timeout: float,
                 timestamp: str, out_dir: Path | None = None) -> Path:
    out = out_dir if out_dir is not None else _REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / f'{timestamp}.md'
    path.write_text(
        render_markdown(results, base_url=base_url, check_timeout=check_timeout,
                        run_timeout=run_timeout, timestamp=timestamp),
        encoding='utf-8')
    return path


def exit_code(results: Sequence[CheckResult]) -> int:
    return 0 if all(r.verdict == PASS for r in results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='probe.py',
        description='BLA-526 M1 robustness probe for a running local BlackBull server')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000',
                        help='target base URL (loopback hosts only)')
    parser.add_argument('--check-timeout', type=float, default=5.0,
                        help='per-check timeout in seconds (default 5)')
    parser.add_argument('--run-timeout', type=float, default=120.0,
                        help='overall run cap in seconds (default 120)')
    args = parser.parse_args(argv)
    if not all(math.isfinite(t) and t > 0
               for t in (args.check_timeout, args.run_timeout)):
        parser.error('timeouts must be finite and positive')
    try:
        target = parse_target(args.base_url)
    except UnsafeTargetError as exc:
        print(f'probe: {exc}', file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc)
    timestamp = started.strftime('%Y%m%dT%H%M%SZ')
    probe = Probe(target, args.check_timeout)
    results = run_checks(probe, run_timeout=args.run_timeout)

    print(f'# {timestamp} target={args.base_url} '
          f'check-timeout={args.check_timeout:g}s run-timeout={args.run_timeout:g}s')
    print(render_table(results))
    report = write_report(results, base_url=args.base_url,
                          check_timeout=args.check_timeout,
                          run_timeout=args.run_timeout, timestamp=timestamp)
    print(f'report: {report}')
    return exit_code(results)


if __name__ == '__main__':
    sys.exit(main())
