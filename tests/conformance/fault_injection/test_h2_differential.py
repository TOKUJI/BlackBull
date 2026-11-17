"""HTTP/2 differential on both axes, with BlackBull never the only judge.

Server axis: every ``BROKEN_CLIENTS`` case runs unchanged against BlackBull's
server, nginx and nghttpd; so does BlackBull's own client, through a proxy that
perturbs the transport.  Client axis: every ``BROKEN_SERVERS`` case is served
to BlackBull's client, httpx (``h2``) and curl (nghttp2).  Outcomes are
compared by ``oracle_h2``; a case passes when BlackBull agrees with each
reference, or when the disagreement is a known divergence listed with the rule
behind it.  A listed divergence that stops happening fails too, so the lists
cannot outlive what they describe.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest

from blackbull.fault_injection.catalogue.h2_differential import BROKEN_CLIENTS, BROKEN_SERVERS
from blackbull.fault_injection.oracle_h2 import (
    ACCEPTED_CATEGORIES, Category, H2Outcome, categorize, run_scenario,
)

from ._perturbing_proxy import PERTURBATIONS, PerturbingProxy
from ._reference_peers import PEER_BUDGET

pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group('reference_peers')]

#: (case, reference) -> (category, why).  Recorded, not judged: the reason
#: names the rule so a reader can tell whose reading it is.
_R, _E = Category.REJECT_DIFFER, Category.ERROR_DIFFER
_NGINX_ACCEPTS = 'nginx answers 200; RFC 9113 {} says a request carrying it is malformed'
KNOWN_SERVER: dict[tuple[str, str], tuple[Category, str]] = {
    ('host_authority_disagree', 'nginx'): (_R, 'nginx answers 400 to any Host beside :authority; '
                                              'RFC 9113 §8.3.1 allows Host, and only SHOULD-rejects '
                                              'a disagreeing one'),
    ('host_authority_agree', 'nginx'): (_R, 'nginx answers 400 to any Host beside :authority; '
                                           'RFC 9113 §8.3.1 allows an agreeing Host'),
    ('field_value_leading_space', 'nginx'): (_R, _NGINX_ACCEPTS.format('§8.2.1')),
    ('connection_header', 'nginx'): (_R, _NGINX_ACCEPTS.format('§8.2.2')),
    ('keep_alive_header', 'nginx'): (_R, _NGINX_ACCEPTS.format('§8.2.2')),
    ('upgrade_header', 'nginx'): (_R, _NGINX_ACCEPTS.format('§8.2.2')),
    ('transfer_encoding_header', 'nginx'): (_R, _NGINX_ACCEPTS.format('§8.2.2')),
    ('te_gzip', 'nginx'): (_R, _NGINX_ACCEPTS.format('§8.2.2')),
    ('pseudo_after_regular', 'nginx'): (_R, _NGINX_ACCEPTS.format('§8.3')),
    ('dtsu_after_field', 'nginx'): (_R, 'nginx decodes the block; RFC 7541 §4.2 (RFC 9113 §4.3.1) '
                                       'makes a size update after a field a decoding error'),
    ('data_on_idle_stream', 'nginx'): (_E, 'nginx ignores DATA on an idle stream; RFC 9113 §5.1 '
                                          'makes it a connection error PROTOCOL_ERROR'),
    ('lower_stream_id_after_higher', 'nghttpd'): (_R, 'nghttpd ignores the lower stream id; '
                                                     'RFC 9113 §5.1.1 makes it a connection error'),
    ('request_after_reset_stream', 'nginx'): (_R, 'nginx answers a GET at its HEADERS, before the '
                                                  "client's RST_STREAM arrives"),
    ('request_one_byte_per_write', 'nghttpd'): (_R, 'nghttpd ends a connection whose preface arrives '
                                                    'in pieces (later frames may); RFC 9113 §3.4 '
                                                    'fixes only the octets, and TCP has no boundaries'),
    ('priority_self_dependency', 'nginx'): (_E, 'BLA-683: BlackBull resets the idle stream, which '
                                                'RFC 9113 §6.4 forbids; the references send a '
                                                'connection error'),
    ('priority_self_dependency', 'nghttpd'): (_E, 'BLA-683, as for nginx'),
}


def _ok_app():
    from http import HTTPMethod

    from blackbull import BlackBull, Response

    app = BlackBull()

    @app.route(path='/', methods=[HTTPMethod.GET])
    async def _root(conn, receive, send):
        await send(Response(b'ok'))

    return app


def _judge(name: str, case_id: str, outcomes: dict, known: dict, record_property) -> None:
    """Fail unless BlackBull agrees with every reference, or the difference is known."""
    record_property('outcomes', json.dumps({n: repr(o) for n, o in outcomes.items()}))
    findings = []
    for reference in list(outcomes)[1:]:
        category = categorize(outcomes[reference], outcomes['blackbull'])
        listed = known.get((name, reference))
        if listed is not None and category is not listed[0]:
            findings.append(f'{reference}: listed as {listed[0].value} ({listed[1]}) but now '
                            f'{category.value}')
        elif listed is None and category not in ACCEPTED_CATEGORIES:
            findings.append(f'{reference}: {category.value}')
    assert not findings, (f'{case_id}: ' + '; '.join(findings) + '\n'
                          + '\n'.join(f'  {n}: {o}' for n, o in outcomes.items()))


@pytest.mark.parametrize('case', BROKEN_CLIENTS.values(), ids=lambda c: c.id)
@pytest.mark.timeout(PEER_BUDGET)
async def test_the_server_agrees_with_both_references(case, nginx_peer, nghttpd_peer,
                                                       record_property):
    from blackbull.testing.native import NativeTestServer

    async with NativeTestServer(_ok_app()) as server:
        targets = {'blackbull': ('127.0.0.1', server.port), 'nginx': nginx_peer,
                   'nghttpd': nghttpd_peer}
        outcomes = dict(zip(targets, await asyncio.gather(
            *(run_scenario(host, port, case.build()) for host, port in targets.values()))))
    _judge(case.name, case.id, outcomes, KNOWN_SERVER, record_property)


# ---------------------------------------------------------------------------
# Client axis: one broken server, three clients
# ---------------------------------------------------------------------------

#: (case, reference client) -> (category, why), as for ``KNOWN_SERVER``.
_HTTPX_ACCEPTS = ('httpx (h2) accepts the response; RFC 9113 {} makes it malformed, and curl '
                  '(nghttp2) rejects it as BlackBull does')
KNOWN_CLIENT: dict[tuple[str, str], tuple[Category, str]] = {
    ('response_uppercase_field', 'httpx'): (_R, _HTTPX_ACCEPTS.format('§8.2.1')),
    ('response_connection_header', 'httpx'): (_R, _HTTPX_ACCEPTS.format('§8.2.2')),
    ('response_without_status', 'httpx'): (_R, _HTTPX_ACCEPTS.format('§8.3.2')),
    ('response_status_two_digits', 'httpx'): (_R, _HTTPX_ACCEPTS.format('§8.3.2')),
    ('response_pseudo_after_regular', 'httpx'): (_R, _HTTPX_ACCEPTS.format('§8.3')),
}

#: Each client's own budget; a client still waiting at the end has timed out.
_CLIENT_TIMEOUT = 5.0
#: curl's exit code for "operation timed out".
_CURLE_OPERATION_TIMEDOUT = 28


def _answered(status: int, body: bytes) -> H2Outcome:
    return H2Outcome(streams=((1, 'response', (status, body)),))


_REJECTED = H2Outcome(streams=((1, 'rejected', None),))
_TIMED_OUT = H2Outcome(streams=((1, 'timeout', None),))


async def _blackbull_client(port: int) -> H2Outcome:
    from blackbull.client.http2 import HTTP2Client

    try:
        async with HTTP2Client('127.0.0.1', port) as client:
            response = await asyncio.wait_for(client.request('GET', '/'), _CLIENT_TIMEOUT)
    except TimeoutError:
        return _TIMED_OUT
    except Exception:  # noqa: BLE001 — any refusal is the outcome being compared
        return _REJECTED
    return _answered(response.status, response.body)


async def _httpx_client(port: int) -> H2Outcome:
    httpx = pytest.importorskip('httpx')
    try:
        async with httpx.AsyncClient(http1=False, http2=True, timeout=_CLIENT_TIMEOUT) as client:
            response = await client.get(f'http://127.0.0.1:{port}/')
    except httpx.TimeoutException:
        return _TIMED_OUT
    except httpx.HTTPError:
        return _REJECTED
    return _answered(response.status_code, response.content)


async def _curl_client(port: int) -> H2Outcome:
    proc = await asyncio.create_subprocess_exec(
        'curl', '-s', '--http2-prior-knowledge', '--max-time', str(_CLIENT_TIMEOUT),
        '-w', '\n%{http_code}', f'http://127.0.0.1:{port}/',
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, _ = await asyncio.wait_for(proc.communicate(), _CLIENT_TIMEOUT + 10)
    if proc.returncode == _CURLE_OPERATION_TIMEDOUT:
        return _TIMED_OUT
    if proc.returncode != 0:
        return _REJECTED
    body, _, status = out.rpartition(b'\n')
    return _answered(int(status), body)


@pytest.mark.skipif(shutil.which('curl') is None, reason='curl not installed')
@pytest.mark.parametrize('case', BROKEN_SERVERS.values(), ids=lambda c: c.id)
async def test_the_client_agrees_with_both_references(case, record_property):
    from blackbull.fault_injection.h2_server import H2FaultServer

    async with H2FaultServer(case.build()) as server:
        clients = {'blackbull': _blackbull_client, 'httpx': _httpx_client, 'curl': _curl_client}
        outcomes = dict(zip(clients, await asyncio.gather(
            *(client(server.port) for client in clients.values()))))
    _judge(case.name, case.id, outcomes, KNOWN_CLIENT, record_property)


# ---------------------------------------------------------------------------
# Server axis, real client stack: BlackBull's client through a perturbing proxy
# ---------------------------------------------------------------------------

#: The stream the follow-up request is recorded under: after the perturbed
#: connection, a clean one must still be served.
_FOLLOW_UP = 999


async def _probe(port: int, perturbation) -> H2Outcome:
    from blackbull.client.http2 import HTTP2Client

    async def requests(target: int) -> list:
        try:
            async with HTTP2Client('127.0.0.1', target) as client:
                return await asyncio.wait_for(asyncio.gather(
                    *(client.request('GET', '/') for _ in range(3)), return_exceptions=True),
                    _CLIENT_TIMEOUT)
        except TimeoutError:
            return [TimeoutError()] * 3
        except Exception as exc:  # noqa: BLE001 — a refused connection is an outcome
            return [exc] * 3

    def stream(sid, result):
        if isinstance(result, TimeoutError):
            return (sid, 'timeout', None)
        if isinstance(result, BaseException):
            return (sid, 'rejected', None)
        return (sid, 'response', (result.status, result.body))

    async with PerturbingProxy(port, perturbation) as proxy:
        perturbed = await requests(proxy.port)
    follow_up = (await requests(port))[0]
    return H2Outcome(streams=tuple(stream(2 * i + 1, r) for i, r in enumerate(perturbed))
                     + (stream(_FOLLOW_UP, follow_up),))


@pytest.mark.parametrize('perturbation', PERTURBATIONS, ids=lambda p: p.name)
@pytest.mark.timeout(PEER_BUDGET)
async def test_the_server_agrees_under_a_perturbed_transport(perturbation, nginx_peer,
                                                             nghttpd_peer, record_property):
    from blackbull.testing.native import NativeTestServer

    async with NativeTestServer(_ok_app()) as server:
        ports = {'blackbull': server.port, 'nginx': nginx_peer[1], 'nghttpd': nghttpd_peer[1]}
        outcomes = dict(zip(ports, await asyncio.gather(
            *(_probe(port, perturbation) for port in ports.values()))))
    assert outcomes['blackbull'].streams[-1][1] == 'response', (
        f'BlackBull stopped serving after {perturbation.name}: {outcomes["blackbull"]}')
    _judge(perturbation.name, f'probe/{perturbation.name}', outcomes, {}, record_property)


async def test_the_proxy_splits_and_resets_as_it_claims():
    """The instrument's positives: one write per octet, and a reset mid-HEADERS."""
    from blackbull.client.http2 import HTTP2Client
    from blackbull.testing.native import NativeTestServer

    by_name = {p.name: p for p in PERTURBATIONS}
    async with NativeTestServer(_ok_app()) as server:
        async with PerturbingProxy(server.port, by_name['byte_per_write']) as proxy:
            async with HTTP2Client('127.0.0.1', proxy.port) as client:
                response = await asyncio.wait_for(client.request('GET', '/'), 10)
            assert response.body == b'ok'
            assert proxy.writes_upstream > 40     # preface alone is 24 octets
        async with PerturbingProxy(server.port, by_name['reset_mid_request']) as proxy:
            with pytest.raises(Exception):
                async with HTTP2Client('127.0.0.1', proxy.port) as client:
                    await asyncio.wait_for(client.request('GET', '/'), 10)


@pytest.mark.parametrize('known,cases,references', [
    pytest.param(KNOWN_SERVER, BROKEN_CLIENTS, ('nginx', 'nghttpd'), id='server-axis'),
    pytest.param(KNOWN_CLIENT, BROKEN_SERVERS, ('httpx', 'curl'), id='client-axis'),
])
def test_every_known_divergence_names_a_case_and_a_reference(known, cases, references):
    assert all(case in cases and reference in references for case, reference in known)
    assert all(category not in ACCEPTED_CATEGORIES and why for category, why in known.values())
