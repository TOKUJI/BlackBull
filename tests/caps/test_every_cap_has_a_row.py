"""Every cap the code can record has a row, on every protocol that enforces it.

``ENFORCED`` is the inventory of (cap, subsystem) pairs. The source scan and
wire tests must agree with it; unresolved forwarding requires an explicit exemption.
"""
import ast
import pathlib

import pytest

from . import test_count_caps, test_rate_caps, test_size_caps, test_time_caps
from ._table import rows_of

ENFORCED: dict[str, set[str]] = {
    'body_timeout': {'http1'},
    'client_body_max_total': {'http1', 'http2'},
    'client_body_timeout': {'http1', 'http2'},
    'client_h2_max_frame_size': {'http2'},
    'client_h2_max_header_list_size': {'http2'},
    'client_head_max_line': {'http1'},
    'client_head_max_total': {'http1', 'http2'},
    'client_head_timeout': {'http1', 'http2'},
    'client_max_interim_responses': {'http1', 'http2'},
    'client_min_body_rate': {'http1', 'http2'},
    'client_raw_queue_depth': {'http2'},
    'client_write_timeout': {'http1', 'http2', 'ws'},
    'client_ws_max_frame_payload': {'ws'},
    'client_ws_max_message_size': {'ws'},
    'compression_max_inflight': {'compression'},
    'frame_rate': {'http2', 'ws'},
    'h1_chunk_line_length': {'http1'},
    'h2_inbound_window_budget': {'http2'},
    'h2_max_concurrent_streams': {'http2'},
    'h2_priority_update_buffer': {'http2'},
    'h2_ws_max_streams_per_connection': {'h2-ws'},
    'header_max_line': {'http1'},
    'header_max_total': {'http1', 'http2'},
    'header_timeout': {'http1', 'http2'},
    'max_body_size': {'http1', 'http2'},
    'max_connections': {'tcp'},
    'min_body_rate': {'http1', 'http2'},
    'mqtt_broker_inbox_max_bytes': {'mqtt'},
    'mqtt_broker_inbox_maxsize': {'mqtt'},
    'mqtt_connection_inbox_max_bytes': {'mqtt'},
    'mqtt_connection_inbox_maxsize': {'mqtt'},
    'mqtt_max_packet_size': {'mqtt'},
    'mqtt_max_queued_messages': {'mqtt'},
    'mqtt_max_retained': {'mqtt'},
    'mqtt_max_sessions': {'mqtt'},
    'mqtt_max_subscriptions': {'mqtt'},
    'request_timeout': {'http1', 'http2'},
    'socket_backlog': {'transport'},
    'stream_queue_depth': {'http2'},
    'write_timeout': {'transport', 'http2'},
    'ws_max_frame_payload': {'ws'},
    'ws_max_message_size': {'ws'},
}

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / 'blackbull'

SUBSYSTEMS = {'http1', 'http2', 'ws', 'h2-ws', 'mqtt', 'tcp', 'transport', 'compression', 'unknown'}
EXEMPT = {
    ('server/connection_actor.py', 'ConnectionActor._on_detect_timeout'):
        ('header_timeout', 'proto', "binding.name if binding is not None else 'unknown'",
         'Registered binding names are open; no binding is named unknown.'),
}


class _Audit:
    """Resolve the literal/default/attribute forwarding used by cap refusals."""

    def __init__(self, sources):
        self.trees = {path: ast.parse(text) for path, text in sources.items()}
        self.parents = {c: n for tree in self.trees.values()
                        for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        self.calls = [n for tree in self.trees.values() for n in ast.walk(tree)
                      if isinstance(n, ast.Call)]
        self.classes = [n for tree in self.trees.values() for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        self.functions = [n for tree in self.trees.values() for n in ast.walk(tree)
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.names = {}
        for tree in self.trees.values():
            aliases = {a.asname: a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                       for a in n.names if a.asname}
            self.names.update((n, aliases.get(n.id, n.id)) for n in ast.walk(tree) if isinstance(n, ast.Name))

    def owner(self, node, kind):
        while node is not None and not isinstance(node, kind):
            node = self.parents.get(node)
        return node

    def bindings(self, func):
        cls = self.owner(func, ast.ClassDef)
        name = cls.name if func.name == '__init__' else func.name
        names = {name}
        if func.name == '__init__':
            while children := {c.name for c in self.classes if c.name not in names
                               and any(isinstance(b, ast.Name) and self.names[b] in names for b in c.bases)
                               and not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                           and n.name == '__init__' for n in c.body)}:
                names |= children
            if any(sum(c.name == name for c in self.classes) != 1 for name in names):
                raise ValueError('ambiguous cap constructor')
        elif sum(f.name == name for f in self.functions) != 1:
            raise ValueError('ambiguous cap forwarding helper')
        positional = func.args.posonlyargs + func.args.args
        defaults = dict(zip([a.arg for a in positional[-len(func.args.defaults):]], func.args.defaults))
        defaults.update((a.arg, v) for a, v in zip(func.args.kwonlyargs, func.args.kw_defaults)
                        if v is not None)
        positional = [a for a in positional if a.arg not in ('self', 'cls')]
        for call in self.calls:
            if self.names.get(call.func, getattr(call.func, 'attr', None)) not in names:
                continue
            if any(isinstance(arg, ast.Starred) for arg in call.args) or any(kw.arg is None for kw in call.keywords):
                raise ValueError('unpacked cap forwarding arguments require an explicit audit')
            yield {**defaults, **dict(zip([a.arg for a in positional], call.args)),
                   **{kw.arg: kw.value for kw in call.keywords}}

    def assignment(self, node):
        scope = self.owner(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if isinstance(node, ast.Attribute):
            scope = self.owner(node, ast.ClassDef)
        if scope is not None:
            assigned = [stmt.value for stmt in ast.walk(scope) if isinstance(stmt, ast.Assign)
                        and any(ast.unparse(t) == ast.unparse(node) for t in stmt.targets)]
            if len(assigned) == 1:
                return assigned[0]
        raise ValueError(ast.unparse(node))

    def values(self, node, bound=None, depth=0):
        if depth > 8:
            raise ValueError('forwarding exceeds the audit scope')
        if isinstance(node, ast.Constant):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return self.values(node.body, bound, depth+1) | self.values(node.orelse, bound, depth+1)
        if isinstance(node, ast.Name):
            if bound is not None and node.id in bound:
                return self.values(bound[node.id], depth=depth+1)
            func = self.owner(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if func is not None and node.id in {a.arg for a in func.args.args + func.args.kwonlyargs}:
                calls = list(self.bindings(func))
                if calls and all(node.id in b for b in calls):
                    return set().union(*(self.values(b[node.id], depth=depth+1) for b in calls))
            return self.values(self.assignment(node), bound, depth+1)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'self':
            return self.values(self.assignment(node), bound, depth+1)
        raise ValueError(ast.unparse(node))

    def pairs(self, cap, protocol):
        attributes = [n for expr in (cap, protocol) for n in ast.walk(expr)
                      if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                      and n.value.id == 'self']
        contexts = [None]
        if attributes:
            setters = {self.owner(self.assignment(n), (ast.FunctionDef, ast.AsyncFunctionDef))
                       for n in attributes}
            if len(setters) != 1:
                raise ValueError('unrelated attribute forwarding')
            contexts = list(self.bindings(setters.pop()))
            if not contexts:
                raise ValueError('no callers of cap attribute setter')
        else:
            func = self.owner(cap, (ast.FunctionDef, ast.AsyncFunctionDef))
            if func is not None:
                params = {a.arg for a in func.args.args + func.args.kwonlyargs}
                if any(isinstance(n, ast.Name) and n.id in params
                       for expr in (cap, protocol) for n in ast.walk(expr)):
                    contexts = list(self.bindings(func))
                    if not contexts:
                        raise ValueError('no callers of cap forwarding helper')
        return {(c, p) for bound in contexts for c in self.values(cap, bound)
                for p in self.values(protocol, bound)}


def _recorded_pairs(sources=None, exemptions=None):
    full_tree = sources is None
    if exemptions is None:
        exemptions = EXEMPT if full_tree else {}
    if full_tree:
        sources = {str(p.relative_to(PACKAGE)): p.read_text(encoding='utf-8')
                   for p in PACKAGE.rglob('*.py')}
    audit = _Audit(sources)
    pairs, exempt = set(), []
    for path, tree in audit.trees.items():
        aliases = {'log_cap_hit'} | {alias.asname or alias.name for n in ast.walk(tree)
                                    if isinstance(n, ast.ImportFrom) for alias in n.names
                                    if alias.name == 'log_cap_hit'}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Name) and node.id in aliases
                    or isinstance(node, ast.Attribute) and node.attr == 'log_cap_hit'):
                parent = audit.parents.get(node)
                assert isinstance(parent, ast.Call) and parent.func is node, (path, 'cap logger escaped the audit')
            if not isinstance(node, ast.Call) or not (
                    isinstance(node.func, ast.Name) and node.func.id in aliases
                    or isinstance(node.func, ast.Attribute) and node.func.attr == 'log_cap_hit'):
                continue
            func = audit.owner(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            cls = audit.owner(node, ast.ClassDef)
            key = path, (cls.name + '.' if cls else '') + (func.name if func else '<module>')
            cap = node.args[0] if node.args else next(kw.value for kw in node.keywords if kw.arg == 'cap')
            protocol = next((kw.value for kw in node.keywords if kw.arg == 'protocol'), ast.Constant(None))
            try:
                found = audit.pairs(cap, protocol)
            except ValueError as exc:
                assert key in exemptions and not isinstance(protocol, ast.Constant), (key, str(exc))
                expected_cap, variable, expression, _reason = exemptions[key]
                assert audit.values(cap) == {expected_cap} and isinstance(protocol, ast.Name) and protocol.id == variable
                assert ast.dump(audit.assignment(protocol)) == ast.dump(ast.parse(expression, mode='eval').body)
                exempt.append(key)
                continue
            assert found and all(p in SUBSYSTEMS for _, p in found), (key, found)
            pairs |= found
    assert sorted(exempt) == sorted(exemptions)
    return pairs


def test_the_code_records_no_cap_outside_the_inventory():
    assert _recorded_pairs() - {(c, p) for c, ps in ENFORCED.items() for p in ps} == set()


def test_every_inventoried_cap_is_recorded_somewhere():
    assert {(c, p) for c, ps in ENFORCED.items() for p in ps} - _recorded_pairs() == set()


@pytest.mark.parametrize('argument', ['', ', protocol=None', ', protocol="http/2"', ', protocol=dynamic'])
def test_audit_refuses_missing_unknown_or_unresolved_subsystems(argument):
    with pytest.raises(AssertionError):
        _recorded_pairs({'example.py': f'log_cap_hit("cap", 2, 1{argument})'})


_WRITER_SOURCE = '''
class Writer:
    def __init__(self, cap="write_timeout", protocol="transport"):
        self._cap = cap
        self._protocol = protocol
    def refuse(self):
        log_cap_hit(self._cap, 2, 1, protocol=self._protocol)
'''


def test_audit_preserves_constructor_pairs_and_defaults():
    source = _WRITER_SOURCE + '''
Writer()
Writer(cap="client_write_timeout", protocol="http1")
Writer(cap="client_write_timeout", protocol="http2")
class ClientWriter(Writer):
    pass
ClientWriter(cap="client_write_timeout", protocol="ws")
'''
    assert _recorded_pairs({'example.py': source}) == {
        ('write_timeout', 'transport'), ('client_write_timeout', 'http1'),
        ('client_write_timeout', 'http2'), ('client_write_timeout', 'ws')}


def test_audit_preserves_helper_pairs():
    source = '''
def refuse(cap, protocol="transport"):
    caps.log_cap_hit(cap, 2, 1, protocol=protocol)
refuse("write_timeout")
refuse("client_write_timeout", protocol="http1")
'''
    assert _recorded_pairs({'example.py': source}) == {
        ('write_timeout', 'transport'), ('client_write_timeout', 'http1')}


def test_audit_tracks_import_aliases_and_refuses_escaped_loggers():
    source = 'from blackbull.server.cap_log import log_cap_hit as hit\n'
    assert _recorded_pairs({'example.py': source + 'hit("write_timeout", 2, 1, protocol="transport")'}) == {
        ('write_timeout', 'transport')}
    with pytest.raises(AssertionError):
        _recorded_pairs({'example.py': source + 'callback = hit'})
    with pytest.raises(AssertionError):
        _recorded_pairs({'example.py': 'import blackbull.server.cap_log as caps\ncallback = caps.log_cap_hit'})


@pytest.mark.parametrize('arguments', ['**{"protocol": None}', '*("cap", "http2")'])
def test_audit_refuses_unpacked_forwarding(arguments):
    with pytest.raises(AssertionError):
        _recorded_pairs({'example.py': _WRITER_SOURCE + f'Writer({arguments})'})


def test_audit_tracks_constructor_import_alias():
    assert _recorded_pairs({'writer.py': _WRITER_SOURCE, 'caller.py': '''
from writer import Writer as Alias
Alias(cap="client_write_timeout", protocol="http1")
'''}) == {('client_write_timeout', 'http1')}


@pytest.mark.parametrize('expression', [
    "binding.name if binding is not None else 'unknown'",
    'binding.name if binding is not None else None',
    "request.path if binding is not None else 'unknown'",
])
def test_dynamic_exemption_pins_the_registered_name_and_fallback(expression):
    source = f'''
class ConnectionActor:
    def _on_detect_timeout(self, binding):
        proto = {expression}
        log_cap_hit('header_timeout', 2, 1, protocol=proto)
'''
    sources = {'server/connection_actor.py': source}
    if expression == EXEMPT[('server/connection_actor.py', 'ConnectionActor._on_detect_timeout')][2]:
        assert _recorded_pairs(sources, EXEMPT) == set()
    else:
        with pytest.raises(AssertionError):
            _recorded_pairs(sources, EXEMPT)


def test_every_cap_has_a_row_on_every_protocol_that_enforces_it():
    covered = {(r.cap, r.protocol) for r in rows_of(
        test_size_caps, test_count_caps, test_time_caps, test_rate_caps)}
    declared = {(cap, protocol) for cap, protocols in ENFORCED.items() for protocol in protocols}
    assert covered == declared, (
        f'without a row: {sorted(declared - covered, key=str)}; '
        f'not inventoried: {sorted(covered - declared, key=str)}')
