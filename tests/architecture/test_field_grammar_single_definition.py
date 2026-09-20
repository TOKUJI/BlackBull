"""One field grammar, two transports — RFC 9110 §5.5 and §5.6.2.

Name octets and prohibited value octets are not a per-transport decision.  The
policy layered on them differs (HTTP/1.1 strips the OWS at a value's edges and
accepts uppercase names, HTTP/2 refuses both), but the alphabet itself is one
thing, and seven spellings of it had accumulated: the H/1 actor, the H/2 frame
layer, the outbound response validator, the cache middleware, the H/1 client,
the chunked-body recipient, and the router's method check.  They drifted — by
the time this was written the H/2 accept set was wider than the H/1 one by 16
name octets and 29 value octets — which is a request-smuggling surface §8.2.1
names explicitly.

The identity checks prove every reader reads one definition, and the RFC
literals below spell the two sets a second time — independently of the module
— so a grammar that is internally consistent but wrong (a separator added to
the alphabet) still fails here.  The walker is a
tripwire for the drift returning: a reader may import the grammar, never
assign it.
"""
import ast
import pathlib

import blackbull.client.http1 as client_http1
import blackbull.headers as headers_module
import blackbull.middleware.cache as cache_module
import blackbull.protocol.frame_types as frame_types
import blackbull.router as router
import blackbull.server.http1_actor as http1_actor
import blackbull.server.recipient as recipient
from blackbull.protocol import field_grammar
from blackbull.protocol.frame_types import (
    field_name_is_valid, field_value_has_boundary_whitespace,
    field_value_is_valid)

#: Each reader and the grammar names it imports.
_READERS = {
    http1_actor: ('TCHAR_OCTETS', 'FIELD_VALUE_ALLOWED_OCTETS'),
    frame_types: ('TCHAR_OCTETS', 'FIELD_VALUE_ALLOWED_OCTETS'),
    headers_module: ('TCHAR_OCTETS', 'FIELD_VALUE_ALLOWED_OCTETS'),
    recipient: ('TCHAR_OCTETS', 'FIELD_VALUE_ALLOWED_OCTETS'),
    client_http1: ('TCHAR_OCTETS', 'TCHAR_SET', 'FIELD_VALUE_ALLOWED_OCTETS',
                   'FIELD_VALUE_ALLOWED_SET'),
    cache_module: ('TCHAR_SET', 'FIELD_VALUE_ALLOWED_SET'),
    router: ('TCHAR_OCTETS',),
}

#: The names the grammar owns.  A reader that assigns one of these has
#: respelled the grammar, which is how the two transports drifted apart.
_OWNED_BY_THE_GRAMMAR = frozenset({
    'TCHAR_OCTETS', '_TCHAR_OCTETS', 'TCHAR_SET', '_TCHAR_SET',
    'FIELD_NAME_INVALID_RE', '_FIELD_NAME_INVALID_RE', '_FIELD_NAME_OCTETS',
    'FIELD_VALUE_INVALID_RE', '_FIELD_VALUE_INVALID_RE',
    'FIELD_VALUE_ALLOWED_OCTETS', '_FIELD_VCHAR', '_BLOCK_ALLOWED_OCTETS',
})

#: RFC 9110 §5.6.2 — names are lowercase in HTTP/2 (RFC 9113 §8.2).
_UPPERCASE = frozenset(range(0x41, 0x5B))
_MARKER_COLON = 0x3A

#: RFC 9110 §5.6.2 tchar: ALPHA / DIGIT / "!" / "#" / "$" / "%" / "&" / "'" /
#: "*" / "+" / "-" / "." / "^" / "_" / "`" / "|" / "~".
_RFC_TCHAR = frozenset(
    b"!#$%&'*+-.^_`|~"
    b'0123456789'
    b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    b'abcdefghijklmnopqrstuvwxyz')

#: RFC 9110 §5.5 field-content: HTAB, SP, VCHAR and obs-text.  Every other C0
#: control, and DEL, is forbidden.
_RFC_FORBIDDEN_VALUE = frozenset(
    c for c in range(256) if (c < 0x20 and c != 0x09) or c == 0x7F)


def _assigned_names(module) -> set:
    tree = ast.parse(pathlib.Path(module.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def test_the_alphabet_is_the_rfc_one():
    assert field_grammar.TCHAR_SET == _RFC_TCHAR
    assert frozenset(field_grammar.TCHAR_OCTETS) == _RFC_TCHAR


def test_the_allowed_value_octets_are_the_rfc_ones():
    allowed = frozenset(field_grammar.FIELD_VALUE_ALLOWED_OCTETS)
    assert allowed == frozenset(range(256)) - _RFC_FORBIDDEN_VALUE
    assert field_grammar.FIELD_VALUE_ALLOWED_SET == allowed


def test_no_reader_respells_the_grammar():
    offenders = {
        module.__name__: sorted(_assigned_names(module) & _OWNED_BY_THE_GRAMMAR)
        for module in _READERS
    }
    assert {name: names for name, names in offenders.items() if names} == {}


def test_every_reader_reads_the_one_definition():
    for module, names in _READERS.items():
        for name in names:
            assert getattr(module, name) is getattr(field_grammar, name), (
                module.__name__, name)


def test_the_router_method_check_is_the_same_alphabet():
    """The router's input is a ``str``, so it cannot use the bytes table; its
    delete table is built from the same alphabet, and this pins the two."""
    wrong = [c for c in range(256)
             if (chr(c).translate(router._TCHAR_DELETE) == '')
             is not (c in field_grammar.TCHAR_SET)]
    assert wrong == []


def test_h2_name_rule_is_the_shared_alphabet_lowercased():
    wrong = {}
    for octet in range(256):
        in_alphabet = octet in field_grammar.TCHAR_SET
        expected = in_alphabet and octet not in _UPPERCASE
        marker = octet == _MARKER_COLON
        # An octet anywhere but the leading position: no marker exception.
        if field_name_is_valid(b'y' + bytes([octet])) is not expected:
            wrong[octet] = 'in the tail'
        # As the leading octet, where the pseudo-header marker is allowed.
        if field_name_is_valid(bytes([octet])) is not (expected or marker):
            wrong[octet] = 'leading'
    assert wrong == {}


def test_the_empty_name_is_not_a_name():
    """``1*tchar``, so no octets is not a name — the alphabet checks have to
    keep that, not just the octet membership."""
    assert not field_name_is_valid(b'')


def test_both_transports_refuse_the_same_value_octets():
    wrong = {}
    for octet in range(256):
        value = bytes([octet])
        h1_refuses = bool(value.translate(
            None, field_grammar.FIELD_VALUE_ALLOWED_OCTETS))
        h2_refuses = not field_value_is_valid(value)
        if h2_refuses is not h1_refuses:
            wrong[octet] = (h1_refuses, h2_refuses)
    assert wrong == {}


def test_the_one_intended_value_difference_is_position_not_octet():
    """SP and HTAB are inside RFC 9110's field-content, so the octet rule
    admits them; HTTP/2 refuses one at an edge (§8.2.1) where HTTP/1.1 strips
    it (RFC 9112 §5)."""
    assert field_value_is_valid(b'a b')
    assert field_value_is_valid(b'a\tb')
    assert field_value_has_boundary_whitespace(b' a')
    assert field_value_has_boundary_whitespace(b'a\t')
