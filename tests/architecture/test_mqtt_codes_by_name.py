"""MQTT の reason code・packet type・property id は名前で書く(BLA-416)。"""
from __future__ import annotations

import ast
import re
import tokenize
from pathlib import Path

from blackbull.mqtt.messages import ReasonCode

_ROOT = Path(__file__).resolve().parents[2]
HEX = re.compile(r'0x[0-9A-Fa-f]{2,8}')
CODE_KEYWORDS = {'reason', 'reason_code', 'reason_codes',
                 'packet_type', 'property_id'}

# 例外は理由つきでここに置く: (ファイル, 親関数名) → 理由
ALLOWED = {
    ('tests/unit/test_mqtt_messages.py', 'test_unknown_reason_code_handled'):
        '未定義コードのプローブは名前が存在しない',
    ('tests/conformance/mqtt/test_mqtt_edge_cases.py',
     'test_packet_type_0_is_forbidden'):
        'Table 2-1 に無い予約値のプローブは名前が存在しない',
}


def _scoped() -> list[Path]:
    return sorted(
        [*_ROOT.glob('blackbull/mqtt/*.py'), _ROOT / 'blackbull/testing/mqtt.py',
         *_ROOT.glob('tests/conformance/mqtt/*.py'),
         *_ROOT.glob('tests/unit/test_mqtt_*.py'),
         *_ROOT.glob('tests/architecture/test_mqtt_*.py'),
         *_ROOT.glob('tests/properties/test_mqtt_*.py')])


def _parents(tree):
    out = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            out[id(c)] = n
    return out


def _function_of(parents, n):
    cur = n
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parents.get(id(cur))
    return ''


def _anchor(parents, n):
    """リテラルを包む List/Tuple を外して位置を決める式へ遡る。"""
    cur = parents.get(id(n))
    while isinstance(cur, (ast.List, ast.Tuple)):
        cur = parents.get(id(cur))
    return cur


def _definition_ids(tree):
    """enum 本体とモジュール定数の定義文脈を node id 集合で返す。"""
    out = set()

    def visit(n, in_enum, top):
        if isinstance(n, ast.ClassDef):
            bases = {getattr(b, 'id', getattr(b, 'attr', '')) for b in n.bases}
            enum = in_enum or bool(bases & {'IntEnum', 'IntFlag'})
            for c in ast.iter_child_nodes(n):
                visit(c, enum, False)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)) and top:
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                out.add(id(t))
            if isinstance(n.value, ast.Constant):
                out.add(id(n.value))
        elif isinstance(n, ast.Constant) and in_enum:
            out.add(id(n))
        else:
            for c in ast.iter_child_nodes(n):
                visit(c, in_enum, top)

    for n in ast.iter_child_nodes(tree):
        visit(n, False, True)
    return out


def _bitop_ids(tree):
    ops = (ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift)
    return {id(c) for n in ast.walk(tree)
            if isinstance(n, ast.BinOp) or
            (isinstance(n, ast.AugAssign) and isinstance(n.op, ops))
            for c in ast.walk(n)}


def _in_wire_bytes(parents, n):
    cur = parents.get(id(n))
    while cur is not None:
        if isinstance(cur, ast.Call) and isinstance(cur.func, ast.Name) \
                and cur.func.id in ('bytes', 'bytearray'):
            return True
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef, ast.Module)):
            return False
        cur = parents.get(id(cur))
    return False


def test_a_reason_code_position_names_its_value():
    """reason code の位置に生の数値を置かない — 値とラベルの取り違え防止。"""
    bad = []
    for path in _scoped():
        tree = ast.parse(path.read_text())
        parents = _parents(tree)
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Constant)
                    and isinstance(n.value, int)
                    and not isinstance(n.value, bool)):
                continue
            anchor = _anchor(parents, n)
            where = None
            if isinstance(anchor, ast.keyword) and anchor.arg in CODE_KEYWORDS:
                where = f'{anchor.arg} の位置'
            elif isinstance(anchor, ast.Compare) and any(
                    isinstance(x, ast.Attribute) and x.attr in CODE_KEYWORDS
                    for x in ast.walk(anchor)):
                where = 'コード属性との比較'
            elif (isinstance(anchor, ast.Call)
                  and isinstance(anchor.func, ast.Name)
                  and anchor.func.id == 'MQTTReasonCode'
                  and n.value in {int(rc) for rc in ReasonCode}):
                where = 'MQTTReasonCode(...) の位置'
            if where is None:
                continue
            if (str(path.relative_to(_ROOT)),
                    _function_of(parents, n)) in ALLOWED:
                continue
            bad.append(f'{path.relative_to(_ROOT)}:{n.lineno}: {where} に生の数値')
    assert not bad, '名前(ReasonCode.X 等)で書く:\n' + '\n'.join(bad)


def test_raw_hex_stays_in_definitions_and_wire_bytes():
    """生の 0x は定義・ビット演算・wire バイト列の文脈に限る。"""
    bad = []
    for path in _scoped():
        src = path.read_text()
        tree = ast.parse(src)
        parents = _parents(tree)
        defs, bits = _definition_ids(tree), _bitop_ids(tree)
        literals = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, int):
                seg = (ast.get_source_segment(src, n) or '').strip()
                if HEX.fullmatch(seg):
                    literals.setdefault(n.lineno, []).append(n)
        import io as _io
        for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            if tok.type == tokenize.NUMBER and HEX.fullmatch(tok.string):
                hits = literals.get(tok.start[0])
                if not hits:
                    continue
                n = hits.pop(0)
                ok = (id(n) in defs or id(n) in bits
                      or _in_wire_bytes(parents, n))
            elif tok.type == tokenize.NAME and HEX.search(tok.string):
                n = None
                ok = False
            else:
                continue
            fn = _function_of(parents, n) if n is not None else ''
            if ok or (str(path.relative_to(_ROOT)), fn) in ALLOWED:
                continue
            bad.append(f'{path.relative_to(_ROOT)}:{tok.start[0]}: {tok.string!r}')
    assert not bad, '名前か定数で書く:\n' + '\n'.join(bad)
