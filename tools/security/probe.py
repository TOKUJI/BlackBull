"""BLA-526 M2/M3 robustness probe against running local BlackBull servers.

Run it with ``just vuln-check`` after ``just vuln-target-up``.  This is a
live-server harness and is deliberately outside the normal pytest run.
Two lanes share the runner: HTTP/1.1 against ``--base-url`` and HTTP/2 over
TLS against ``--h2-url``.  Safety gates, verdict semantics, and the check
oracles are documented in docs/security/probe.md; severity ranks in
docs/security/severity.md.
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
import ssl
import sys
import time
from typing import Callable, Sequence
import urllib.parse
import warnings

if __package__ in (None, ''):
    # By-path invocation: the project is non-packaged (see pyproject.toml),
    # so blackbull is importable only with the repository root on sys.path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import hpack

from blackbull.client.http1 import HTTP1Client
from blackbull.client.http2 import HTTP2Client
from blackbull.fault_injection.catalogue.h1_client import (
    content_length_and_transfer_encoding,
    two_content_lengths,
)
from blackbull.fault_injection import scenario_h2_client as h2s
from blackbull.fault_injection.scenario_h1 import (
    Abort,
    HalfClose,
    ReadResponse,
    Scenario,
    ScenarioResult,
    SendRawBytes,
    Sleep,
)
from blackbull.protocol.frame_types import ErrorCodes, FrameTypes

#: The probe may only be pointed at these hosts.  Enforced in
#: [`parse_target`][] before any socket is opened; never resolve DNS.
ALLOWED_HOSTS = frozenset({'127.0.0.1', '::1', 'localhost'})

#: Schemes the runner speaks.  ``https`` carries the HTTP/2 lane (ALPN ``h2``).
ALLOWED_SCHEMES = frozenset({'http', 'https'})

MAX_CONCURRENT_CONNECTIONS = 4
MAX_TOTAL_CONNECTIONS = 96

PASS, FAIL, TIMEOUT = 'PASS', 'FAIL', 'TIMEOUT'

#: Severity ranks exactly as docs/security/severity.md defines them.
SEVERITIES = ('Critical', 'High', 'Medium', 'Low', 'Info')

#: Lane names, in run order.  ``h1`` is HTTP/1.1 over cleartext; ``h2`` is
#: HTTP/2 over TLS (or h2c when its target URL is ``http``).
LANES = ('h1', 'h2')

_CONNECT_TIMEOUT_S = 2.0
#: Per-check wall-clock margin over ``--check-timeout``.  The whole client
#: session — connect, exchange, teardown — runs under one ``asyncio.timeout``
#: of that total, and every scenario ends in an ``Abort`` (RST) so teardown
#: cannot block on a peer that stopped reading.
_SCENARIO_SLACK_S = 5.0
#: The short "is another response coming?" read that closes a pipelined
#: exchange.  Long enough for a local answer, short enough that a clean
#: no-more-responses outcome does not cost a full check timeout.
_TAIL_TIMEOUT_S = 1.0
#: H1-ROBUST-011's total hold; the check is gentle by construction.
_SLOW_HOLD_MAX_S = 5.0

#: Well-known certificate published by tools/security/fixture_app.py at
#: startup; the probe verifies the TLS lane against it instead of disabling
#: certificate checks.
DEFAULT_TLS_CA = '/tmp/bb-vuln-target-tls/cert.pem'

_REPORT_DIR = Path(__file__).resolve().parents[2] / 'bench' / 'results' / 'security'


class UnsafeTargetError(ValueError):
    """The target URL is not on the loopback allow-list."""


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int


def parse_target(base_url: str) -> Target:
    """Return the loopback *Target* for *base_url*, or raise [`UnsafeTargetError`][]. """
    try:
        parts = urllib.parse.urlsplit(base_url)
    except ValueError as exc:
        raise UnsafeTargetError(f'refused: unparseable URL {base_url!r}: {exc}') from exc
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeTargetError(
            f'refused: scheme {parts.scheme!r} in {base_url!r} '
            f'(one of {sorted(ALLOWED_SCHEMES)} only)')
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
    default_port = 443 if parts.scheme == 'https' else 80
    return Target(scheme=parts.scheme, host=host,
                  port=port if port is not None else default_port)


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
    lane: str
    #: Rank a hang/crash carries — High per severity.md's defaults table.
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

    HTTP/1.1 wire access goes through [`blackbull.fault_injection.scenario_h1`][]
    steps driven by [`HTTP1Client`][blackbull.client.http1.HTTP1Client]; the
    HTTP/2 lane through `scenario_h2_client` steps driven by
    [`HTTP2Client`][blackbull.client.http2.HTTP2Client] in scenario mode.  One
    ``asyncio.timeout`` bounds every session.
    """

    def __init__(self, target: Target, check_timeout: float,
                 tls_ca: str | None = None) -> None:
        self.target = target
        self.check_timeout = check_timeout
        self.tls_ca = tls_ca
        self.budget = ConnectionBudget()
        #: Narrowed per check by the runner against the run budget.
        self.effective_timeout = check_timeout

    # ---- HTTP/1.1 ------------------------------------------------------

    def scenario(self, name: str, *steps) -> ScenarioResult:
        steps = (*steps, Abort())

        async def _go():
            async with asyncio.timeout(self._session_bound()):
                async with HTTP1Client(
                        self.target.host, self.target.port,
                        connect_timeout=min(_CONNECT_TIMEOUT_S,
                                            self.effective_timeout)) as client:
                    return await client.execute_scenario(
                        Scenario(name=name, steps=steps))

        return self._bounded(_go)

    def raw_request(self, name: str, raw: bytes, *, half_close: bool = False) -> ScenarioResult:
        steps = [SendRawBytes(raw)]
        if half_close:
            steps.append(HalfClose())
        steps.append(ReadResponse(timeout=self.effective_timeout))
        return self.scenario(name, *steps)

    def scenarios_parallel(self, *named: tuple[str, tuple]) -> list[ScenarioResult]:
        """Run several whole scenarios concurrently (one budget slot each)."""
        async def _one(name: str, steps):
            async with HTTP1Client(
                    self.target.host, self.target.port,
                    connect_timeout=min(_CONNECT_TIMEOUT_S,
                                        self.effective_timeout)) as client:
                return await client.execute_scenario(
                    Scenario(name=name, steps=(*steps, Abort())))

        async def _go():
            async with asyncio.timeout(self._session_bound()):
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(_one(name, steps))
                             for name, steps in named]
                return [task.result() for task in tasks]

        return self._bounded(_go, slots=len(named))

    # ---- HTTP/2 --------------------------------------------------------

    def h2_scenario(self, name: str, *steps) -> h2s.ScenarioH2ClientResult:
        """Run `scenario_h2_client` steps against the h2 lane.

        The client is constructed with ``scenario_mode=True`` so the scenario
        owns the wire from byte zero (the preface is a step, not a side effect
        of connecting) and no receive loop races the scenario's own reads.
        """
        steps = (*steps, h2s.Abort())
        ctx = self._client_tls_context()

        async def _go():
            async with asyncio.timeout(self._session_bound()):
                async with HTTP2Client(
                        self.target.host, self.target.port, ssl=ctx,
                        connect_timeout=min(_CONNECT_TIMEOUT_S,
                                            self.effective_timeout),
                        scenario_mode=True) as client:
                    return await client.execute_scenario(
                        h2s.ScenarioH2Client(name=name, steps=steps))

        return self._bounded(_go)

    def tls_attempt(self, min_version: ssl.TLSVersion,
                    max_version: ssl.TLSVersion) -> tuple[bool, str]:
        """One bounded TLS handshake attempt; returns (ok, detail)."""
        ctx = self._client_tls_context()
        with warnings.catch_warnings():
            # Offering TLS 1.0/1.1 is deprecated by design; the warning is
            # the point of the attempt, not a defect in the probe.
            warnings.simplefilter('ignore', DeprecationWarning)
            ctx.minimum_version = min_version
            ctx.maximum_version = max_version
        ctx.set_alpn_protocols(['h2'])

        async def _go():
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.target.host, self.target.port,
                                        ssl=ctx,
                                        server_hostname=self.target.host),
                self.effective_timeout + _CONNECT_TIMEOUT_S)
            try:
                ss = writer.get_extra_info('ssl_object')
                return (True,
                        f'tls={ss.version()} alpn={ss.selected_alpn_protocol()}')
            finally:
                writer.close()

        self.budget.acquire()
        try:
            return asyncio.run(_go())
        except Exception as exc:  # noqa: BLE001 — a failed handshake is an outcome
            return (False, f'{type(exc).__name__}: {exc}')
        finally:
            self.budget.release()

    def _client_tls_context(self) -> ssl.SSLContext | None:
        if self.target.scheme != 'https':
            return None
        ctx = ssl.create_default_context(cafile=self.tls_ca)
        ctx.check_hostname = False
        return ctx

    # ---- shared bounds -------------------------------------------------

    def _session_bound(self) -> float:
        return (self.effective_timeout + _CONNECT_TIMEOUT_S + _SCENARIO_SLACK_S)

    def _bounded(self, go, *, slots: int = 1):
        for _ in range(slots):
            self.budget.acquire()
        try:
            return asyncio.run(go())
        finally:
            for _ in range(slots):
                self.budget.release()


# ------------------------------------------------------------------
# HTTP/1.1 helpers and oracles
# ------------------------------------------------------------------

def _get_request(path: str) -> bytes:
    return f'GET {path} HTTP/1.1\r\nHost: probe\r\nConnection: close\r\n\r\n'.encode('latin-1')


def _get_keepalive(path: str = '/') -> bytes:
    return f'GET {path} HTTP/1.1\r\nHost: probe\r\n\r\n'.encode('latin-1')


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
# Pipelined exchange: abuse + follow-up GET / on one connection (STATE-001)
# ------------------------------------------------------------------

#: (status, body) of the fixture's GET / — the one response shape the
#: state oracle accepts as the pipelined follow-up.
_OK = (200, b'ok')

_STOP_CLOSED = 'closed'
_STOP_TIMEOUT = 'timeout'
_STOP_DONE = 'done'


def _stop_reason(result: ScenarioResult) -> str:
    """Classify why the exchange stopped reading.

    ``closed`` covers every peer-side end of the connection (EOF and the
    client's own "connection is not reusable" refusal after a
    ``connection: close`` answer); ``done`` means every read completed —
    i.e. more responses than the exchange has requests.
    """
    if result.timed_out:
        return _STOP_TIMEOUT
    exc = result.exception or ''
    if exc.startswith('ConnectionError(') or exc.startswith('IncompleteReadError(') \
            or exc.startswith('ConnectionResetError(') or exc.startswith('BrokenPipeError('):
        return _STOP_CLOSED
    if not exc:
        return _STOP_DONE
    return f'error:{exc}'


def _exchange(probe: Probe, label: str, abuse: bytes, *,
              half_close: bool = False) -> tuple[tuple[tuple[int, bytes], ...], str]:
    """One pipelined exchange: *abuse* then a keep-alive ``GET /``, one connection.

    Returns the parsed responses in wire order and the stop reason.  The
    three read steps bound "how many answers came back": any response to the
    abusive request, exactly one to the pipelined ``GET /``, and a short tail
    read that must find nothing more.
    """
    steps: list = [SendRawBytes(abuse + _get_keepalive('/'))]
    if half_close:
        steps.append(HalfClose())
    steps += [ReadResponse(timeout=probe.effective_timeout),
              ReadResponse(timeout=probe.effective_timeout),
              ReadResponse(timeout=min(_TAIL_TIMEOUT_S, probe.effective_timeout))]
    result = probe.scenario(label, *steps)
    responses = tuple(
        (r.status, bytes(r.body))
        for r in result.received
        if r is not None and getattr(r, 'status', None) is not None)
    return responses, _stop_reason(result)


def state_verdict(responses: tuple[tuple[int, bytes], ...], stop: str) -> Verdict:
    """STATE-001 oracle: the pipelined ``GET /`` yields exactly one clean
    ``200 ok`` or the connection is closed; anything else is contamination.
    """
    n = len(responses)
    if stop.startswith('error:'):
        return Verdict(FAIL, f'garbled response bytes ({stop[6:]})')
    if n > 2:
        return Verdict(FAIL, f'{n} responses to 2 pipelined requests (desync)')
    if n == 2:
        if responses[1] == _OK:
            return Verdict(PASS, 'pipelined GET / answered one clean 200 "ok"')
        status, body = responses[1]
        return Verdict(FAIL, f'pipelined GET / answered {status} body={body[:32]!r}')
    if n == 1:
        if responses[0] == _OK:
            return Verdict(PASS, 'pipelined GET / answered one clean 200 "ok"')
        if stop == _STOP_CLOSED:
            return Verdict(PASS, 'connection closed after the abusive exchange')
        return Verdict(FAIL, f'GET / unanswered while the connection stayed open '
                             f'(last response {responses[0][0]})')
    if stop == _STOP_CLOSED:
        return Verdict(PASS, 'connection closed without a response')
    return Verdict(TIMEOUT, 'no response at all within the bound')


def abuse_accept_verdict(responses: tuple[tuple[int, bytes], ...], stop: str,
                         *, accept: frozenset[int], expected: str,
                         ok_200_body: bytes | None = None) -> Verdict:
    """Judge the abusive request's own response against its accept set.

    ``ok_200_body`` makes a 200 acceptable only when the body is exactly that
    echo (CHUNK-001); without it a 200 is never accepted.  A follow-up
    ``GET /`` answered ``200 ok`` in a one-response exchange is the abuse
    being *dropped*, which is not the accept set.
    """
    if responses and responses[0] != _OK:
        status, body = responses[0]
        if status in accept:
            if status == 200 and ok_200_body is not None:
                if body == ok_200_body:
                    return Verdict(PASS, f'accepted with 200, body echoed exactly')
                return Verdict(FAIL, f'200 echoed wrong body {body[:32]!r} '
                                     f'(expected {ok_200_body!r})')
            return Verdict(PASS, f'rejected with {status}')
        return Verdict(FAIL, f'accepted abuse with {status} (expected {expected} or close)')
    if stop == _STOP_CLOSED:
        return Verdict(PASS, 'rejected by connection close')
    return Verdict(FAIL, f'abuse not rejected (expected {expected} or close)')


def _combine(verdicts: Sequence[Verdict]) -> Verdict:
    """Worst verdict wins; details join.  TIMEOUT outranks FAIL only when
    nothing was judged FAIL (a real oracle violation is the sharper finding)."""
    detail = '; '.join(v.detail for v in verdicts)
    for wanted in (FAIL,):
        if any(v.verdict == wanted for v in verdicts):
            return Verdict(FAIL, detail)
    for wanted in (TIMEOUT,):
        if any(v.verdict == wanted for v in verdicts):
            return Verdict(TIMEOUT, detail)
    return Verdict(PASS, detail)


# ------------------------------------------------------------------
# M1 checks (oracles unchanged)
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


def _echoed_headers(body: bytes) -> list[tuple[str, str]]:
    echoed = json.loads(body.decode('utf-8'))['headers']
    if not (isinstance(echoed, list)
            and all(isinstance(item, list) and len(item) == 2
                    and all(isinstance(part, str) for part in item)
                    for item in echoed)):
        raise TypeError('not a list of [name, value] string pairs')
    return [(name, value) for name, value in echoed]


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
        echoed = _echoed_headers(bytes(resp.body))
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


# ------------------------------------------------------------------
# M2 checks — HTTP/1 attack surface
# ------------------------------------------------------------------

def _smuggle_002(probe: Probe) -> Verdict:
    """CL + obfuscated/duplicated TE (RFC 9112 §6.1/§6.3): 400-or-close,
    then the STATE-001 pipelined follow-up on the same connection."""
    variants = (
        ('chunked-identity',
         b'POST /echo-body HTTP/1.1\r\nHost: probe\r\nContent-Length: 5\r\n'
         b'Transfer-Encoding: chunked, identity\r\n\r\n0\r\n\r\n'),
        ('xchunked',
         b'POST /echo-body HTTP/1.1\r\nHost: probe\r\nContent-Length: 5\r\n'
         b'Transfer-Encoding: xchunked\r\n\r\n0\r\n\r\n'),
        ('dup-te',
         b'POST /echo-body HTTP/1.1\r\nHost: probe\r\nContent-Length: 5\r\n'
         b'Transfer-Encoding: chunked\r\nTransfer-Encoding: x\r\n\r\n0\r\n\r\n'),
        ('te-first',
         b'POST /echo-body HTTP/1.1\r\nHost: probe\r\n'
         b'Transfer-Encoding: chunked, identity\r\nContent-Length: 5\r\n\r\n0\r\n\r\n'),
    )
    verdicts = []
    for label, raw in variants:
        responses, stop = _exchange(probe, f'smuggle-002/{label}', raw)
        abuse = abuse_accept_verdict(responses, stop, accept=_ACCEPT_400, expected='400')
        state = state_verdict(responses, stop)
        verdicts.append(Verdict(_combine((abuse, state)).verdict,
                                f'{label}: {abuse.detail}; {state.detail}'))
    return _combine(verdicts)


def _smuggle_003(probe: Probe) -> Verdict:
    """Duplicate Content-Length with different values: 400-or-close + STATE-001."""
    catalogued = two_content_lengths()
    abuse_raw = b''.join(
        step.data for step in catalogued.steps if isinstance(step, SendRawBytes))
    responses, stop = _exchange(probe, 'smuggle-003', abuse_raw)
    abuse = abuse_accept_verdict(responses, stop, accept=_ACCEPT_400, expected='400')
    state = state_verdict(responses, stop)
    return _combine((abuse, state))


def _chunk_001(probe: Probe) -> Verdict:
    """Chunk extensions must not corrupt framing: 200 with an exact body
    echo, or 400; then STATE-001 on the same connection."""
    variants = (
        ('ext-token', b'1;x=y\r\nZ\r\n0\r\n\r\n', b'Z'),
        ('ext-quoted', b'4;foo="a b"\r\nWiki\r\n0\r\n\r\n', b'Wiki'),
    )
    verdicts = []
    for label, body, echo in variants:
        raw = (b'POST /echo-body HTTP/1.1\r\nHost: probe\r\n'
               b'Transfer-Encoding: chunked\r\n\r\n' + body)
        responses, stop = _exchange(probe, f'chunk-001/{label}', raw)
        abuse = abuse_accept_verdict(responses, stop, accept=frozenset({200, 400}),
                                     expected='400', ok_200_body=echo)
        state = state_verdict(responses, stop)
        verdicts.append(Verdict(_combine((abuse, state)).verdict,
                                f'{label}: {abuse.detail}; {state.detail}'))
    return _combine(verdicts)


def _chunk_002(probe: Probe) -> Verdict:
    """Malformed chunk sizes (RFC 9112 §7.1): 400-or-close + STATE-001.
    Each variant ends with a half-close so a size that swallows the pipelined
    ``GET /`` as chunk data cannot fake a hang-free exchange."""
    variants = (
        ('negative', b'-1\r\n'),
        ('hex-prefix', b'0x10\r\n'),
        ('overflow', b'FFFFFFFFFFFFFFFF\r\n'),
    )
    verdicts = []
    for label, size_line in variants:
        raw = (b'POST /echo-body HTTP/1.1\r\nHost: probe\r\n'
               b'Transfer-Encoding: chunked\r\n\r\n' + size_line)
        responses, stop = _exchange(probe, f'chunk-002/{label}', raw, half_close=True)
        abuse = abuse_accept_verdict(responses, stop, accept=_ACCEPT_400, expected='400')
        state = state_verdict(responses, stop)
        verdicts.append(Verdict(_combine((abuse, state)).verdict,
                                f'{label}: {abuse.detail}; {state.detail}'))
    return _combine(verdicts)


def _h1_robust_006(probe: Probe) -> Verdict:
    """Malformed field names (RFC 9112 §5.1, RFC 9110 §5.1): 400-or-close.
    A colon "inside" the name is unobservable — the field name ends at the
    first colon — so the crisp variants all carry whitespace in the name."""
    variants = (
        ('space-in-name', b'Foo Bar: x'),
        ('space-before-colon', b'Foo : x'),
        ('tab-in-name', b'Foo\tBar: x'),
    )
    verdicts = []
    for label, field in variants:
        raw = b'GET /echo-headers HTTP/1.1\r\nHost: probe\r\n' + field \
            + b'\r\nConnection: close\r\n\r\n'
        result = probe.raw_request('h1-robust-006', raw)
        verdicts.append(_abuse_oracle(result, probe.effective_timeout,
                                      _ACCEPT_400, '400'))
    return _combine(verdicts)


_OBSF_FOLD = (
    ('fold-into-value', b'Foo: a\r\n b', 'foo', 'b'),
    ('fold-injection', b'Foo: a\r\n X-Smuggle: 1', 'foo', 'x-smuggle'),
)


def _obsfold_variant_verdict(probe: Probe, label: str, field: bytes,
                             own_name: str, smuggled: str) -> Verdict:
    timeout = probe.effective_timeout
    raw = (b'GET /echo-headers HTTP/1.1\r\nHost: probe\r\n' + field
           + b'\r\nConnection: close\r\n\r\n')
    result = probe.raw_request('h1-robust-007', raw)
    early = _reject_or_timeout(result, timeout)
    if early is not None:
        return Verdict(early.verdict, f'{label}: {early.detail}')
    resp = result.response
    if resp is None:
        return Verdict(PASS, f'{label}: rejected by connection close')
    if resp.status == 400:
        return Verdict(PASS, f'{label}: rejected with 400 (obs-fold refused)')
    if resp.status < 200 or resp.status >= 300:
        return Verdict(FAIL, f'{label}: unexpected status {resp.status}')
    try:
        echoed = _echoed_headers(bytes(resp.body))
    except (ValueError, KeyError, TypeError):
        return Verdict(FAIL, f'{label}: {resp.status} but echo body is not a header list')
    if any('\r' in value or '\n' in value for _, value in echoed):
        return Verdict(FAIL, f'{label}: echoed value kept CR/LF')
    names = [name.lower() for name, _ in echoed]
    if smuggled in names:
        return Verdict(FAIL, f'{label}: obs-fold injected a {smuggled!r} field line '
                             f'(CWE-113)')
    own = [value for name, value in echoed if name.lower() == own_name]
    if len(own) != 1:
        return Verdict(FAIL, f'{label}: {len(own)} {own_name!r} fields after coalescing')
    if own[0] not in ('a b', 'a\tb'):
        return Verdict(FAIL, f'{label}: coalesced to {own[0]!r} '
                             f"(expected 'a b' or 'a\\tb')")
    return Verdict(PASS, f'{label}: safely coalesced into one field ({own[0]!r})')


def _h1_robust_007(probe: Probe) -> Verdict:
    return _combine([_obsfold_variant_verdict(probe, label, field, own, smuggled)
                     for label, field, own, smuggled in _OBSF_FOLD])


def _h1_robust_008(probe: Probe) -> Verdict:
    raw = b'GET / HTTP/1.1\r\nConnection: close\r\n\r\n'
    return _abuse_oracle(probe.raw_request('h1-robust-008', raw),
                         probe.effective_timeout, _ACCEPT_400, '400')


def _absform_verdict(probe: Probe, label: str, raw: bytes) -> Verdict:
    timeout = probe.effective_timeout
    result = probe.raw_request('h1-robust-009', raw)
    early = _reject_or_timeout(result, timeout)
    if early is not None:
        return Verdict(early.verdict, f'{label}: {early.detail}')
    resp = result.response
    if resp is None:
        return Verdict(PASS, f'{label}: rejected by connection close')
    if resp.status == 400:
        return Verdict(PASS, f'{label}: rejected with 400')
    if resp.status == 200 and bytes(resp.body) == b'ok':
        return Verdict(PASS, f'{label}: absolute-form accepted, routed to / (200 "ok")')
    return Verdict(FAIL, f'{label}: unexpected {resp.status} body='
                         f'{bytes(resp.body)[:32]!r} (expected 200 "ok", 400, or close)')


def _h1_robust_009(probe: Probe) -> Verdict:
    authority = f'{probe.target.host}:{probe.target.port}'.encode('ascii')
    return _combine([
        _absform_verdict(probe, 'same-authority',
                         b'GET http://' + authority + b'/ HTTP/1.1\r\nHost: '
                         + authority + b'\r\nConnection: close\r\n\r\n'),
        _absform_verdict(probe, 'foreign-authority',
                         b'GET http://evil.example/ HTTP/1.1\r\nHost: probe\r\n'
                         b'Connection: close\r\n\r\n'),
    ])


_NUL_PATHS = (
    ('nul-root', b'/\x00'),
    ('nul-truncation', b'/\x00/static/hello.txt'),
    ('overlong-raw', b'/\xc0\xaf'),
    ('overlong-pct', b'/%c0%af'),
)


def _h1_robust_010(probe: Probe) -> Verdict:
    """NUL bytes and overlong UTF-8 in the request target: 400/404-or-close.
    A 200 is always FAIL — the target carried bytes the router must never
    see, whatever the body."""
    verdicts = []
    for label, path in _NUL_PATHS:
        raw = b'GET ' + path + b' HTTP/1.1\r\nHost: probe\r\nConnection: close\r\n\r\n'
        result = probe.raw_request('h1-robust-010', raw)
        verdicts.append(_abuse_oracle(result, probe.effective_timeout,
                                      frozenset({400, 404}), '400/404'))
    return _combine(verdicts)


def _h1_robust_011(probe: Probe) -> Verdict:
    """Bounded slow-send (slowloris-lite): two connections each send a partial
    request line and hold for ``min(check-timeout, 5s)``.

    Gentle by construction — two connections, one bounded hold, then abort.
    The oracle: the exchange must complete inside the check deadline with no
    response (the server is entitled to wait) or a 408; anything else, or a
    deadline overrun, fails.  Server survival is BASELINE-003's row.
    """
    hold = min(probe.effective_timeout, _SLOW_HOLD_MAX_S)
    partial = b'GET / HT'
    steps = (SendRawBytes(partial), Sleep(hold),
             ReadResponse(timeout=min(_TAIL_TIMEOUT_S, probe.effective_timeout)))
    results = probe.scenarios_parallel(
        ('h1-robust-011/a', steps), ('h1-robust-011/b', steps))
    details = []
    verdict = PASS
    for name, result in zip(('a', 'b'), results):
        if result.timed_out:
            details.append(f'{name}: no response while holding (expected)')
        elif result.response is None:
            details.append(f'{name}: connection closed (expected)')
        elif result.response.status == 408:
            details.append(f'{name}: answered 408 (expected)')
        else:
            verdict = FAIL
            details.append(f'{name}: answered {result.response.status} to a partial '
                           f'request line')
    details.append(f'held {hold:g}s on 2 connections; server survival: BASELINE-003')
    return Verdict(verdict, '; '.join(details))


def _state_001(probe: Probe) -> Verdict:
    """STATE-001 standalone: a battery of abusive exchanges, each pipelined
    with ``GET /`` on the same connection, must show no state contamination."""
    abuses = (
        ('chunk-ext', b'POST /echo-body HTTP/1.1\r\nHost: probe\r\n'
                      b'Transfer-Encoding: chunked\r\n\r\n1;x=y\r\nZ\r\n0\r\n\r\n', False),
        ('obs-fold', b'GET /echo-headers HTTP/1.1\r\nHost: probe\r\n'
                     b'Foo: a\r\n b\r\n\r\n', False),
        ('garbage-line', b'\x00\x01garbage\r\n\r\n', False),
        ('te-xchunked', b'POST /echo-body HTTP/1.1\r\nHost: probe\r\n'
                        b'Content-Length: 5\r\nTransfer-Encoding: xchunked\r\n\r\n'
                        b'hello', False),
        ('truncated-body', b'POST /echo-body HTTP/1.1\r\nHost: probe\r\n'
                           b'Content-Length: 100\r\n\r\nshort', True),
    )
    verdicts = []
    for label, raw, half_close in abuses:
        responses, stop = _exchange(probe, f'state-001/{label}', raw,
                                    half_close=half_close)
        state = state_verdict(responses, stop)
        verdicts.append(Verdict(state.verdict, f'{label}: {state.detail}'))
    return _combine(verdicts)


# ------------------------------------------------------------------
# M3 checks — HTTP/2 over TLS
# ------------------------------------------------------------------

@dataclass(frozen=True)
class H2Info:
    """One received frame (or EOF) reduced to what the h2 oracles judge on."""
    kind: str
    stream_id: int | None
    error_code: int | None = None
    status: int | None = None
    body: bytes = b''
    end_stream: bool = False


def h2_info(frame) -> H2Info:
    """Reduce a `scenario_h2_client` result frame to [`H2Info`][].

    Duck-typed on purpose: unit tests feed plain stand-ins, and a frame the
    parser did not recognise (``FrameType() is None``) is its own kind —
    an unknown frame type is an outcome, not an error.
    """
    if frame is None:
        return H2Info(kind='EOF', stream_id=None)
    ft = getattr(frame, 'FrameType', None)
    frame_type = ft() if callable(ft) else ft
    kind = getattr(frame_type, 'name', None) or (
        'UNKNOWN' if frame_type is None else str(frame_type))
    pseudo = getattr(frame, 'pseudo_headers', None) or {}
    raw_status = pseudo.get(':status')
    try:
        status = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        status = None
    error_code = getattr(frame, 'error_code', None)
    return H2Info(
        kind=kind,
        stream_id=getattr(frame, 'stream_id', None),
        error_code=int(error_code) if error_code is not None else None,
        status=status,
        body=bytes(getattr(frame, 'payload', b'') or b''),
        end_stream=bool(getattr(frame, 'end_stream', False)),
    )


def h2_infos(result) -> list[H2Info]:
    return [h2_info(frame) for frame in result.received]


def h2_ok_verdict(infos: Sequence[H2Info], stream_id: int) -> Verdict:
    """The stream completed exactly one response: 200 with body ``ok``."""
    status = None
    body = b''
    for info in infos:
        if info.stream_id != stream_id:
            continue
        if info.kind == 'HEADERS' and info.status is not None:
            status = info.status
        elif info.kind == 'DATA':
            body += info.body
    if status == 200 and body == b'ok':
        return Verdict(PASS, '200, body "ok"')
    return Verdict(FAIL, f'status={status} body={body[:32]!r} (expected 200 "ok")')


def h2_error_verdict(infos: Sequence[H2Info], *, stream_id: int | None,
                     accept_codes: frozenset[int], expected: str,
                     accept_4xx: frozenset[int] = frozenset(),
                     accept_kinds: frozenset[str] = frozenset({'GOAWAY', 'RST_STREAM'})) -> Verdict:
    """Judge the abuse's error signalling against a strict accept set.

    An error frame is accepted only when it is of an ``accept_kinds`` kind
    (so a stream error cannot stand in for a required connection error, or
    vice versa) and carries an ``accept_codes`` error code; a connection
    close with no error frame is accepted as rejection-by-close; ``accept_4xx``
    (if non-empty) additionally accepts a 4xx response on *stream_id*.
    """
    errors = [info for info in infos if info.kind in ('GOAWAY', 'RST_STREAM')]
    for info in errors:
        if info.kind in accept_kinds and (
                stream_id is None or info.kind == 'GOAWAY'
                or info.stream_id == stream_id):
            if info.error_code in accept_codes:
                return Verdict(PASS, f'rejected with {info.kind} '
                                     f'{_error_name(info.error_code)}')
            return Verdict(FAIL, f'{info.kind} carries error '
                                 f'{_error_name(info.error_code)} (expected {expected})')
    if errors:
        first = errors[0]
        return Verdict(FAIL, f'{first.kind} {_error_name(first.error_code)} is '
                             f'outside the accept set (expected {expected})')
    for info in infos:
        if info.kind == 'HEADERS' and info.stream_id == stream_id \
                and info.status is not None:
            if accept_4xx and info.status in accept_4xx:
                return Verdict(PASS, f'rejected with {info.status}')
            return Verdict(FAIL, f'accepted abuse with {info.status} '
                                 f'(expected {expected})')
    if any(info.kind == 'EOF' for info in infos):
        return Verdict(PASS, 'rejected by connection close')
    return Verdict(TIMEOUT, 'no verdict frame within the bound')


def _error_name(code: int | None) -> str:
    if code is None:
        return '?'
    try:
        return ErrorCodes(code).name
    except ValueError:
        return str(code)


def _h2_prefix() -> list:
    """A conformant client handshake: preface, SETTINGS, read, ACK."""
    return [h2s.SendPreface(),
            h2s.SendFrame(FrameTypes.SETTINGS, flags=0, stream_id=0),
            h2s.ReadResponse(timeout=2.0),
            h2s.SendFrame(FrameTypes.SETTINGS, flags=0x01, stream_id=0)]


def _h2_request(probe: Probe, stream_id: int = 1, path: str = '/',
                pseudo: tuple = (), headers: tuple = ()) -> h2s.SendRawBytes:
    authority = f'{probe.target.host}:{probe.target.port}'
    fields = pseudo or ((':method', 'GET'), (':path', path),
                        (':scheme', probe.target.scheme),
                        (':authority', authority))
    return h2s.SendRawBytes(h2s.encode_headers(h2s.SendHeaders(
        pseudo=fields, headers=headers, stream_id=stream_id, end_stream=True)))


def _h2_raw_headers(fields: Sequence[tuple[str, str]], stream_id: int = 1,
                    *, fragment: int | None = None) -> list:
    """HEADERS (+ CONTINUATIONs) carrying *fields* in exactly that order."""
    block = hpack.Encoder().encode(list(fields))
    if fragment is None:
        return [h2s.SendRawBytes(h2s.encode_frame(h2s.SendFrame(
            FrameTypes.HEADERS, flags=0x05, stream_id=stream_id, data=block)))]
    steps = []
    first, rest = block[:fragment], block[fragment:]
    steps.append(h2s.SendRawBytes(h2s.encode_frame(h2s.SendFrame(
        FrameTypes.HEADERS, flags=0x01, stream_id=stream_id, data=first))))
    while rest:
        chunk, rest = rest[:fragment], rest[fragment:]
        flags = 0x04 if not rest else 0x00
        steps.append(h2s.SendRawBytes(h2s.encode_frame(h2s.SendFrame(
            FrameTypes.CONTINUATION, flags=flags, stream_id=stream_id, data=chunk))))
    return steps


def _h2_exchange(probe: Probe, label: str, abuse: Sequence) -> list[H2Info]:
    """Handshake, send *abuse* steps, then read whatever comes back."""
    reads = [h2s.ReadResponse(timeout=probe.effective_timeout)] + \
        [h2s.ReadResponse(timeout=min(_TAIL_TIMEOUT_S, probe.effective_timeout))
         for _ in range(3)]
    result = probe.h2_scenario(label, *_h2_prefix(), *abuse, *reads)
    return h2_infos(result)


_H2_PROTOCOL_ERROR = frozenset({int(ErrorCodes.PROTOCOL_ERROR)})


def _h2_base_001(probe: Probe) -> Verdict:
    infos = _h2_exchange(probe, 'h2-base-001', [_h2_request(probe)])
    return h2_ok_verdict(infos, stream_id=1)


def _h2_robust_001(probe: Probe) -> Verdict:
    """Malformed request pseudo-headers (RFC 9113 §8.1/§8.3): a stream or
    connection PROTOCOL_ERROR, or close; never a dispatched request."""
    variants = (
        ('missing-method', _h2_raw_headers(
            ((':path', '/'), (':scheme', probe.target.scheme),
             (':authority', f'{probe.target.host}:{probe.target.port}')))),
        ('pseudo-after-regular', _h2_raw_headers(
            ((':path', '/'), ('x-first', '1'), (':method', 'GET'),
             (':scheme', probe.target.scheme),
             (':authority', f'{probe.target.host}:{probe.target.port}')))),
    )
    verdicts = []
    for label, abuse in variants:
        infos = _h2_exchange(probe, f'h2-robust-001/{label}', abuse)
        verdict = h2_error_verdict(infos, stream_id=1,
                                   accept_codes=_H2_PROTOCOL_ERROR,
                                   expected='PROTOCOL_ERROR')
        verdicts.append(Verdict(verdict.verdict, f'{label}: {verdict.detail}'))
    return _combine(verdicts)


def _h2_robust_002(probe: Probe) -> Verdict:
    """DATA on an idle stream or on stream 0 (RFC 9113 §6.1): connection error."""
    variants = (
        ('idle-stream', 5),
        ('stream-0', 0),
    )
    verdicts = []
    for label, stream_id in variants:
        infos = _h2_exchange(probe, f'h2-robust-002/{label}',
                             [h2s.SendFrame(FrameTypes.DATA, stream_id=stream_id,
                                            data=b'x')])
        verdict = h2_error_verdict(infos, stream_id=None,
                                   accept_codes=_H2_PROTOCOL_ERROR,
                                   expected='GOAWAY PROTOCOL_ERROR',
                                   accept_kinds=frozenset({'GOAWAY'}))
        verdicts.append(Verdict(verdict.verdict, f'{label}: {verdict.detail}'))
    return _combine(verdicts)


def _h2_robust_003(probe: Probe) -> Verdict:
    """An unknown frame type with the reserved bit clear MUST be ignored
    (RFC 9113 §4.1): the same connection must still serve a clean request."""
    infos = _h2_exchange(probe, 'h2-robust-003',
                         [h2s.SendFrame(0xfa, stream_id=0, data=b'whatever'),
                          _h2_request(probe)])
    ignored = h2_ok_verdict(infos, stream_id=1)
    return Verdict(ignored.verdict,
                   f'unknown frame 0xfa ignored: {ignored.detail}')


def _h2_robust_004(probe: Probe) -> Verdict:
    """Oversized header list (128 KiB, fragmented per MAX_FRAME_SIZE):
    431 or REFUSED_STREAM/ENHANCE_YOUR_CALM, no crash."""
    big = [('x-big', 'a' * 8192) for _ in range(16)]
    fields = ((':method', 'GET'), (':path', '/'),
              (':scheme', probe.target.scheme),
              (':authority', f'{probe.target.host}:{probe.target.port}')) + tuple(big)
    infos = _h2_exchange(probe, 'h2-robust-004',
                         _h2_raw_headers(fields, fragment=16000))
    accept = frozenset({int(ErrorCodes.ENHANCE_YOUR_CALM),
                        int(ErrorCodes.REFUSED_STREAM)})
    verdict = h2_error_verdict(infos, stream_id=1, accept_codes=accept,
                               expected='ENHANCE_YOUR_CALM/REFUSED_STREAM',
                               accept_4xx=frozenset({431}))
    return Verdict(verdict.verdict, f'128 KiB header list: {verdict.detail}')


def _h2_robust_005(probe: Probe) -> Verdict:
    """PRIORITY self-dependency (RFC 9113 §5.3.1): PROTOCOL_ERROR."""
    infos = _h2_exchange(probe, 'h2-robust-005',
                         [h2s.SendFrame(FrameTypes.PRIORITY, stream_id=1,
                                        data=(1).to_bytes(4, 'big') + bytes([0]))])
    verdict = h2_error_verdict(infos, stream_id=1,
                               accept_codes=_H2_PROTOCOL_ERROR,
                               expected='PROTOCOL_ERROR')
    return Verdict(verdict.verdict, f'PRIORITY self-dependency: {verdict.detail}')


def _h2_base_002(probe: Probe) -> Verdict:
    infos = _h2_exchange(probe, 'h2-base-002', [_h2_request(probe)])
    verdict = h2_ok_verdict(infos, stream_id=1)
    return Verdict(verdict.verdict,
                   f'{verdict.detail} — server survived all h2 probes')


_TLS_ATTEMPTS = (
    ('TLS1.0', ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1),
    ('TLS1.1', ssl.TLSVersion.TLSv1_1, ssl.TLSVersion.TLSv1_1),
    ('TLS1.2', ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_2),
    ('TLS1.3', ssl.TLSVersion.TLSv1_3, ssl.TLSVersion.TLSv1_3),
)


def tls_verdict(attempts: Sequence[tuple[str, bool, str]]) -> Verdict:
    """TLS-001: <1.2 must fail, 1.2/1.3 must succeed with ALPN ``h2``."""
    details = []
    verdict = PASS
    by_label = {label: (ok, detail) for label, ok, detail in attempts}
    for label in ('TLS1.0', 'TLS1.1'):
        ok, detail = by_label.get(label, (True, 'not attempted'))
        if ok:
            verdict = FAIL
            details.append(f'{label}: handshake succeeded ({detail})')
        else:
            details.append(f'{label}: refused ({detail.splitlines()[0][:60]})')
    for label, wanted in (('TLS1.2', 'TLSv1.2'), ('TLS1.3', 'TLSv1.3')):
        ok, detail = by_label.get(label, (False, 'not attempted'))
        if ok and detail == f'tls={wanted} alpn=h2':
            details.append(f'{label}: {detail}')
        elif ok:
            verdict = FAIL
            details.append(f'{label}: negotiated {detail} '
                           f'(expected tls={wanted} alpn=h2)')
        else:
            verdict = FAIL
            details.append(f'{label}: handshake failed ({detail})')
    return Verdict(verdict, '; '.join(details))


def _tls_001(probe: Probe) -> Verdict:
    attempts = [(label, *probe.tls_attempt(lo, hi))
                for label, lo, hi in _TLS_ATTEMPTS]
    return tls_verdict(attempts)


# ------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------

CHECKS: tuple[Check, ...] = (
    Check('BASELINE-001', 'GET / returns 200 with body "ok"', 'High', 'CWE-400', _baseline_001, 'h1'),
    Check('BASELINE-002', 'GET /json returns 200 with JSON {"ok": true}', 'High', 'CWE-400', _baseline_002, 'h1'),
    Check('H1-ROBUST-001', 'unknown method FOO answered with 4xx/501 or close', 'Info', 'CWE-755', _h1_robust_001, 'h1', 'High'),
    Check('H1-ROBUST-002', '100 KiB header value answered with 4xx or close', 'Medium', 'CWE-400', _h1_robust_002, 'h1', 'High'),
    Check('H1-ROBUST-003', 'garbage request-line bytes answered with 400 or close', 'Info', 'CWE-755', _h1_robust_003, 'h1', 'High'),
    Check('H1-ROBUST-004', 'CRLF in a header value rejected, sanitized, or kept opaque (RFC 9110 §5.5)', 'High', 'CWE-113', _h1_robust_004, 'h1'),
    Check('H1-ROBUST-005', 'Content-Length with truncated body then FIN answered with 4xx/408 or close', 'Info', 'CWE-755', _h1_robust_005, 'h1', 'High'),
    Check('H1-ROBUST-006', 'field name with whitespace answered with 400 or close (RFC 9112 §5.1)', 'Medium', 'CWE-444', _h1_robust_006, 'h1', 'High'),
    Check('H1-ROBUST-007', 'obs-fold answered with 400 or safe coalescing, never injection (RFC 9112 §5.2)', 'Medium', 'CWE-444', _h1_robust_007, 'h1', 'High'),
    Check('H1-ROBUST-008', 'HTTP/1.1 request without Host answered with 400 or close (RFC 9112 §3.2)', 'Low', 'CWE-755', _h1_robust_008, 'h1', 'High'),
    Check('H1-ROBUST-009', 'absolute-form request line answered with 200 "ok"/400 or close, never crash', 'Info', 'CWE-755', _h1_robust_009, 'h1', 'High'),
    Check('H1-ROBUST-010', 'NUL or overlong UTF-8 in the path answered with 400/404 or close, never 200', 'High', 'CWE-158', _h1_robust_010, 'h1', 'High'),
    Check('SMUGGLE-001', 'Content-Length and Transfer-Encoding together rejected (RFC 9112 §6.3)', 'High', 'CWE-444', _smuggle_001, 'h1'),
    Check('SMUGGLE-002', 'CL with obfuscated/duplicated TE rejected, pipelined GET / clean (RFC 9112 §6.1/§6.3)', 'High', 'CWE-444', _smuggle_002, 'h1'),
    Check('SMUGGLE-003', 'duplicate Content-Length with different values rejected, pipelined GET / clean (RFC 9112 §6.3)', 'High', 'CWE-444', _smuggle_003, 'h1'),
    Check('CHUNK-001', 'chunk extensions do not corrupt framing: exact body echo or 400, pipelined GET / clean', 'Medium', 'CWE-444', _chunk_001, 'h1', 'High'),
    Check('CHUNK-002', 'malformed chunk sizes rejected (RFC 9112 §7.1), pipelined GET / clean', 'Medium', 'CWE-444', _chunk_002, 'h1', 'High'),
    Check('STATE-001', 'each abusive exchange leaves a pipelined GET / exactly one clean 200 "ok" or a closed connection', 'High', 'CWE-444', _state_001, 'h1'),
    Check('H1-ROBUST-011', 'partial request line held open: bounded hold, 408/close/no-answer only', 'Medium', 'CWE-400', _h1_robust_011, 'h1', 'High'),
    Check('STATIC-001', 'traversal via /static/../ and %2e%2e/ never serves files outside the root', 'High', 'CWE-22', _static_001, 'h1'),
    Check('HDR-001', 'server: header carries no filesystem path or Python version', 'Low', 'CWE-200', _hdr_001, 'h1'),
    Check('BASELINE-003', 'GET / still returns 200 after every probe', 'High', 'CWE-400', _baseline_003, 'h1'),
    Check('H2-BASE-001', 'GET / over HTTP/2 returns 200 with body "ok"', 'High', 'CWE-400', _h2_base_001, 'h2'),
    Check('H2-ROBUST-001', 'HEADERS with missing/misordered pseudo-headers answered PROTOCOL_ERROR or close', 'Medium', 'CWE-444', _h2_robust_001, 'h2', 'High'),
    Check('H2-ROBUST-002', 'DATA on idle stream or stream 0 answered GOAWAY PROTOCOL_ERROR (RFC 9113 §6.1)', 'Medium', 'CWE-444', _h2_robust_002, 'h2', 'High'),
    Check('H2-ROBUST-003', 'unknown frame type with reserved bit clear ignored, connection keeps working (RFC 9113 §4.1)', 'Low', 'CWE-755', _h2_robust_003, 'h2', 'High'),
    Check('H2-ROBUST-004', '128 KiB header list answered 431 or REFUSED_STREAM/ENHANCE_YOUR_CALM', 'Medium', 'CWE-400', _h2_robust_004, 'h2', 'High'),
    Check('H2-ROBUST-005', 'PRIORITY self-dependency answered PROTOCOL_ERROR (RFC 9113 §5.3.1)', 'Low', 'CWE-755', _h2_robust_005, 'h2', 'High'),
    Check('H2-BASE-002', 'a fresh HTTP/2 request after all h2 abuse returns 200 with body "ok"', 'High', 'CWE-400', _h2_base_002, 'h2'),
    Check('TLS-001', 'TLS 1.0/1.1 refused; TLS 1.2/1.3 handshake negotiates ALPN h2', 'Medium', 'CWE-326', _tls_001, 'h2'),
)


def checks_for(lane: str) -> tuple[Check, ...]:
    return tuple(check for check in CHECKS if check.lane == lane)


# ------------------------------------------------------------------
# Runner and reporting
# ------------------------------------------------------------------

def run_checks(probe: Probe, checks: Sequence[Check],
               deadline: float) -> list[CheckResult]:
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


@dataclass(frozen=True)
class Lane:
    """One protocol lane's target URL and its check results."""
    name: str
    base_url: str
    results: tuple[CheckResult, ...]


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


def render_markdown(lanes: Sequence[Lane], *,
                    check_timeout: float, run_timeout: float,
                    timestamp: str) -> str:
    lines = [
        f'# BLA-526 robustness probe — {timestamp}',
        '',
        f'- Bounds: check timeout {check_timeout:g}s, run cap {run_timeout:g}s, '
        f'connections {MAX_CONCURRENT_CONNECTIONS} concurrent / {MAX_TOTAL_CONNECTIONS} per run',
        '- Verdicts: PASS = mechanical oracle held; FAIL = oracle violated; '
        'TIMEOUT = no answer within the bound.',
        '- Severity is the rank a failure of that check carries '
        '(docs/security/severity.md). Recording of findings: docs/security/probe.md.',
        '',
    ]
    for lane in lanes:
        lines += [
            f'## {lane.name} lane — {lane.base_url}',
            '',
            '| Check | Severity | Verdict | Detail | CWE |',
            '|---|---|---|---|---|',
        ]
        for r in lane.results:
            lines.append(f'| {r.check_id} | {r.severity} | {r.verdict} | {r.detail} | {r.cwe} |')
        lines.append('')
    return '\n'.join(lines)


def write_report(lanes: Sequence[Lane], *, check_timeout: float,
                 run_timeout: float, timestamp: str,
                 out_dir: Path | None = None) -> Path:
    out = out_dir if out_dir is not None else _REPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / f'{timestamp}.md'
    path.write_text(
        render_markdown(lanes, check_timeout=check_timeout,
                        run_timeout=run_timeout, timestamp=timestamp),
        encoding='utf-8')
    return path


def exit_code(lanes: Sequence[Lane]) -> int:
    return 0 if all(r.verdict == PASS
                    for lane in lanes for r in lane.results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='probe.py',
        description='BLA-526 robustness probe for running local BlackBull servers')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000',
                        help='HTTP/1.1 lane target (loopback hosts only)')
    parser.add_argument('--h2-url', default='https://127.0.0.1:8443',
                        help='HTTP/2 lane target (loopback hosts only)')
    parser.add_argument('--lane', choices=(*LANES, 'all'), default='all',
                        help='which lane(s) to run (default all)')
    parser.add_argument('--tls-ca', default=DEFAULT_TLS_CA,
                        help=f'PEM bundle the TLS lane verifies against '
                             f'(default {DEFAULT_TLS_CA})')
    parser.add_argument('--check-timeout', type=float, default=5.0,
                        help='per-check timeout in seconds (default 5)')
    parser.add_argument('--run-timeout', type=float, default=120.0,
                        help='overall run cap in seconds (default 120)')
    args = parser.parse_args(argv)
    if not all(math.isfinite(t) and t > 0
               for t in (args.check_timeout, args.run_timeout)):
        parser.error('timeouts must be finite and positive')

    # The gate covers both lane URLs regardless of --lane: a URL the CLI
    # names is a URL the run is accountable for.
    gated = {}
    try:
        for lane, url in (('h1', args.base_url), ('h2', args.h2_url)):
            gated[lane] = (url, parse_target(url))
    except UnsafeTargetError as exc:
        print(f'probe: {exc}', file=sys.stderr)
        return 2

    lane_names = LANES if args.lane == 'all' else (args.lane,)
    for lane in lane_names:
        if gated[lane][1].scheme == 'https' and not Path(args.tls_ca).is_file():
            print(f'probe: refused: {lane} lane is https but --tls-ca '
                  f'{args.tls_ca} does not exist; refusing to skip TLS '
                  f'verification', file=sys.stderr)
            return 2

    started = datetime.now(timezone.utc)
    timestamp = started.strftime('%Y%m%dT%H%M%SZ')
    deadline = time.monotonic() + args.run_timeout
    lanes: list[Lane] = []
    for lane in lane_names:
        url, target = gated[lane]
        probe = Probe(target, args.check_timeout, tls_ca=args.tls_ca)
        results = run_checks(probe, checks_for(lane), deadline)
        lanes.append(Lane(lane, url, tuple(results)))

    print(f'# {timestamp} lane={args.lane} '
          f'check-timeout={args.check_timeout:g}s run-timeout={args.run_timeout:g}s')
    for lane in lanes:
        print(f'## {lane.name} lane — {lane.base_url}')
        print(render_table(lane.results))
    report = write_report(lanes, check_timeout=args.check_timeout,
                          run_timeout=args.run_timeout, timestamp=timestamp)
    print(f'report: {report}')
    return exit_code(lanes)


if __name__ == '__main__':
    sys.exit(main())
