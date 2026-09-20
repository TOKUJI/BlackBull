"""The Host ASCII rule rides the forbidden-byte scan, not a second decode.

`_validate_host` runs on every HTTP/1.1 request, so the non-ASCII rule (RFC
3986 §3.2 authorities are ASCII) must not pay for another pass over the value:
it lives in the one regex the function already runs.

Two questions, two answers: the octet sweeps below prove the rule is
*enforced* (nothing can drop it), and the walker proves it stays *one pass*.
The walker allow-lists the three attribute calls the function needs — two on
the raw header, one scan — and flags every other call, loop, comprehension and
subscript, so a second pass is caught whichever way it is spelled rather than
only the `decode` this change removed.  It is still a tripwire, not a proof: an
aliased value, or a read through an API none of those shapes describes, would
be missed, which is why the sweeps carry the enforcement claim.
"""
import ast
import pathlib

import pytest

import blackbull.server.http1_actor as http1_actor

_FUNCTION = '_validate_host'
_SCAN = '_HOST_FORBIDDEN_RE'

# The only attribute calls the function may make.  Anything else is a read of
# the value by another name (``decode``, ``isascii``, ``translate``, ``match``,
# ``isdisjoint``, a second pattern's ``search``, ...).
_ALLOWED_READS = frozenset({'getlist', 'strip'})
_SCAN_METHOD = 'search'
# The only bare calls: the presence check and the raise.
_ALLOWED_CALLS = frozenset({'len', 'BadRequestError'})
# The only names that may be subscripted: the header list and the match.
_ALLOWED_SUBSCRIPTS = frozenset({'hosts', 'match'})


def _validate_host_function() -> ast.FunctionDef:
    tree = ast.parse(pathlib.Path(http1_actor.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _FUNCTION:
            return node
    raise AssertionError(f'{_FUNCTION} not found')


def _second_passes(node: ast.AST) -> list[str]:
    """Every shape a second read of the value would have to take."""
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                receiver = func.value
                name = receiver.id if isinstance(receiver, ast.Name) else 'expr'
                if func.attr == _SCAN_METHOD:
                    if name != _SCAN:
                        found.append(f'{name}.{func.attr}()')
                elif func.attr not in _ALLOWED_READS:
                    found.append(f'{name}.{func.attr}()')
            elif isinstance(func, ast.Name) and func.id not in _ALLOWED_CALLS:
                found.append(f'{func.id}()')
        elif isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp,
                                ast.GeneratorExp, ast.For, ast.While)):
            found.append(type(child).__name__)
        elif (isinstance(child, ast.Subscript)
              and isinstance(child.value, ast.Name)
              and child.value.id not in _ALLOWED_SUBSCRIPTS):
            found.append(f'{child.value.id}[...]')
    return found


def _scan_calls(node: ast.AST) -> int:
    return sum(
        1 for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == _SCAN_METHOD
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == _SCAN
    )


def test_validate_host_reads_the_value_once():
    function = _validate_host_function()
    assert _second_passes(function) == []
    assert _scan_calls(function) == 1


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


_SPELLINGS = {
    'bytes decode': 'value.decode("ascii")',
    'bytes isascii': 'value.isascii()',
    'str constructor': 'str(value, "ascii")',
    'codecs module': 'codecs.decode(value, "ascii")',
    'bytes translate': 'value.translate(None, forbidden)',
    'bytes contains': 'value.__contains__(0x80)',
    'bytes slice': 'value[1:]',
    'frozenset isdisjoint': '_HOST_FORBIDDEN_BYTES.isdisjoint(value)',
    'frozenset intersection': '_HOST_FORBIDDEN_BYTES.intersection(value)',
    'helper call': '_is_ascii(value)',
    'getattr lookup': 'getattr(value, "decode")("ascii")',
    'all comprehension': 'all(b < 0x80 for b in value)',
    'itertools filter': 'itertools.filterfalse(_bad, value)',
    'second pattern': '_ASCII_RE.search(value)',
    're module search': 're.search(_ASCII_RE, value)',
    're module match': 're.match(_ASCII_RE, value)',
    'explicit loop': 'for b in value:\n        pass',
}


@pytest.mark.parametrize('source', _SPELLINGS.values(), ids=_SPELLINGS)
def test_the_walker_sees_every_spelling_of_a_second_pass(source):
    body = '\n    '.join(source.split('\n'))
    sample = ast.parse(f'def f(value):\n    {body}\n')
    assert _second_passes(sample) != [], source


def test_the_walker_counts_a_duplicate_scan():
    sample = ast.parse('def f(value):\n'
                       f'    {_SCAN}.search(value)\n'
                       f'    return {_SCAN}.search(value)\n')
    assert _scan_calls(sample) == 2


def _with_extra_line(line: str) -> list[str]:
    """The real body with *line* spliced in before the scan, as a regression.

    Anchored on the scan expression rather than the statement around it, so a
    rebinding of the result (``match = ...`` vs ``if match := ...``) does not
    decide whether the tripwire fires.
    """
    body = ast.unparse(_validate_host_function()).splitlines()
    for index, text in enumerate(body):
        if _SCAN in text:
            indent = text[:len(text) - len(text.lstrip())]
            break
    else:
        pytest.fail(f'{_FUNCTION} no longer scans with {_SCAN}: update this '
                    f'guard rather than deleting it')
    body.insert(index, f'{indent}{line}')
    return _second_passes(ast.parse('\n'.join(body)))


def test_the_walker_sees_the_decode_when_it_is_put_back():
    assert _with_extra_line('value.decode("ascii")') != []


def test_the_walker_sees_a_second_pattern_when_it_is_added():
    assert _with_extra_line('_ASCII_RE.search(value)') != []
