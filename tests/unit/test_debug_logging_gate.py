"""Disabled DEBUG logging must cost nothing on a per-request path.

``logger.debug(...)`` with DEBUG off is not free: the call happens, the
arguments are built, ``Logger.debug`` calls ``isEnabledFor``, and 24 executed
bytecode instructions produce no output.  On HTTP/2 the server made twenty
such calls per request and on HTTP/1.1 three, which measured at 4.5 % and
1.7 % of those lanes — more than the whole receive-path regression they sit
next to.

The fix is the bargain ``@log`` already strikes and the logging guide already
documents: read the level once at import, and branch on a module constant.
Guarding with ``isEnabledFor`` at the call site is *not* enough — that is what
``debug`` does internally, and it saves 6 instructions of the 24.

These tests assert both halves: the guard is present everywhere it matters
(so it cannot rot back), and the logging still works when DEBUG is configured
the documented way (so this is a gate, not a deletion).
"""
from __future__ import annotations

import ast
import inspect
import logging

import pytest

#: Modules measured to run ``logger.debug`` on a per-request path.  Membership
#: is the claim: a module here must have no ungated debug call.
HOT_MODULES = (
    'blackbull.app',
    'blackbull.protocol.frame_types',
    'blackbull.server.http2_actor',
    'blackbull.server.sender',
)


def _ungated_debug_calls(module) -> list[str]:
    """Every ``logger.debug(...)`` not lexically inside an ``if`` guard."""
    source = inspect.getsource(module)
    tree = ast.parse(source)

    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            for inner in ast.walk(node.test):
                # ``if _DEBUG:`` or ``if logger.isEnabledFor(...):`` or a
                # local ``debug`` bound from either.
                if isinstance(inner, ast.Name) and 'DEBUG' in inner.id.upper():
                    break
                if (isinstance(inner, ast.Attribute)
                        and inner.attr == 'isEnabledFor'):
                    break
            else:
                continue
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    guarded.add(id(inner))

    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'debug'
                and 'logger' in ast.unparse(node.func.value)
                and id(node) not in guarded):
            bad.append(f'line {node.lineno}')
    return bad


@pytest.mark.parametrize('name', HOT_MODULES)
def test_no_ungated_debug_call_on_a_per_request_path(name):
    module = __import__(name, fromlist=['_'])
    bad = _ungated_debug_calls(module)
    assert not bad, (
        f'{name} calls logger.debug without a gate at {bad}.  With DEBUG off '
        f'each of those is 24 executed instructions producing nothing, once '
        f'per request.  Wrap it in `if _DEBUG:` (see blackbull.logger.'
        f'debug_gate).')


@pytest.mark.parametrize('name', HOT_MODULES)
def test_the_gate_exists_and_is_a_plain_bool(name):
    """A callable gate would put back the call the gate exists to remove.

    Its *value* is not asserted: ``pytest.ini`` sets ``log_level = debug``, so
    under the suite these read True and the guarded calls run — which is what
    keeps the logging itself covered.  Production reads False, which is the
    case the instruction count was taken in.
    """
    module = __import__(name, fromlist=['_'])
    assert hasattr(module, '_DEBUG'), (
        f'{name} has no _DEBUG gate; the guards have nothing to read')
    assert isinstance(module._DEBUG, bool), (
        f'{name}._DEBUG is {type(module._DEBUG).__name__}, not a bool — a '
        f'callable or a Logger here reinstates the per-call work')


def test_the_gate_is_false_when_the_logger_is_quiet():
    from blackbull.logger import debug_gate
    quiet = logging.getLogger('not_blackbull.quiet.gate')
    quiet.setLevel(logging.WARNING)
    assert debug_gate(quiet) is False


def test_debug_configured_before_import_still_logs():
    """The gate must not be a deletion.

    Configured the way ``docs/guide/logging.md`` prescribes — level set before
    the module is imported — the gate reads True and the debug output appears.

    In a subprocess, because the only way to observe an import-time read is a
    fresh import, and re-importing this module in-process mints a *second*
    ``frame_types`` with its own ``ErrorCodes`` enum.  Objects built from one
    then fail ``isinstance`` against the other — which is invisible to a plain
    run and fails the beartype pass, session-wide.  Reloading a module that
    defines enums is not a local act.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent('''
        import logging
        logging.basicConfig(level=logging.DEBUG)
        from blackbull.protocol import frame_types
        from blackbull.server import http2_actor
        print(frame_types._DEBUG, http2_actor._DEBUG)
    ''')
    out = subprocess.run([sys.executable, '-c', script],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ['True', 'True'], (
        f'DEBUG was configured before import and the gates still read '
        f'{out.stdout.strip()!r} — the gate is not reading the level it '
        f'claims to, so this is a deletion rather than a gate')
