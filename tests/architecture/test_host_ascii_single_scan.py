"""The Host ASCII rule rides the forbidden-byte scan, not a second decode.

`_validate_host` runs on every HTTP/1.1 request, so the non-ASCII rule (RFC
3986 §3.2 authorities are ASCII) must not pay for another pass over the value:
it lives in the one regex the function already runs.  A `decode` — or an
`isascii` — call in that function is the regression this file exists to catch,
and the octet sweep is there so the rule cannot be dropped instead of moved.
"""
import ast
import pathlib

import blackbull.server.http1_actor as http1_actor

_SCAN_CALLS = frozenset({'decode', 'isascii'})


def _validate_host_function() -> ast.FunctionDef:
    tree = ast.parse(pathlib.Path(http1_actor.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_validate_host':
            return node
    raise AssertionError('_validate_host not found')


def _second_scan_calls(node: ast.AST) -> list[str]:
    return [
        call.func.attr for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in _SCAN_CALLS
    ]


def test_validate_host_does_not_rescan_the_value():
    assert _second_scan_calls(_validate_host_function()) == []


def test_the_single_scan_rejects_every_non_ascii_octet():
    regex = http1_actor._HOST_FORBIDDEN_RE
    missed = [b for b in range(0x80, 0x100)
              if regex.search(bytes([b])) is None]
    assert missed == [], f'high bytes the Host scan accepts: {missed}'


def test_the_single_scan_still_covers_the_delimiter_set():
    regex = http1_actor._HOST_FORBIDDEN_RE
    missed = [b for b in sorted(http1_actor._HOST_FORBIDDEN_BYTES)
              if regex.search(bytes([b])) is None]
    assert missed == [], f'forbidden bytes the Host scan accepts: {missed}'


def test_the_single_scan_leaves_a_real_authority_alone():
    assert http1_actor._HOST_FORBIDDEN_RE.search(b'example.com:8080') is None


def test_the_walker_finds_a_second_scan():
    """Guard the guard: a walker that finds nothing is not evidence."""
    sample = ast.parse('def f(value):\n'
                       '    value.decode("ascii")\n'
                       '    return value.isascii()\n')
    assert sorted(_second_scan_calls(sample)) == ['decode', 'isascii']
