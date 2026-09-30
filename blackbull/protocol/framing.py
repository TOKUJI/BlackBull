"""RFC 9112 §6 — how a message declares its body length.

Whatever side reads or writes it answers ``Content-Length`` identically, so
the answer is here once: one value, written once, in ``1*DIGIT``, agreed on by
every occurrence.  A message that carries two boundaries does not have one.

``Transfer-Encoding`` has one reading here too — what its list names.  What
each side does about the names stays with each side: a server's refusal
grade, a sender's rewriting contract and a reader's framing are three
policies over one list.
"""
from collections.abc import Iterable
from http import HTTPMethod

from .field_grammar import FIELD_VALUE_ALLOWED_SET, TCHAR_SET

__all__ = ('NO_CONTENT_GENERATED_STATUSES', 'NO_CONTENT_STATUSES',
           'TransferCoding', 'is_informational', 'method_is',
           'parse_content_length', 'parse_status', 'response_has_content',
           'split_transfer_codings')


def parse_content_length(fields: Iterable[tuple[bytes, bytes]]
                         ) -> int | None:
    """The length every ``Content-Length`` occurrence agrees on, or ``None``.

    Occurrences are ``#field-value`` lists too, so each member counts.  Raises
    ``ValueError`` when a member is not ``1*DIGIT`` or the members disagree;
    the message names the values it could not reconcile.
    """
    values: list[int] = []
    for _name, raw in fields:
        for member in raw.split(b','):
            value = member.strip(b' \t')
            if not value or not value.isdigit():
                raise ValueError(f'invalid Content-Length value: {value!r}')
            values.append(int(value))
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise ValueError(
            f'conflicting Content-Length values: {sorted(set(values))!r}')
    return values[0]


#: One member of a ``Transfer-Encoding`` list: the lowered coding token and
#: the parameters written on it, each ``(name, value)`` pair as written —
#: the value being a token or a quoted string, quotes included, nothing
#: decoded.
TransferCoding = tuple[bytes, tuple[tuple[bytes, bytes], ...]]

#: RFC 9110 §5.6.1 lets a recipient ignore "a reasonable number" of empty
#: list members.  The number is here, because the list is read here once: a
#: field of commas is bounded work whatever side reads it.
_MAX_EMPTY_TRANSFER_MEMBERS = 16


def split_transfer_codings(
        fields: Iterable[tuple[bytes, bytes]]) -> list[TransferCoding]:
    """Every ``Transfer-Encoding`` member as RFC 9112 §5.5 writes one, or
    ``ValueError``.

    What the list *is* is answered once: the occurrences split on their
    commas, OWS off, each coding token lowered, and the parameters kept
    rather than dropped — so a policy that refuses ``chunked`` carrying a
    parameter sees one, and a policy that reads only the names drops them
    as a decision instead of never being shown them.  An empty member stays
    in the list as ``(b'', ())``: a sender that would rewrite the field must
    see every member it would be rewriting away (``chunked, `` is not
    ``chunked``), while a reader that ignores empties per RFC 9110 §5.6.1
    leaves them out at its own policy step.

    A member that is not
    ``token *( OWS ";" OWS token OWS "=" OWS ( token / quoted-string ) )``
    raises: a parameter this reading cannot see through must not be able to
    hide a different final coding from it.  So does a list holding more
    empty members than the bound above.
    """
    members: list[TransferCoding] = []
    empty_members = 0
    for _name, value in fields:
        pos = 0
        while True:
            pos = _skip_ows(value, pos)
            if pos == len(value):
                # An empty field and the member after a trailing comma are
                # both empty members, but not an unbounded amount of work.
                empty_members += 1
                if empty_members > _MAX_EMPTY_TRANSFER_MEMBERS:
                    raise ValueError(
                        'too many empty Transfer-Encoding list members')
                members.append((b'', ()))
                break
            if value[pos] == 0x2c:  # comma: empty member
                empty_members += 1
                if empty_members > _MAX_EMPTY_TRANSFER_MEMBERS:
                    raise ValueError(
                        'too many empty Transfer-Encoding list members')
                members.append((b'', ()))
                pos += 1
                continue

            name, pos = _transfer_token(value, pos)
            params: list[tuple[bytes, bytes]] = []
            pos = _skip_ows(value, pos)
            while pos < len(value) and value[pos] == 0x3b:  # ';'
                pos = _skip_ows(value, pos + 1)
                param, pos = _transfer_token(value, pos)
                pos = _skip_ows(value, pos)
                if pos >= len(value) or value[pos] != 0x3d:  # '='
                    raise ValueError(
                        'Transfer-Encoding parameter requires "="')
                pos = _skip_ows(value, pos + 1)
                start = pos
                if pos < len(value) and value[pos] == 0x22:
                    pos = _transfer_quoted_string(value, pos)
                else:
                    _, pos = _transfer_token(value, pos)
                params.append((param, value[start:pos]))
                pos = _skip_ows(value, pos)
            members.append((name.lower(), tuple(params)))
            if pos == len(value):
                break
            if value[pos] != 0x2c:
                raise ValueError(
                    'invalid Transfer-Encoding list separator')
            pos += 1
    return members


def _skip_ows(value: bytes, pos: int) -> int:
    while pos < len(value) and value[pos] in (0x20, 0x09):
        pos += 1
    return pos


def _transfer_token(value: bytes, pos: int) -> tuple[bytes, int]:
    start = pos
    while pos < len(value) and value[pos] in TCHAR_SET:
        pos += 1
    if pos == start:
        raise ValueError(
            f'invalid Transfer-Encoding token at position {pos}')
    return value[start:pos], pos


def _transfer_quoted_string(value: bytes, pos: int) -> int:
    """The opening quote is consumed here.  quoted-pair permits only
    HTAB/SP/VCHAR/obs-text after the backslash."""
    pos += 1
    while pos < len(value):
        octet = value[pos]
        if octet == 0x22:
            return pos + 1
        if octet == 0x5c:
            pos += 1
            if pos >= len(value) or value[pos] not in FIELD_VALUE_ALLOWED_SET:
                raise ValueError(
                    'invalid quoted Transfer-Encoding parameter')
        elif octet not in FIELD_VALUE_ALLOWED_SET:
            raise ValueError(
                'invalid quoted Transfer-Encoding parameter')
        pos += 1
    raise ValueError('unterminated quoted Transfer-Encoding parameter')


def parse_status(value: str | bytes) -> int | None:
    """A status code as RFC 9110 §15 writes one, or ``None`` if it is not.

    Three ASCII digits and nothing else.  There is deliberately no 100-599
    bound: a range one transport enforces and the other does not is an accept
    set the two disagree on, and neither protocol draws one here.
    """
    if isinstance(value, bytes):
        try:
            value = value.decode('ascii')
        except UnicodeDecodeError:
            return None
    if len(value) != 3 or not value.isdigit() or not value.isascii():
        return None
    return int(value)


#: RFC 9112 §6.3 rule 1 — a response to HEAD and any of these has no body and
#: no trailer section, whatever the field sections say.
NO_CONTENT_STATUSES: frozenset[int] = frozenset(range(100, 200)) | {204, 304}
#: What a server may not generate content in. RFC 9110 §15.3.6 adds 205 to
#: the set above, and only to what it sends.
NO_CONTENT_GENERATED_STATUSES: frozenset[int] = NO_CONTENT_STATUSES | {205}


def is_informational(status: int) -> bool:
    """Whether *status* is an informational (1xx) response — RFC 9110 §15.

    An informational response is provisional: it shares its sender with the
    final response that must still follow, so it commits no status, completes
    no exchange, and carries no content framing.
    """
    return 100 <= status < 200


def method_is(method: str | bytes | HTTPMethod | None, expected: str) -> bool:
    """Whether *method* is exactly *expected*.

    RFC 9110 §9.1 makes a method name case-sensitive, so `head` is not
    `HEAD` and folding the two together changes whether a response may
    carry content.
    """
    if method.__class__ is str:
        return method == expected
    if method.__class__ is bytes:
        return method == expected.encode('ascii')
    return method is not None and str(method) == expected


def response_has_content(method: str | bytes | HTTPMethod | None,
                         status: int) -> bool:
    """How the body length of a response to *method* with *status* is found.

    RFC 9112 §6.3 rule 1: a response to HEAD (RFC 9110 §9.3.2) and any 1xx
    (§15.2), 204 (§15.3.5) or 304 (§15.4.5) has none whatever the field
    sections say, and an informational response is not the response yet.

    This is the framing question and it stops there. RFC 9110 §15.3.6 also
    forbids a *server* to generate content in a 205, but RFC 9112 §6.3 still
    frames one normally, so a peer that sends one has to be read to the
    length it declared — leaving those octets behind would desynchronise the
    reader from the very peer that was already misbehaving. The generation
    rule lives on the sender beside it, not in here.
    """
    return status not in NO_CONTENT_STATUSES and not method_is(method, 'HEAD')
