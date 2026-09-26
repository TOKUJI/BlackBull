"""One spelling per rule (BLA-461).

The informational test, the bodyless test and the method comparison live in
`protocol.framing` and every transport reads them from there. A copy written
at a call site drifts: the HTTP/2 one already had, comparing a method folded
to upper case where RFC 9110 §9.1 makes it case-sensitive, and the sender's
content rule had already grown a status the shared one did not have.

The behaviour each rule decides is pinned in
`tests/unit/client/test_http2_client_response_completion.py`; this pins that
there is only one of them — including inline, which is how the last two
copies hid.
"""
from pathlib import Path

from blackbull.protocol import framing

ROOT = Path(__file__).resolve().parents[2] / 'blackbull'
FRAMING = ROOT / 'protocol' / 'framing.py'

#: A rule written by hand at a call site. Each is what the shared function
#: spells once; any of them outside ``framing.py`` is a copy that can drift.
INLINE = (
    'status in (204',          # response_has_content
    'status not in (204',      # response_has_content
    '100 <= status',           # is_informational
    'status < 200',            # is_informational
    "== 'HEAD'",               # method_is
    '== "HEAD"',
    "!= 'HEAD'",
    "== 'CONNECT'",            # method_is
    '== "CONNECT"',
    "!= 'CONNECT'",
    "== b'HEAD'",              # method_is — a b' spelling escapes the list
    "== b'CONNECT'",
    "!= b'HEAD'",
    "!= b'CONNECT'",
)


def test_framing_defines_each_rule_once():
    text = FRAMING.read_text()
    for rule in ('is_informational', 'response_has_content', 'method_is'):
        assert text.count(f'def {rule}(') == 1, rule


def test_no_transport_redefines_a_rule():
    for path in sorted(ROOT.rglob('*.py')):
        if path == FRAMING:
            continue
        text = path.read_text()
        for rule in ('is_informational', 'response_has_content', 'method_is'):
            assert f'def {rule}(' not in text, f'{path.name} redefines {rule}'


def test_no_transport_writes_a_rule_by_hand():
    for path in sorted(ROOT.rglob('*.py')):
        if path == FRAMING:
            continue
        text = path.read_text()
        for spelling in INLINE:
            assert spelling not in text, (
                f'{path.name} writes {spelling!r} instead of the shared rule')


def test_the_contentless_sets_are_pinned():
    """The frozensets are a lookup table for the rule, not a second rule to
    reason about: these two lines are what keeps them equal to it."""
    assert framing.NO_CONTENT_STATUSES == frozenset(range(100, 200)) | {204, 304}
    assert framing.NO_CONTENT_GENERATED_STATUSES - framing.NO_CONTENT_STATUSES == {
        205}
