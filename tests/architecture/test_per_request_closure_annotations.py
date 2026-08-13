"""Per-request closures must not carry annotations.

A nested ``def`` pays for its annotations on **every creation**: CPython builds
an ``__annotate__`` closure alongside the function object.  PEP 649 defers
*evaluating* an annotation, not building the machinery that would evaluate it.
Directly measured on 3.14, min-of-5 over 2M iterations::

    async def i(e):                 76 ns
    async def i(e) -> None:        148 ns   (+72)
    async def i(e: T):             169 ns   (+92)
    async def i(e: T) -> None:     170 ns   (+93)

A bare ``-> None`` therefore costs ~78% of a full annotation — dropping only
the parameter annotation recovers less than a quarter of it.  Sprint 81
confirmed the end-to-end shape: annotating three per-request send closures cost
+0.23 µs/req (+3.4%) on the H/1.1 dispatch micro-driver.

The type information is not lost, it moves to a comment at each site.  Nothing
statically checks these signatures anyway — they are closures internal to an
actor, not public surface, and not in pyright's gate scope.

**Why a test and not a convention.**  Sprint 81 fixed five sites and wrote the
rule into the cautions file; the four HTTP/2 sites below survived that sweep
anyway, because a rule that lives in prose is enforced by whoever remembers to
read it.  The same lesson produced ``test_typing_gate``'s ``Final`` check: what
a type checker cannot see needs a structural assertion or it rots.

Registration-time and startup-time factories are deliberately out of scope —
they run once and cost nothing per request.  This asserts only about factories
that run per request, per stream, or per RPC.
"""
import ast
import pathlib

import pytest

# Factories whose nested defs are built per request / per stream / per RPC.
# A closure created inside one of these pays its annotation cost on every
# creation, so none of them may carry annotations.
#
# Adding a factory here is how the guard grows; the point of naming them is
# that the sweep stays honest about scope rather than flagging registration-
# time and startup-time defs, which are free.
PER_REQUEST_FACTORIES = {
    'blackbull/server/http2_actor.py': {
        '_make_stream_recipient',      # once per stream
        '_make_done_cb',               # once per stream
        '_release_recipient_credit',   # once per stream release
        '_handle_h2_websocket',        # once per H/2 WebSocket connection
    },
    'blackbull/grpc/asgi.py': {
        '_serve_server_streaming',     # once per server-streaming RPC
    },
}

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _annotated_nested_defs(path: str, factory_names: set[str]):
    """Every annotated ``def`` nested inside one of *factory_names*.

    Yields ``(factory, closure, lineno, what)`` where *what* names the
    offending annotations, so a failure says which ones to strip.
    """
    tree = ast.parse((_REPO_ROOT / path).read_text(encoding='utf-8'))
    for outer in ast.walk(tree):
        if not isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if outer.name not in factory_names:
            continue
        for node in ast.walk(outer):
            if node is outer:
                continue
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            annotated = [a.arg for a in (*args.posonlyargs, *args.args,
                                         *args.kwonlyargs) if a.annotation]
            for a in (args.vararg, args.kwarg):
                if a is not None and a.annotation:
                    annotated.append(a.arg)
            what = []
            if annotated:
                what.append('params ' + ', '.join(annotated))
            if node.returns is not None:
                what.append('return')
            if what:
                yield outer.name, node.name, node.lineno, ' + '.join(what)


@pytest.mark.parametrize('path', sorted(PER_REQUEST_FACTORIES))
def test_no_annotated_closure_in_a_per_request_factory(path):
    offenders = list(_annotated_nested_defs(path, PER_REQUEST_FACTORIES[path]))
    assert not offenders, (
        'Annotated nested def(s) in a per-request factory — each costs '
        '~90 ns per creation. Strip the annotations and record the accepted '
        'shape in a comment (see blackbull/app.py::_wrap_send):\n'
        + '\n'.join(f'  {path}:{line}  {factory}::{closure}  ({what})'
                    for factory, closure, line, what in offenders))


def test_the_sweep_can_actually_see_an_annotation():
    """Guard the guard.

    A structural test that silently stops matching is worse than no test: it
    reports success forever.  This drives the same walker over a known-bad
    sample, so a refactor that breaks the AST matching fails here rather than
    quietly passing everything above.
    """
    sample = _REPO_ROOT / 'tests' / 'architecture' / '_annotated_sample.py'
    sample.write_text(
        'def factory():\n'
        '    async def inner(n: int) -> None:\n'
        '        pass\n'
        '    return inner\n', encoding='utf-8')
    try:
        found = list(_annotated_nested_defs(
            str(sample.relative_to(_REPO_ROOT)), {'factory'}))
    finally:
        sample.unlink()

    assert len(found) == 1
    factory, closure, _, what = found[0]
    assert (factory, closure) == ('factory', 'inner')
    assert 'params n' in what and 'return' in what
