"""One spelling per rule (BLA-461).

The informational test, the bodyless test and the method comparison live in
`protocol.framing` and every transport reads them from there. A copy written
at a call site drifts: the HTTP/2 one already had, comparing a method folded
to upper case where RFC 9110 §9.1 makes it case-sensitive.

The behaviour these rules decide is pinned in
`tests/unit/client/test_http2_client_response_completion.py`; this pins that
there is only one of them.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / 'blackbull'
RULES = ('is_informational', 'response_has_content', 'method_is')


def test_framing_defines_each_rule_once():
    text = (ROOT / 'protocol' / 'framing.py').read_text()
    for rule in RULES:
        assert text.count(f'def {rule}(') == 1, rule


def test_no_transport_redefines_a_rule():
    for path in sorted(ROOT.rglob('*.py')):
        if path.name == 'framing.py':
            continue
        text = path.read_text()
        for rule in RULES:
            assert f'def {rule}(' not in text, f'{path.name} redefines {rule}'
