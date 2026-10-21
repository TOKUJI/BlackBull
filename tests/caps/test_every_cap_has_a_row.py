"""Every cap the code can record has a row, on every protocol that enforces it.

``ENFORCED`` is the inventory: each cap and the protocols whose refusal it
names.  The scan below proves the code records no name the inventory lacks;
the tables prove each listed (cap, protocol) refuses as documented.
"""
import ast
import pathlib

from . import test_count_caps, test_rate_caps, test_size_caps, test_time_caps
from ._table import rows_of

ENFORCED: dict[str, set[str | None]] = {
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
    'header_max_total': {'http1'},
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
    'socket_backlog': {None},
    'stream_queue_depth': {'http2'},
    'write_timeout': {None, 'http2'},
    'ws_max_frame_payload': {'ws'},
    'ws_max_message_size': {'ws'},
}

PACKAGE = pathlib.Path(__file__).resolve().parents[2] / 'blackbull'


def _named_by(func, param: str, tree) -> list[str]:
    """Literal *param* values that callers of *func* pass: one level of
    forwarding, which is how a shared refusal helper receives the name."""
    params = [a.arg for a in func.args.posonlyargs + func.args.args]
    index = params.index(param) - (params[0] in ('self', 'cls')) if param in params else None
    names = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        callee = getattr(call.func, 'attr', getattr(call.func, 'id', None))
        if callee != func.name:
            continue
        values = [kw.value for kw in call.keywords if kw.arg == param]
        if index is not None and index < len(call.args):
            values.append(call.args[index])
        names += [v.value for v in values if isinstance(v, ast.Constant)]
    return names


def _recorded_names() -> set[str]:
    names = set()
    for path in PACKAGE.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                args = a.posonlyargs + a.args + a.kwonlyargs
                defaults = list(zip(args[len(args) - len(a.defaults) - len(a.kwonlyargs):],
                                    a.defaults)) + list(zip(a.kwonlyargs, a.kw_defaults))
                names |= {d.value for arg, d in defaults
                          if isinstance(d, ast.Constant) and arg.arg.endswith('cap')
                          or isinstance(d, ast.Constant) and arg.arg.endswith('cap_name')}
            if not isinstance(node, ast.Call):
                continue
            names |= {kw.value.value for kw in node.keywords
                      if (kw.arg or '').endswith(('cap', 'cap_name'))
                      and isinstance(kw.value, ast.Constant)}
            if getattr(node.func, 'id', None) != 'log_cap_hit' or not node.args:
                continue
            first = node.args[0]
            # Every literal the expression can evaluate to, conditionals included.
            names |= {c.value for c in ast.walk(first)
                      if isinstance(c, ast.Constant) and isinstance(c.value, str)}
            if isinstance(first, ast.Name):
                func = parents.get(node)
                while not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func = parents[func]
                names |= set(_named_by(func, first.id, tree))
    return names - {None}


def test_the_code_records_no_cap_outside_the_inventory():
    assert _recorded_names() - set(ENFORCED) == set()


def test_every_inventoried_cap_is_recorded_somewhere():
    assert set(ENFORCED) - _recorded_names() == set()


def test_every_cap_has_a_row_on_every_protocol_that_enforces_it():
    covered = {(r.cap, r.protocol) for r in rows_of(
        test_size_caps, test_count_caps, test_time_caps, test_rate_caps)}
    declared = {(cap, protocol) for cap, protocols in ENFORCED.items() for protocol in protocols}
    assert covered == declared, (
        f'without a row: {sorted(declared - covered, key=str)}; '
        f'not inventoried: {sorted(covered - declared, key=str)}')
