"""MQTT の reason code・packet type・property id は名前で書く(BLA-416)。"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

from blackbull.mqtt.messages import ReasonCode

_ROOT = Path(__file__).resolve().parents[2]
HEX = re.compile(r'0x[0-9A-Fa-f]{2,8}')
CODE_KEYWORDS = {'reason', 'reason_code', 'reason_codes',
                 'packet_type', 'property_id'}
BIT_OPS = (ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift)

# 例外は理由つきでここに置く: (ファイル, 親の関数・クラス名) → 理由
ALLOWED = {
    ('tests/unit/test_mqtt_messages.py', 'test_unknown_reason_code_handled'):
        '未定義コードのプローブは名前が存在しない',
    ('tests/conformance/mqtt/test_mqtt_edge_cases.py',
     'test_packet_type_0_is_forbidden'):
        'Table 2-1 に無い予約値のプローブは名前が存在しない',
    ('tests/conformance/mqtt/test_mqtt_packet_structure.py',
     'TestReasonCodes'):
        '§2.4 の独立転記 — 実装の値から導かない仕様検証',
    ('tests/conformance/mqtt/test_mqtt_properties.py',
     'TestPropertyIdentifiers'):
        'Table 2-3 の独立転記 — 実装の値から導かない仕様検証',
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


def _scope_of(parents, n):
    """リテラルを囲む最寄りの関数名・クラス名(モジュール直下は空)。"""
    cur = n
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef)):
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
    """enum 本体とモジュール直下のスカラー定数の定義文脈を node id 集合で返す。"""
    out = set()

    def visit(n, in_enum, top):
        if isinstance(n, ast.ClassDef):
            bases = {getattr(b, 'id', getattr(b, 'attr', '')) for b in n.bases}
            enum = in_enum or bool(bases & {'IntEnum', 'IntFlag'})
            for c in ast.iter_child_nodes(n):
                visit(c, enum, False)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for c in ast.iter_child_nodes(n):
                visit(c, in_enum, False)
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
    out = set()
    for n in ast.walk(tree):
        if ((isinstance(n, ast.BinOp) or isinstance(n, ast.AugAssign))
                and isinstance(n.op, BIT_OPS)):
            out |= {id(c) for c in ast.walk(n)}
    return out


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


def _reason_code_violations(src: str, rel: str) -> list[str]:
    """reason code の位置に生の数値があればその箇所を返す。"""
    bad = []
    tree = ast.parse(src)
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
        elif isinstance(anchor, (ast.Assign, ast.AnnAssign)):
            targets = (anchor.targets if isinstance(anchor, ast.Assign)
                       else [anchor.target])
            names = {getattr(t, 'id', getattr(t, 'attr', ''))
                     for t in targets}
            if names & CODE_KEYWORDS:
                where = 'コード名の変数への代入'
        elif (isinstance(anchor, ast.Call)
              and isinstance(anchor.func, ast.Name)
              and anchor.func.id == 'MQTTReasonCode'
              and n.value in {int(rc) for rc in ReasonCode}):
            where = 'MQTTReasonCode(...) の位置'
        if where is None or (rel, _scope_of(parents, n)) in ALLOWED:
            continue
        bad.append(f'{rel}:{n.lineno}: {where} に生の数値')
    return bad


def _hex_violations(src: str, rel: str) -> list[str]:
    """定義・ビット演算・wire バイト列の外にある生の 0x を返す。"""
    bad = []
    tree = ast.parse(src)
    parents = _parents(tree)
    defs, bits = _definition_ids(tree), _bitop_ids(tree)
    literals = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            seg = (ast.get_source_segment(src, n) or '').strip()
            if HEX.fullmatch(seg):
                literals.setdefault(n.lineno, []).append(n)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
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
        if ok or (rel, _scope_of(parents, n) if n is not None else '') in ALLOWED:
            continue
        bad.append(f'{rel}:{tok.start[0]}: {tok.string!r}')
    return bad


def test_a_reason_code_position_names_its_value():
    """reason code の位置に生の数値を置かない — 値とラベルの取り違え防止。"""
    bad = []
    for path in _scoped():
        bad += _reason_code_violations(path.read_text(),
                                       str(path.relative_to(_ROOT)))
    assert not bad, '名前(ReasonCode.X 等)で書く:\n' + '\n'.join(bad)


def test_raw_hex_stays_in_definitions_and_wire_bytes():
    """生の 0x は定義・ビット演算・wire バイト列の文脈に限る。"""
    bad = []
    for path in _scoped():
        bad += _hex_violations(path.read_text(),
                               str(path.relative_to(_ROOT)))
    assert not bad, '名前か定数で書く:\n' + '\n'.join(bad)


def test_the_detector_allows_definitions_masks_and_wire_bytes():
    ok = '''MASK = 0x0F
VALUE = 0x80 | MASK


class Codes(IntEnum):
    CODE = 0x87


def f(first):
    data = bytes([0x00, 0xFF])
    return (first & MASK), data
'''
    assert not _hex_violations(ok, 'x.py')
    assert not _reason_code_violations(ok, 'x.py')


def test_the_detector_catches_local_narrowing_and_arithmetic():
    local = 'def f():\n    reason_code = 0x87\n    return reason_code\n'
    assert _hex_violations(local, 'x.py')
    assert _reason_code_violations(local, 'x.py')
    assert _hex_violations('def f():\n    return 0x87 + 0\n', 'x.py')
    assert _reason_code_violations(
        'def f():\n    return x.reason_code == 3\n', 'x.py')
    assert _reason_code_violations(
        'def f():\n    return MQTTReasonCode(0x87)\n', 'x.py')
