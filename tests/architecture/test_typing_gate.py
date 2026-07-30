"""Static type-check gate for the ASGI message channel.

Runs pyright over the narrow scope configured in `pyproject.toml`
(`[tool.pyright]`: `blackbull/asgi.py` + `tests/typing`) and asserts both
directions of the proof:

- `tests/typing/assert_narrowing.py` — zero diagnostics.  Proves the
  `ASGIEvent` constants are `Literal` (the `Final` keystone), that
  `event['type']` narrows `ASGIReceiveEvent` / `ASGISendEvent` to a single
  member by both `==` and `match`, and that all 19 shapes construct.
- `tests/typing/expect_errors.py` — at least one diagnostic on **every** line
  carrying an `# EXPECT-ERROR` comment, and none anywhere else.  Without this
  half, the gate would still pass if the unions silently degraded to `Any`.

The `# EXPECT-ERROR` tags are located by tokenising the file rather than by
substring search, so prose mentioning the tag (in this docstring, or in
`expect_errors.py`'s own) is never mistaken for a tagged line.

Skips when pyright is unavailable rather than failing — the gate is
authoritative in CI, and a contributor without the `[testing]` extra installed
should not see a red suite.
"""
import io
import json
import pathlib
import subprocess
import sys
import tokenize

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TYPING_DIR = REPO_ROOT / 'tests' / 'typing'
POSITIVE = TYPING_DIR / 'assert_narrowing.py'
NEGATIVE = TYPING_DIR / 'expect_errors.py'

EXPECT_ERROR_TAG = 'EXPECT-ERROR'


def _expect_error_lines(path: pathlib.Path) -> set[int]:
    """1-indexed lines carrying an ``# EXPECT-ERROR`` *comment*.

    Tokenising means a docstring or string literal that merely mentions the
    tag is not counted — only real comments are.
    """
    source = path.read_text()
    tagged: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT and EXPECT_ERROR_TAG in tok.string:
            tagged.add(tok.start[0])
    return tagged


@pytest.fixture(scope='module')
def pyright_diagnostics() -> dict[str, dict[int, list[str]]]:
    """Run pyright once; return {filename: {line: [rule, ...]}}."""
    try:
        proc = subprocess.run(
            [sys.executable, '-m', 'pyright', '--outputjson'],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f'pyright unavailable or timed out: {exc!r}')

    # pyright exits non-zero whenever diagnostics exist — which is the
    # expected state here (expect_errors.py is full of them).  Only a missing
    # module or an unparseable payload is a real failure.
    if 'No module named pyright' in proc.stderr:
        pytest.skip('pyright not installed (pip install -e ".[testing]")')
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.fail(
            f'pyright produced no JSON (exit {proc.returncode})\n'
            f'stdout: {proc.stdout[:2000]}\nstderr: {proc.stderr[:2000]}'
        )

    by_file: dict[str, dict[int, list[str]]] = {}
    for diag in payload['generalDiagnostics']:
        name = pathlib.Path(diag['file']).name
        line = diag['range']['start']['line'] + 1        # pyright is 0-indexed
        label = diag.get('rule') or diag['severity']
        by_file.setdefault(name, {}).setdefault(line, []).append(label)
    return by_file


def test_positive_proof_is_clean(pyright_diagnostics):
    """`assert_narrowing.py` must type-check with zero diagnostics.

    A failure here means the unions stopped narrowing — most likely because
    the `Final` annotations on `ASGIEvent` were dropped, which silently
    demotes every constant back to `str`.
    """
    found = pyright_diagnostics.get(POSITIVE.name, {})
    assert not found, (
        f'{POSITIVE.name} must be diagnostic-free; got: '
        + '; '.join(f'line {ln}: {", ".join(rules)}'
                    for ln, rules in sorted(found.items()))
    )


def test_declarations_are_clean(pyright_diagnostics):
    """`blackbull/asgi.py` itself must type-check clean."""
    found = pyright_diagnostics.get('asgi.py', {})
    assert not found, (
        'blackbull/asgi.py must be diagnostic-free; got: '
        + '; '.join(f'line {ln}: {", ".join(rules)}'
                    for ln, rules in sorted(found.items()))
    )


def test_only_the_negative_file_has_diagnostics(pyright_diagnostics):
    """`expect_errors.py` is the *only* file in scope allowed to error.

    Named-file assertions alone would let a newly added `tests/typing/`
    module sit in the gate's scope failing silently, since nothing would
    look at it.
    """
    offenders = sorted(set(pyright_diagnostics) - {NEGATIVE.name})
    assert not offenders, (
        f'unexpected diagnostics outside {NEGATIVE.name}: '
        + '; '.join(
            f'{name} (line(s) {sorted(pyright_diagnostics[name])})'
            for name in offenders
        )
    )


def test_every_expected_error_is_reported(pyright_diagnostics):
    """Each `# EXPECT-ERROR` line must draw at least one diagnostic.

    This is the half that proves the types have teeth — including the 0.43.2
    regression (a `Response` object passed to an `ASGISendCallable`), which
    `tests/unit/test_middleware_decorator.py` catches at runtime.
    """
    tagged = _expect_error_lines(NEGATIVE)
    assert tagged, f'no {EXPECT_ERROR_TAG} comments found in {NEGATIVE.name}'

    found = pyright_diagnostics.get(NEGATIVE.name, {})
    missing = sorted(tagged - found.keys())
    assert not missing, (
        f'{NEGATIVE.name}: no diagnostic on {EXPECT_ERROR_TAG} line(s) '
        f'{missing} — these unsound cases now type-check'
    )


def test_no_unexpected_errors_in_negative_file(pyright_diagnostics):
    """Nothing in `expect_errors.py` errors *except* the tagged lines.

    Guards the sanctioned `cast` escape hatch that Phase 2's annotation sweep
    relies on, and keeps stray breakage from masquerading as expected failure.
    """
    tagged = _expect_error_lines(NEGATIVE)
    found = pyright_diagnostics.get(NEGATIVE.name, {})
    unexpected = sorted(set(found) - tagged)
    assert not unexpected, (
        f'{NEGATIVE.name}: unexpected diagnostic(s) on untagged line(s) '
        + '; '.join(f'{ln}: {", ".join(found[ln])}' for ln in unexpected)
    )


def test_every_asgi_event_constant_is_final():
    """Every `ASGIEvent` constant carries a `Final` annotation.

    This is asserted structurally, from the AST, because **pyright cannot
    catch its absence**: pyright infers `Literal['http.request']` for a bare
    class-body assignment, so the narrowing proofs in `assert_narrowing.py`
    keep passing with every `Final` stripped.  mypy does not — it infers plain
    `str` there, and every `event['type'] == ASGIEvent.X` comparison stops
    narrowing.  So `Final` is load-bearing for mypy users, invisible to the
    pyright gate, and would otherwise rot silently.
    """
    import ast

    tree = ast.parse((REPO_ROOT / 'blackbull' / 'asgi.py').read_text())
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == 'ASGIEvent')

    bare = [n.targets[0].id for n in cls.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)]
    assert not bare, (
        f'ASGIEvent constants missing `Final`: {bare} — mypy will infer `str` '
        'and no comparison against them narrows the event unions'
    )

    annotated = [n for n in cls.body if isinstance(n, ast.AnnAssign)]
    non_final = [n.target.id for n in annotated
                 if not (isinstance(n.annotation, ast.Name)
                         and n.annotation.id == 'Final')]
    assert not non_final, f'ASGIEvent constants not annotated `Final`: {non_final}'
    assert len(annotated) == 19, (
        f'expected 19 ASGI event constants, found {len(annotated)}'
    )


def test_pyright_scope_is_narrow():
    """The gate's scope stays deliberately small.

    Whole-repo pyright would drown in pre-existing diagnostics; if someone
    widens `include` to the package, this gate stops meaning "the message
    channel narrows" and starts meaning "the repo is pyright-clean", which
    it is not.

    Growing by a *named module written against the declarations* is the
    intended way for this list to change — `blackbull/websocket.py` (the
    82) constructs the WebSocket event shapes, so type-checking it proves the
    declarations hold where they are actually used.  Each addition is a
    reviewed decision, which is exactly what an assertion on the literal set
    forces.  What must never appear is a directory that pulls in code the
    declarations had nothing to do with.
    """
    import tomllib

    with open(REPO_ROOT / 'pyproject.toml', 'rb') as fh:
        config = tomllib.load(fh)

    include = config['tool']['pyright']['include']
    assert set(include) == {
        'blackbull/asgi.py',
        'blackbull/websocket.py',
        'tests/typing',
    }, include
    package_entries = [p for p in include if p.startswith('blackbull/')]
    assert all(p.endswith('.py') for p in package_entries), (
        'the gate covers named modules, not package directories: '
        f'{package_entries}')
