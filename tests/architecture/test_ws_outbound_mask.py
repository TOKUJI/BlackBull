"""The WebSocket recipient's outbound frames take their mask from one place.

RFC 6455 §5.1 — a client MUST mask every frame it sends and a server MUST NOT.
``WebSocketRecipient`` is the one endpoint whose role is a runtime flag
(``require_masked``), so it is the one place the mask has to be derived rather
than chosen: every frame it emits goes through ``_encode_frame``.

**Why a test and not a comment.**  The unknown-opcode CLOSE called
``encode_frame`` directly and went out unmasked to a client peer — the other
six sites happened to pass the flag.  A rule that lives in the six correct call
sites is enforced by whoever remembers all seven.
"""
import ast
import pathlib

_RECIPIENT = (pathlib.Path(__file__).resolve().parents[2]
              / 'blackbull' / 'server' / 'recipient.py')


def _enclosing(node, parents) -> str:
    """The nearest enclosing function's name, or ``<module>``."""
    scope = node
    while scope in parents:
        scope = parents[scope]
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return scope.name
    return '<module>'


def _inside_encode_frame(node, parents) -> bool:
    """Whether *node* sits inside ``_encode_frame``, however deeply nested."""
    scope = node
    while scope in parents:
        scope = parents[scope]
        if (isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
                and scope.name == '_encode_frame'):
            return True
    return False


def _bare_encode_frame_calls(path) -> list[tuple[str, int]]:
    """``(enclosing function, line)`` for every ``encode_frame`` call outside
    ``_encode_frame``, which is the one place that derives the mask.

    An aliased import or ``getattr`` would evade this; the sweep matches the
    module's own name for the codec entry point.
    """
    tree = ast.parse(pathlib.Path(path).read_text(encoding='utf-8'))
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        named = (isinstance(func, ast.Name) and func.id == 'encode_frame')
        dotted = (isinstance(func, ast.Attribute)
                  and func.attr == 'encode_frame')
        if (named or dotted) and not _inside_encode_frame(node, parents):
            offenders.append((_enclosing(node, parents), node.lineno))
    return offenders


def test_the_recipient_encodes_through_its_one_mask_site():
    offenders = _bare_encode_frame_calls(_RECIPIENT)
    assert not offenders, (
        'encode_frame called outside WebSocketRecipient._encode_frame — the mask '
        'depends on the endpoint role, so it belongs in that one place:\n'
        + '\n'.join(f'  {name}:{line}' for name, line in offenders))


def test_the_sweep_can_actually_see_a_bare_call(tmp_path):
    """Guard the guard: a structural test that silently stops matching is
    worse than no test, because it reports success forever.

    The sample carries the three shapes that matter: a module-level call, a
    dotted call inside another function, and a helper nested inside ``_encode_frame``
    that a name-based sweep would report as a false positive.
    """
    sample = tmp_path / 'sample.py'
    sample.write_text(
        'encode_frame(0)\n'
        '\n'
        'def _encode_frame():\n'
        '    def helper():\n'
        '        return encode_frame(1)\n'
        '    return helper\n'
        '\n'
        'def other():\n'
        '    return ws_codec.encode_frame(2)\n', encoding='utf-8')
    assert [name for name, _ in _bare_encode_frame_calls(sample)] == \
        ['<module>', 'other']
