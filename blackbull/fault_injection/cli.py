"""``blackbull-client``: run a client fault scenario, or compare two targets.

``run`` executes one scenario JSON file against ``--target``; ``oracle`` runs
it against ``--target`` and ``--reference`` and compares the outcomes with
``oracle_h1`` or ``oracle_h2``.  Exit codes: 0 ok, 1 divergence (a category
outside the accepted ones, or a run whose ``Expect…`` step did not match),
2 usage (including a refused production context or remote target), 3 a
transport or harness failure.  Nothing is sent before the checks pass.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl as _ssl
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from . import oracle_h1, oracle_h2
from ._scenario import unsafe_reason
from .scenario_h1 import Scenario
from .scenario_h2_client import scenario_from_json as _h2_from_json

EXIT_OK, EXIT_DIVERGED, EXIT_USAGE, EXIT_TRANSPORT = 0, 1, 2, 3

_LOADERS = {'h1': Scenario.from_json, 'h2': _h2_from_json}
_ALPN = {'h1': 'http/1.1', 'h2': 'h2'}


class _Usage(Exception):
    """An invocation refused before any byte is sent."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='blackbull-client', description=__doc__.split('\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('run', 'oracle'):
        sub = commands.add_parser(name)
        sub.add_argument('scenario', type=Path, help='scenario JSON Lines file')
        sub.add_argument('--protocol', required=True, choices=sorted(_LOADERS))
        sub.add_argument('--target', required=True, help='host:port')
        if name == 'oracle':
            sub.add_argument('--reference', required=True, help='host:port')
        sub.add_argument('--tls', action='store_true')
        trust = sub.add_mutually_exclusive_group()
        trust.add_argument('--cafile', type=Path, help='trust this CA (with --tls)')
        trust.add_argument('--insecure', action='store_true',
                           help='do not verify the certificate (with --tls)')
        sub.add_argument('--allow-remote', action='store_true',
                         help='permit a non-loopback target')
        sub.add_argument('--json', type=Path, help='write the result record here')
        sub.add_argument('--junit', type=Path, help='write a JUnit report here')
    return parser


def _address(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(':')
    if not sep or not host or not port.isdigit():
        raise _Usage(f'{text!r} is not host:port')
    return host.strip('[]'), int(port)


def _tls(args) -> _ssl.SSLContext | None:
    if not args.tls:
        if args.cafile or args.insecure:
            raise _Usage('--cafile and --insecure need --tls')
        return None
    context = _ssl.create_default_context()
    if args.cafile:
        try:
            context.load_verify_locations(cafile=args.cafile)
        except (OSError, _ssl.SSLError) as exc:
            raise _Usage(f'cannot load --cafile {args.cafile}: {exc}') from exc
    if args.insecure:
        context.check_hostname = False
        context.verify_mode = _ssl.CERT_NONE
    context.set_alpn_protocols([_ALPN[args.protocol]])
    return context


def _prepare(args):
    try:
        scenario = _LOADERS[args.protocol](args.scenario.read_text())
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise _Usage(f'cannot read {args.scenario} as an {args.protocol} scenario: {exc}') from exc
    addresses = [_address(args.target)]
    if args.command == 'oracle':
        addresses.append(_address(args.reference))
        if args.protocol == 'h2':
            for step in scenario.steps:
                if not isinstance(step, oracle_h2.STEPS):
                    raise _Usage(f'{type(step).__name__}: the h2 oracle reads every frame '
                                 'itself, so a scenario for it has no read steps')
    for host, _ in addresses:
        reason = unsafe_reason('blackbull-client', 'requests', host, args.allow_remote,
                               action='connect to')
        if reason is not None:
            raise _Usage(reason)
    return scenario, addresses, _tls(args)


# -- run ----------------------------------------------------------------------

def _steps(scenario, completed: int) -> list[dict]:
    return [{'op': type(step).__name__,
             'wire': step.wire().hex() if hasattr(step, 'wire') else None,
             'completed': i < completed}
            for i, step in enumerate(scenario.steps)]


def _response(response: dict) -> dict:
    return {**response, 'headers': [[k.decode('latin-1'), v.decode('latin-1')]
                                     for k, v in response['headers']],
            'body': response['body'].hex()}


async def _run_h1(scenario, host, port, context) -> dict:
    outcome, _ = await oracle_h1.run_scenario(host, port, scenario, ssl=context)
    result = outcome.result
    code = {oracle_h1.Observed.ERROR: EXIT_TRANSPORT,
            oracle_h1.Observed.UNMET: EXIT_DIVERGED}.get(outcome.observed, EXIT_OK)
    return dict(exit=code, steps=_steps(scenario, result.steps_completed if result else 0),
                outcome=dict(observed=outcome.observed.value, exception=outcome.exception,
                             responses=[_response(r) for r in outcome.responses]))


def _frame(frame) -> dict:
    record = {'type': type(frame).__name__, 'stream_id': getattr(frame, 'stream_id', None)}
    if getattr(frame, 'error_code', None) is not None:
        record['error_code'] = int(frame.error_code)
    return record


async def _run_h2(scenario, host, port, context) -> dict:
    from ..client.exceptions import ClientError  # noqa: PLC0415 — the client imports this package
    from ..client.http2 import HTTP2Client  # noqa: PLC0415

    try:
        async with HTTP2Client(host, port, ssl=context, scenario_mode=True,
                               connect_timeout=2.0) as client:
            result = await asyncio.wait_for(client.execute_scenario(scenario),
                                            oracle_h2.PER_SCENARIO_TIMEOUT_S)
    except (OSError, TimeoutError, ClientError) as exc:
        return dict(exit=EXIT_TRANSPORT, steps=_steps(scenario, 0),
                    outcome=dict(exception=repr(exc), frames=[]))
    unmet = result.wait_timed_out or any(not held for _, held in result.expectations)
    return dict(exit=EXIT_DIVERGED if unmet else EXIT_OK,
                steps=_steps(scenario, result.steps_completed),
                outcome=dict(exception=result.exception, peer_closed=result.peer_closed,
                             frames=[_frame(f) for f in result.received]))


# -- oracle -------------------------------------------------------------------

async def _oracle(protocol, scenario, addresses, context) -> dict:
    if protocol == 'h1':
        (target, _), (reference, _) = [await oracle_h1.run_scenario(h, p, scenario, ssl=context)
                                       for h, p in addresses]
        category, accepted = oracle_h1.categorize(reference, target), oracle_h1.ACCEPTED_CATEGORIES
        harness = oracle_h1.Category.HARNESS_ERROR
        show = lambda o: dict(observed=o.observed.value, exception=o.exception,  # noqa: E731
                              responses=[_response(r) for r in o.responses])
    else:
        target, reference = await asyncio.gather(
            *(oracle_h2.run_scenario(h, p, scenario, ssl=context) for h, p in addresses))
        category, accepted = oracle_h2.categorize(reference, target), oracle_h2.ACCEPTED_CATEGORIES
        harness = oracle_h2.Category.HARNESS_ERROR
        show = repr
    code = (EXIT_TRANSPORT if category is harness
            else EXIT_OK if category in accepted else EXIT_DIVERGED)
    return dict(exit=code, category=category.value,
                outcome=dict(target=show(target), reference=show(reference)))


# -- reports ------------------------------------------------------------------

def _junit(path: Path, command: str, protocol: str, scenario, record: dict) -> None:
    suite = ET.Element('testsuite', name='blackbull-client', tests='1',
                       failures=str(int(record['exit'] == EXIT_DIVERGED)),
                       errors=str(int(record['exit'] == EXIT_TRANSPORT)))
    case = ET.SubElement(suite, 'testcase', classname=f'{command}.{protocol}',
                         name=scenario.name or 'scenario')
    if record['exit'] == EXIT_DIVERGED:
        ET.SubElement(case, 'failure', message=record.get('category', 'an expectation failed'))
    elif record['exit'] == EXIT_TRANSPORT:
        ET.SubElement(case, 'error', message='transport or harness failure')
    ET.SubElement(case, 'system-out').text = json.dumps(record)
    ET.ElementTree(suite).write(path, encoding='utf-8', xml_declaration=True)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:   # argparse: --help is 0, a usage error 2
        return exc.code
    try:
        scenario, addresses, context = _prepare(args)
    except _Usage as exc:
        print(f'blackbull-client: {exc}', file=sys.stderr)
        return EXIT_USAGE
    if args.command == 'run':
        runner = _run_h1 if args.protocol == 'h1' else _run_h2
        work = runner(scenario, *addresses[0], context)
    else:
        work = _oracle(args.protocol, scenario, addresses, context)
    try:
        record = asyncio.run(work)
    except Exception as exc:  # noqa: BLE001 — nothing a runner raises may read as divergence
        print(f'blackbull-client: {exc!r}', file=sys.stderr)
        record = dict(exit=EXIT_TRANSPORT, outcome=dict(exception=repr(exc)))
    record = dict(command=args.command, protocol=args.protocol, scenario=scenario.name,
                  target=args.target, **record)
    try:
        if args.json:
            args.json.write_text(json.dumps(record, indent=1))
        if args.junit:
            _junit(args.junit, args.command, args.protocol, scenario, record)
    except OSError as exc:
        print(f'blackbull-client: cannot write the report: {exc}', file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps({k: record[k] for k in ('command', 'exit', 'category') if k in record}))
    return record['exit']


if __name__ == '__main__':
    sys.exit(main())
